"""
v0.9.3: audit / locks MCP ツール (experimental)

- `query_audit`: audit table を filter + cursor pagination で取得
- `list_locks`: 現在の resource lock 一覧 (include_stale)
"""
from __future__ import annotations
import logging

from fastmcp import FastMCP

from lab_executor.audit import AuditStore
from lab_executor.job import JobManager
from lab_executor.response_envelope import make_envelope, make_error

logger = logging.getLogger(__name__)


def register_tools(mcp: FastMCP, job_mgr: JobManager) -> None:
    audit = AuditStore(job_mgr.store)

    def _audit_unavailable_response() -> dict | None:
        """v0.9.3.1: AuditStore init 失敗時 (visibility のため明示 envelope)"""
        if getattr(job_mgr, "_audit_init_error", False) and job_mgr.audit is None:
            return make_envelope(
                "error",
                errors=[make_error(
                    "internal",
                    "AuditStore が初期化されていません。audit / locks 機能は無効です",
                    recoverable=False,
                    details={"sub_class": "audit_unavailable"},
                )],
            )
        return None

    @mcp.tool()
    async def query_audit(
        job_id: str = "",
        resource: str = "",
        owner: str = "",
        event_type: str = "",
        severity: str = "",
        since: str = "",
        until: str = "",
        limit: int = 200,
        cursor: dict | None = None,
        include_details: bool = False,
    ) -> dict:
        """**(experimental, v0.9.3)** 監査ログを filter + cursor pagination で取得

        対象: MCP tool 呼び出し / resource lock / safety 拒否 / export 等の
        運用イベント (job_events と重複する Job 内部進行は含まない)。

        引数 (すべて任意 filter):
          job_id / resource / owner / event_type / severity / since / until

        cursor (前回返却の `pagination.next_cursor` をそのまま渡す):
          `{"timestamp": "...", "audit_id": "aud_..."}`

        limit 上限 5000 にクランプ。`include_details=true` で
        request_summary / response_summary / metadata も同梱 (default false)。
        """
        unavail = _audit_unavailable_response()
        if unavail is not None:
            return unavail
        if limit <= 0:
            limit = 200
        clamp_warning = None
        if limit > 5000:
            clamp_warning = f"limit={limit} は上限 5000 にクランプ"
            limit = 5000
        try:
            events, next_cursor = audit.query(
                job_id=job_id or None,
                resource=resource or None,
                owner=owner or None,
                event_type=event_type or None,
                severity=severity or None,
                since=since or None,
                until=until or None,
                limit=limit,
                cursor=cursor or None,
                include_details=include_details,
            )
        except Exception as e:
            logger.exception("query_audit 失敗")
            return make_envelope(
                "error",
                errors=[make_error("internal", str(e), recoverable=False)],
            )

        data = {
            "events": events,
            "pagination": {
                "limit": limit,
                "returned": len(events),
                "has_more": next_cursor is not None,
                "next_cursor": next_cursor,
            },
            "include_details": include_details,
        }
        if clamp_warning:
            data["clamp_warning"] = clamp_warning
        return make_envelope("ok", data=data)

    @mcp.tool()
    async def list_locks(
        resource: str = "",
        owner: str = "",
        include_stale: bool = True,
    ) -> dict:
        """**(experimental, v0.9.3)** 現在の resource lock 一覧

        各 lock の `stale` フィールドで lease 切れを判定可能。`include_stale=false`
        で stale を除外。

        AI エージェントは、blocked response の `blocked_by` 情報と組み合わせ、
        必要なら `cancel_job` / `wait_and_retry` を判断する。
        """
        unavail = _audit_unavailable_response()
        if unavail is not None:
            return unavail
        try:
            locks = audit.list_locks(
                resource=resource or None,
                owner=owner or None,
                include_stale=include_stale,
            )
        except Exception as e:
            return make_envelope(
                "error",
                errors=[make_error("internal", str(e), recoverable=False)],
            )
        return make_envelope("ok", data={
            "locks": locks,
            "count": len(locks),
            "include_stale": include_stale,
        })
