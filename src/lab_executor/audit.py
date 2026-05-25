"""
v0.9.3: Audit + locks (operational integrity, experimental)

JobStore の SQLite 接続を共有して `audit` / `locks` テーブルへ書き込む。
監査・lock 競合追跡・stale lock 検出のための薄い層。

- audit と job_events を**重複させない**:
  job_events は「Job 内部の進行」、audit は「外部から見た操作・セキュリティ・
  運用記録」(tool_called / lock_blocked / safety_blocked 等)。
- request/response payload は `summarize_for_audit` で要約 (sensitive
  keys redact + 長い文字列は head + truncated=true)。
"""
from __future__ import annotations
import json
import logging
import threading
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Iterable

logger = logging.getLogger(__name__)


SENSITIVE_KEYS = (
    "token", "api_key", "apikey", "password", "secret", "authorization",
    "credentials",
)

# tool 引数 / 結果のうち大きすぎる data array は count に置換
TRUNCATE_STR_LEN = 200
TRUNCATE_LIST_LEN = 5


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ============================================================
# Redaction / summary
# ============================================================


def summarize_for_audit(payload: Any, *, depth: int = 0) -> Any:
    """audit 用に payload を安全に要約する (sensitive keys redact + 長文 truncate)"""
    if depth > 6:
        return "<deep>"
    if payload is None or isinstance(payload, (bool, int, float)):
        return payload
    if isinstance(payload, str):
        if len(payload) > TRUNCATE_STR_LEN:
            return {
                "_truncated": True,
                "len": len(payload),
                "head": payload[:TRUNCATE_STR_LEN],
            }
        return payload
    if isinstance(payload, dict):
        out: dict[str, Any] = {}
        for k, v in payload.items():
            key_lower = str(k).lower()
            if any(s in key_lower for s in SENSITIVE_KEYS):
                out[k] = "[REDACTED]"
                continue
            out[k] = summarize_for_audit(v, depth=depth + 1)
        return out
    if isinstance(payload, (list, tuple)):
        if len(payload) > TRUNCATE_LIST_LEN:
            return {
                "_truncated_list": True,
                "len": len(payload),
                "head": [summarize_for_audit(x, depth=depth + 1)
                         for x in payload[:TRUNCATE_LIST_LEN]],
            }
        return [summarize_for_audit(x, depth=depth + 1) for x in payload]
    # 不明型は str() に
    return {"_repr": str(payload)[:TRUNCATE_STR_LEN]}


# ============================================================
# AuditStore
# ============================================================


SEVERITY_LEVELS = ("info", "warning", "error", "critical")


class AuditStore:
    """JobStore の接続を共有して audit / locks テーブルを扱う。

    JobStore が thread-local 接続を持つため、AuditStore は JobStore を
    保持するだけで、書き込み時に `store._connect()` 経由でアクセスする。
    """

    def __init__(self, job_store, *, write_lock: threading.Lock | None = None):
        self._store = job_store
        # JobStore 既存の _write_lock を共有する (DB 競合を避ける)
        self._write_lock = write_lock or job_store._write_lock

    # ---- audit ----

    def record_event(
        self,
        event_type: str,
        *,
        status: str = "ok",
        severity: str = "info",
        owner: str | None = None,
        client_id: str | None = None,
        tool_name: str | None = None,
        job_id: str | None = None,
        resource: str | None = None,
        target_id: str | None = None,
        error_class: str | None = None,
        message: str | None = None,
        request: Any = None,
        response: Any = None,
        metadata: Any = None,
    ) -> str:
        """audit event を 1 件書き込む。返り値 = audit_id"""
        if severity not in SEVERITY_LEVELS:
            severity = "info"
        audit_id = f"aud_{uuid.uuid4().hex[:16]}"
        try:
            req_json = (json.dumps(
                summarize_for_audit(request), ensure_ascii=False,
                default=str,
            ) if request is not None else None)
        except Exception:
            req_json = None
        try:
            resp_json = (json.dumps(
                summarize_for_audit(response), ensure_ascii=False,
                default=str,
            ) if response is not None else None)
        except Exception:
            resp_json = None
        try:
            meta_json = (json.dumps(metadata, ensure_ascii=False, default=str)
                          if metadata is not None else None)
        except Exception:
            meta_json = None

        with self._write_lock:
            self._store._connect().execute(
                """
                INSERT INTO audit
                (audit_id, timestamp, event_type, severity, owner, client_id,
                 tool_name, job_id, resource, target_id, status, error_class,
                 message, request_summary_json, response_summary_json,
                 metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    audit_id, _now_iso(), event_type, severity, owner,
                    client_id, tool_name, job_id, resource, target_id,
                    status, error_class, message,
                    req_json, resp_json, meta_json,
                ),
            )
        return audit_id

    def query(
        self,
        *,
        job_id: str | None = None,
        resource: str | None = None,
        owner: str | None = None,
        event_type: str | None = None,
        severity: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 200,
        cursor: dict | None = None,
        include_details: bool = False,
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        """audit table を filter + cursor pagination で取得。

        Returns:
            (events, next_cursor)
        """
        wheres: list[str] = []
        params: list[Any] = []
        if job_id:
            wheres.append("job_id = ?")
            params.append(job_id)
        if resource:
            wheres.append("resource = ?")
            params.append(resource)
        if owner:
            wheres.append("owner = ?")
            params.append(owner)
        if event_type:
            wheres.append("event_type = ?")
            params.append(event_type)
        if severity:
            wheres.append("severity = ?")
            params.append(severity)
        if since:
            wheres.append("timestamp >= ?")
            params.append(since)
        if until:
            wheres.append("timestamp < ?")
            params.append(until)
        # cursor: 過去ページの末尾より「古い」ものを取る (新しい順)
        if cursor and isinstance(cursor, dict):
            cts = cursor.get("timestamp")
            cid = cursor.get("audit_id")
            if cts is not None and cid is not None:
                wheres.append(
                    "(timestamp < ? OR (timestamp = ? AND audit_id < ?))"
                )
                params.extend([cts, cts, cid])

        where_sql = (" WHERE " + " AND ".join(wheres)) if wheres else ""
        q = (
            "SELECT * FROM audit" + where_sql
            + " ORDER BY timestamp DESC, audit_id DESC LIMIT ?"
        )
        params.append(int(limit) + 1)
        rows = self._store._connect().execute(q, tuple(params)).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        events: list[dict[str, Any]] = []
        for r in rows:
            ev: dict[str, Any] = {
                "audit_id": r["audit_id"],
                "timestamp": r["timestamp"],
                "event_type": r["event_type"],
                "severity": r["severity"],
                "owner": r["owner"],
                "client_id": r["client_id"],
                "tool_name": r["tool_name"],
                "job_id": r["job_id"],
                "resource": r["resource"],
                "target_id": r["target_id"],
                "status": r["status"],
                "error_class": r["error_class"],
                "message": r["message"],
            }
            if include_details:
                if r["request_summary_json"]:
                    try:
                        ev["request_summary"] = json.loads(
                            r["request_summary_json"])
                    except Exception:
                        ev["request_summary"] = None
                if r["response_summary_json"]:
                    try:
                        ev["response_summary"] = json.loads(
                            r["response_summary_json"])
                    except Exception:
                        ev["response_summary"] = None
                if r["metadata_json"]:
                    try:
                        ev["metadata"] = json.loads(r["metadata_json"])
                    except Exception:
                        ev["metadata"] = None
            events.append(ev)

        next_cursor: dict | None = None
        if has_more and events:
            tail = events[-1]
            next_cursor = {
                "timestamp": tail["timestamp"],
                "audit_id": tail["audit_id"],
            }
        return events, next_cursor

    # ---- locks ----

    def acquire_lock(
        self,
        resource: str,
        *,
        owner: str,
        job_id: str | None = None,
        client_id: str | None = None,
        lease_seconds: float | None = 3600.0,
        lock_reason: str | None = None,
        metadata: dict | None = None,
    ) -> dict[str, Any]:
        """lock を INSERT。既存があれば blocked_by を返す。

        Returns:
            {"acquired": True, "lock": {...}} or
            {"acquired": False, "blocked_by": {...}, "stale": bool}
        """
        with self._write_lock:
            conn = self._store._connect()
            existing = conn.execute(
                "SELECT * FROM locks WHERE resource=?", (resource,),
            ).fetchone()
            if existing is not None:
                lease_until = existing["lease_until"]
                stale = False
                try:
                    if lease_until:
                        lu = datetime.fromisoformat(lease_until)
                        stale = lu < datetime.now(timezone.utc)
                except Exception:
                    pass
                if not stale:
                    return {
                        "acquired": False,
                        "blocked_by": {
                            "owner": existing["owner"],
                            "job_id": existing["job_id"],
                            "client_id": existing["client_id"],
                            "acquired_at": existing["acquired_at"],
                            "lease_until": existing["lease_until"],
                            "lock_reason": existing["lock_reason"],
                        },
                        "stale": False,
                    }
                # stale なら上書き取得を許可
                conn.execute("DELETE FROM locks WHERE resource=?", (resource,))

            acquired_at = _now_iso()
            lease_until: str | None = None
            if lease_seconds is not None and lease_seconds > 0:
                lease_until = (
                    datetime.now(timezone.utc)
                    + timedelta(seconds=lease_seconds)
                ).isoformat(timespec="seconds")
            try:
                meta_json = (json.dumps(metadata, ensure_ascii=False,
                                         default=str)
                              if metadata else None)
            except Exception:
                meta_json = None
            conn.execute(
                """
                INSERT INTO locks
                (resource, owner, job_id, client_id, acquired_at, lease_until,
                 lock_reason, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    resource, owner, job_id, client_id, acquired_at,
                    lease_until, lock_reason, meta_json,
                ),
            )
            return {
                "acquired": True,
                "lock": {
                    "resource": resource, "owner": owner, "job_id": job_id,
                    "client_id": client_id, "acquired_at": acquired_at,
                    "lease_until": lease_until, "lock_reason": lock_reason,
                },
            }

    def release_lock(
        self,
        resource: str,
        *,
        owner: str | None = None,
        only_if_owner_matches: bool = True,
    ) -> bool:
        """lock を削除。owner 指定 + only_if_owner_matches=True なら所有者
        一致時のみ解放。"""
        with self._write_lock:
            conn = self._store._connect()
            if owner and only_if_owner_matches:
                cur = conn.execute(
                    "DELETE FROM locks WHERE resource=? AND owner=?",
                    (resource, owner),
                )
            else:
                cur = conn.execute(
                    "DELETE FROM locks WHERE resource=?", (resource,),
                )
            return cur.rowcount > 0

    def list_locks(
        self,
        *,
        resource: str | None = None,
        owner: str | None = None,
        include_stale: bool = True,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        wheres: list[str] = []
        params: list[Any] = []
        if resource:
            wheres.append("resource = ?")
            params.append(resource)
        if owner:
            wheres.append("owner = ?")
            params.append(owner)
        where_sql = (" WHERE " + " AND ".join(wheres)) if wheres else ""
        rows = self._store._connect().execute(
            f"SELECT * FROM locks{where_sql} ORDER BY acquired_at ASC",
            tuple(params),
        ).fetchall()
        now = now or datetime.now(timezone.utc)
        out: list[dict[str, Any]] = []
        for r in rows:
            stale = False
            if r["lease_until"]:
                try:
                    stale = datetime.fromisoformat(r["lease_until"]) < now
                except Exception:
                    stale = False
            if stale and not include_stale:
                continue
            try:
                meta = (json.loads(r["metadata_json"])
                         if r["metadata_json"] else None)
            except Exception:
                meta = None
            out.append({
                "resource": r["resource"],
                "owner": r["owner"],
                "job_id": r["job_id"],
                "client_id": r["client_id"],
                "acquired_at": r["acquired_at"],
                "lease_until": r["lease_until"],
                "lock_reason": r["lock_reason"],
                "stale": stale,
                "metadata": meta,
            })
        return out

    def release_stale_locks(self) -> int:
        """サーバ起動時等に lease 切れ lock を一括削除。返り値 = 削除数。"""
        now = _now_iso()
        with self._write_lock:
            cur = self._store._connect().execute(
                "DELETE FROM locks WHERE lease_until IS NOT NULL "
                "AND lease_until < ?",
                (now,),
            )
            return cur.rowcount
