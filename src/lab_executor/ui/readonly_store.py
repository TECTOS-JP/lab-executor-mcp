"""read-only SQLite アクセサ (Web UI M1)。

実験ランタイムの state DB を **書き込み一切なし** で読むための薄いラッパ。
``JobStore`` (job/store.py) はコンストラクタで schema 作成 = 書き込みを行うため
UI からはインスタンス化しない。代わりに接続をリクエスト毎に
``sqlite3.connect(f"file:{path}?mode=ro", uri=True)`` で開き、
``PRAGMA query_only=ON`` を併用して二重に書き込みを禁止する。

返す dict のキー名は ``JobStore.list_events`` / ``list_steps`` /
``list_target_runs`` / ``_row_to_record`` と揃え、views.py と observation.py が
そのまま消費できるようにする。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

# JobStore はインスタンス化しないが、行→dict 変換の staticmethod
# `_row_to_record` は再利用してよい (計画書の指示)。
from lab_executor.job.store import JobStore


class UiStoreError(Exception):
    """DB 不在・テーブル不在など、UI が案内ページを出すための例外。"""


# UI が参照する主要テーブル。1 つでも欠ければ「古い / 未初期化の DB」とみなす。
_REQUIRED_TABLES = ("jobs", "job_events", "job_steps", "target_runs")


class ReadOnlyJobStore:
    """state DB を read-only で読むアクセサ。

    ``db_path`` は存在チェックのみ行い、実際の接続はメソッド呼び出し毎に開いて
    閉じる (long-lived connection を持たない)。
    """

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path)

    @property
    def db_path(self) -> Path:
        return self._db_path

    # ---------- 接続 ----------

    def _connect(self) -> sqlite3.Connection:
        """read-only 接続を開く。存在しない / テーブル不在なら UiStoreError。"""
        if not self._db_path.exists():
            raise UiStoreError(
                f"state DB が見つかりません: {self._db_path}\n"
                "実験サーバ (serve) がまだ一度も起動していないか、"
                "--db のパスが誤っている可能性があります。"
            )
        # mode=ro: ファイルは存在必須、書き込み不可。uri=True 必須。
        uri = f"file:{self._db_path.as_posix()}?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True)
        except sqlite3.OperationalError as e:  # pragma: no cover - 稀
            raise UiStoreError(f"state DB を開けません: {e}") from e
        conn.row_factory = sqlite3.Row
        # 二重防護: query_only を立てて writer 系の文を拒否させる。
        conn.execute("PRAGMA query_only=ON;")
        self._ensure_tables(conn)
        return conn

    @staticmethod
    def _ensure_tables(conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        present = {r["name"] for r in rows}
        missing = [t for t in _REQUIRED_TABLES if t not in present]
        if missing:
            raise UiStoreError(
                "state DB のスキーマが古い / 未初期化です "
                f"(不足テーブル: {', '.join(missing)})。"
                "新しい serve を一度起動して migration を走らせてください。"
            )

    # ---------- jobs ----------

    def list_jobs(
        self,
        status_filter: list[str] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """jobs を新しい順に取得し、``_row_to_record().to_dict()`` と同形の dict を返す。"""
        q = "SELECT * FROM jobs"
        params: list[Any] = []
        if status_filter:
            placeholders = ",".join("?" * len(status_filter))
            q += f" WHERE status IN ({placeholders})"
            params.extend(status_filter)
        q += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(int(limit))
        conn = self._connect()
        try:
            rows = conn.execute(q, tuple(params)).fetchall()
            return [JobStore._row_to_record(r).to_dict() for r in rows]
        finally:
            conn.close()

    def list_jobs_with_last_event(
        self,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """jobs と「各 job の最新 event_type」を **1 クエリ** で取得 (N+1 解消)。

        M1 の ``_build_job_rows`` は job 毎に ``list_events(limit=1)`` を発行して
        いた (N+1)。ここでは相関サブクエリで最新 event_type を 1 度に引く。

        返り値は ``_row_to_record().to_dict()`` に ``last_event_type`` キーを
        足した dict のリスト (新しい順)。
        """
        q = (
            "SELECT j.*, ("
            "  SELECT e.event_type FROM job_events e"
            "  WHERE e.job_id = j.job_id"
            "  ORDER BY e.event_id DESC LIMIT 1"
            ") AS last_event_type "
            "FROM jobs j "
            "ORDER BY j.created_at DESC, j.rowid DESC LIMIT ?"
        )
        conn = self._connect()
        try:
            rows = conn.execute(q, (int(limit),)).fetchall()
            out: list[dict[str, Any]] = []
            for r in rows:
                record = JobStore._row_to_record(r).to_dict()
                record["last_event_type"] = r["last_event_type"]
                out.append(record)
            return out
        finally:
            conn.close()

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            return JobStore._row_to_record(row).to_dict() if row else None
        finally:
            conn.close()

    # ---------- events / steps / target_runs ----------
    # 下記 3 メソッドは JobStore の同名メソッドと同じキー名の dict を返す。

    def list_events(
        self, job_id: str, limit: int = 200, offset: int = 0
    ) -> list[dict[str, Any]]:
        """job_events を新しい順に取得 (JobStore.list_events と同形)。"""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM job_events WHERE job_id = ? "
                "ORDER BY event_id DESC LIMIT ? OFFSET ?",
                (job_id, int(limit), int(offset)),
            ).fetchall()
            return [
                {
                    "event_id": r["event_id"],
                    "job_id": r["job_id"],
                    "timestamp": r["timestamp"],
                    "event_type": r["event_type"],
                    "target_id": r["target_id"],
                    "step_index": r["step_index"],
                    "payload": (
                        json.loads(r["payload_json"]) if r["payload_json"] else None
                    ),
                }
                for r in rows
            ]
        finally:
            conn.close()

    def list_steps(self, job_id: str) -> list[dict[str, Any]]:
        """job_steps を古い順に取得 (JobStore.list_steps と同形)。"""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM job_steps WHERE job_id = ? ORDER BY id ASC",
                (job_id,),
            ).fetchall()
            return [
                {
                    "id": r["id"],
                    "job_id": r["job_id"],
                    "target_id": r["target_id"],
                    "step_index": r["step_index"],
                    "step_type": r["step_type"],
                    "status": r["status"],
                    "started_at": r["started_at"],
                    "ended_at": r["ended_at"],
                    "result": json.loads(r["result_json"]) if r["result_json"] else None,
                    "error": json.loads(r["error_json"]) if r["error_json"] else None,
                }
                for r in rows
            ]
        finally:
            conn.close()

    def list_target_runs(self, job_id: str) -> list[dict[str, Any]]:
        """target_runs を古い順に取得 (JobStore.list_target_runs と同形)。"""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM target_runs WHERE job_id = ? ORDER BY id ASC",
                (job_id,),
            ).fetchall()
            return [
                {
                    "id": r["id"],
                    "job_id": r["job_id"],
                    "target_id": r["target_id"],
                    "status": r["status"],
                    "started_at": r["started_at"],
                    "ended_at": r["ended_at"],
                    "required_resources": (
                        json.loads(r["required_resources_json"])
                        if r["required_resources_json"] else []
                    ),
                    "bindings": (
                        json.loads(r["bindings_json"]) if r["bindings_json"] else {}
                    ),
                    "parameters": (
                        json.loads(r["parameters_json"]) if r["parameters_json"] else {}
                    ),
                    "result": json.loads(r["result_json"]) if r["result_json"] else None,
                    "error": json.loads(r["error_json"]) if r["error_json"] else None,
                }
                for r in rows
            ]
        finally:
            conn.close()

    # ---------- job_pauses (v2.30.0 SP-4) ----------

    def get_active_pause(self, job_id: str) -> dict[str, Any] | None:
        """未解決 (resolution IS NULL) の pause レコードを返す (JobStore と同形)。

        古い DB (job_pauses テーブル未作成) では None を返す (エラーにしない —
        pause 機能を使っていない DB を UI が読めなくならないように)。
        """
        conn = self._connect()
        try:
            try:
                row = conn.execute(
                    "SELECT * FROM job_pauses "
                    "WHERE job_id = ? AND resolution IS NULL "
                    "ORDER BY id DESC LIMIT 1",
                    (job_id,),
                ).fetchone()
            except sqlite3.OperationalError:
                return None  # テーブル未作成 (migration 前の DB)
            if row is None:
                return None
            return {
                "id": row["id"],
                "job_id": row["job_id"],
                "step_path": row["step_path"],
                "message": row["message"],
                "expose": (
                    json.loads(row["expose_json"]) if row["expose_json"] else {}
                ),
                "requested_at": row["requested_at"],
                "timeout_at": row["timeout_at"],
                "resolution": row["resolution"],
            }
        finally:
            conn.close()

    # ---------- health ----------

    def health(self) -> dict[str, Any]:
        """serve プロセスの死活の「目安」。

        ``last_write_at`` は ``MAX(jobs.updated_at)`` と ``MAX(job_events.timestamp)``
        の大きい方。断定はできない (stdio serve が複数立つ構成のため、最後に誰かが
        書いた時刻しか分からない)。
        """
        from datetime import datetime, timezone

        conn = self._connect()
        try:
            row = conn.execute("SELECT MAX(updated_at) AS m FROM jobs").fetchone()
            max_job = row["m"] if row else None
            row = conn.execute(
                "SELECT MAX(timestamp) AS m FROM job_events"
            ).fetchone()
            max_evt = row["m"] if row else None
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM jobs "
                "WHERE status IN ('queued','running','waiting','cancelling')"
            ).fetchone()
            active = int(row["n"]) if row else 0
        finally:
            conn.close()

        candidates = [x for x in (max_job, max_evt) if x]
        last_write_at = max(candidates) if candidates else None

        seconds_since: float | None = None
        if last_write_at:
            try:
                dt = datetime.fromisoformat(last_write_at)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                seconds_since = (
                    datetime.now(timezone.utc) - dt
                ).total_seconds()
            except (ValueError, TypeError):
                seconds_since = None

        return {
            "db_path": str(self._db_path),
            "last_write_at": last_write_at,
            "seconds_since_last_write": (
                round(seconds_since, 1) if seconds_since is not None else None
            ),
            "active_jobs": active,
        }
