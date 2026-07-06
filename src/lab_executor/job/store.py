"""
Job メタデータの SQLite 永続化 (v0.5.0-rc2, 最小実装)

v0.5.0-rc2 では `jobs` テーブルのみ。完全永続化 (measurement_cache / locks /
monitor_data / audit / job_steps) は v0.7.0 で拡張する。

スキーマ:
  jobs:
    job_id              TEXT PRIMARY KEY
    owner               TEXT
    resource_name       TEXT
    recipe              TEXT      -- recipe 名
    parameters_json     TEXT      -- JSON dump
    status              TEXT      -- JobStatus value
    current_step_index  INTEGER
    error_class         TEXT      -- failed/timeout 時のエラー分類
    last_step_summary   TEXT      -- 直近 step の人間向け 1 行サマリ
    result_json         TEXT      -- 完了/失敗時の steps_executed JSON
    created_at          TEXT      -- ISO 8601
    updated_at          TEXT
"""
from __future__ import annotations
import json
import logging
import os
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lab_executor.job.state_machine import JobStatus, validate_transition

logger = logging.getLogger(__name__)


def default_store_path() -> Path:
    """環境変数 VISA_MCP_STATE_DB または ~/.visa-mcp/state.sqlite"""
    raw = os.environ.get("VISA_MCP_STATE_DB")
    if raw:
        return Path(raw)
    return Path.home() / ".visa-mcp" / "state.sqlite"


@dataclass
class JobRecord:
    """SQLite 1 行に対応する Job のメタデータ"""
    job_id: str
    owner: str = ""
    resource_name: str = ""
    recipe: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    status: JobStatus = JobStatus.QUEUED
    current_step_index: int = -1
    error_class: str = ""
    last_step_summary: str = ""
    result: dict[str, Any] | None = None
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "owner": self.owner,
            "resource_name": self.resource_name,
            "recipe": self.recipe,
            "parameters": self.parameters,
            "status": self.status.value,
            "current_step_index": self.current_step_index,
            "error_class": self.error_class,
            "last_step_summary": self.last_step_summary,
            "result": self.result,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id              TEXT PRIMARY KEY,
    owner               TEXT NOT NULL DEFAULT '',
    resource_name       TEXT NOT NULL DEFAULT '',
    recipe              TEXT NOT NULL DEFAULT '',
    parameters_json     TEXT NOT NULL DEFAULT '{}',
    status              TEXT NOT NULL,
    current_step_index  INTEGER NOT NULL DEFAULT -1,
    error_class         TEXT NOT NULL DEFAULT '',
    last_step_summary   TEXT NOT NULL DEFAULT '',
    result_json         TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_resource ON jobs(resource_name);
"""

# v0.7.0: Persistence の本格拡張。
# 既存 v0.5.x の jobs テーブルは保持し、新規テーブルのみ追加 (ALTER TABLE 不要)。
# PRAGMA user_version=1 でこの schema を識別。
_SCHEMA_V0_7_0_ADDITIONS = """
-- step 単位の実行履歴 (target_id NULL なら単一 Job、非 NULL なら Map/Group の target 内 step)
CREATE TABLE IF NOT EXISTS job_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    target_id TEXT,
    step_index INTEGER NOT NULL,
    step_type TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    result_json TEXT,
    error_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_job_steps_job ON job_steps(job_id);
CREATE INDEX IF NOT EXISTS idx_job_steps_target ON job_steps(job_id, target_id);

-- Group/Map ジョブの target 単位集約
CREATE TABLE IF NOT EXISTS target_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    required_resources_json TEXT,
    bindings_json TEXT,
    parameters_json TEXT,
    result_json TEXT,
    error_json TEXT,
    UNIQUE(job_id, target_id)
);
CREATE INDEX IF NOT EXISTS idx_target_runs_job ON target_runs(job_id);

-- 時系列イベント: barrier/stagger/poll/cancel/safe_shutdown 等
CREATE TABLE IF NOT EXISTS job_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,
    target_id TEXT,
    step_index INTEGER,
    payload_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_job_events_job_ts ON job_events(job_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_job_events_type ON job_events(event_type);

-- 最新測定値キャッシュ (上書き)
CREATE TABLE IF NOT EXISTS measurement_cache (
    instrument TEXT NOT NULL,
    measurement TEXT NOT NULL,
    value_json TEXT NOT NULL,
    unit TEXT,
    timestamp TEXT NOT NULL,
    source_job_id TEXT,
    PRIMARY KEY (instrument, measurement)
);

-- monitor jobs の時系列データ (start_monitor の出力)
CREATE TABLE IF NOT EXISTS monitor_data (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    monitor_id TEXT NOT NULL,
    instrument TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    value_json TEXT NOT NULL,
    sample_count INTEGER
);
CREATE INDEX IF NOT EXISTS idx_monitor_data_id_ts ON monitor_data(monitor_id, timestamp);
"""


# v0.8.0 で追加: experiment_plans + experiment_templates
_SCHEMA_V0_8_0_ADDITIONS = """
-- DSL plan の永続化 (v0.8.0): 1 Job につき 1 plan
CREATE TABLE IF NOT EXISTS experiment_plans (
    plan_id TEXT PRIMARY KEY,
    job_id TEXT,
    name TEXT NOT NULL DEFAULT '',
    dsl_version TEXT NOT NULL,
    original_plan_json TEXT NOT NULL,
    compiled_summary_json TEXT,
    validation_result_json TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_experiment_plans_job ON experiment_plans(job_id);

-- 再利用可能テンプレート
CREATE TABLE IF NOT EXISTS experiment_templates (
    name TEXT PRIMARY KEY,
    dsl_version TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


# v0.9.3: audit + locks additions (operational integrity)
_SCHEMA_V0_9_3_ADDITIONS = """
CREATE TABLE IF NOT EXISTS audit (
    audit_id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    owner TEXT,
    client_id TEXT,
    tool_name TEXT,
    job_id TEXT,
    resource TEXT,
    target_id TEXT,
    status TEXT NOT NULL,
    error_class TEXT,
    message TEXT,
    request_summary_json TEXT,
    response_summary_json TEXT,
    metadata_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_job_id ON audit(job_id);
CREATE INDEX IF NOT EXISTS idx_audit_resource ON audit(resource);
CREATE INDEX IF NOT EXISTS idx_audit_owner ON audit(owner);
CREATE INDEX IF NOT EXISTS idx_audit_event_type ON audit(event_type);

CREATE TABLE IF NOT EXISTS locks (
    resource TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    job_id TEXT,
    client_id TEXT,
    acquired_at TEXT NOT NULL,
    lease_until TEXT,
    lock_reason TEXT,
    metadata_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_locks_owner ON locks(owner);
CREATE INDEX IF NOT EXISTS idx_locks_job_id ON locks(job_id);
"""


# 現行 schema version (PRAGMA user_version で管理)
CURRENT_SCHEMA_VERSION = 3


def _apply_migrations(conn: sqlite3.Connection) -> int:
    """既存 DB を最新 schema へ migration する。

    PRAGMA user_version で現在 version を取得し、不足分を順に適用。
    既存 v0.5.x の DB (user_version=0) でも非破壊的に v1 へ上がる。

    返り値: 最終 user_version
    """
    row = conn.execute("PRAGMA user_version").fetchone()
    current = int(row[0]) if row is not None else 0

    if current < 1:
        # v0.5.x → v0.7.0: 新規テーブルを追加 (既存 jobs テーブルには触れない)
        conn.executescript(_SCHEMA_V0_7_0_ADDITIONS)
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        logger.info(
            "SQLite schema migration: user_version 0 → 1 (v0.7.0 additions)",
        )
        current = 1

    if current < 2:
        # v0.7.x → v0.8.0: experiment_plans / experiment_templates 追加
        conn.executescript(_SCHEMA_V0_8_0_ADDITIONS)
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
        logger.info(
            "SQLite schema migration: user_version 1 → 2 (v0.8.0 additions)",
        )
        current = 2

    if current < 3:
        # v0.8.x → v0.9.3: audit + locks 追加 (Operational integrity)
        conn.executescript(_SCHEMA_V0_9_3_ADDITIONS)
        conn.execute("PRAGMA user_version = 3")
        conn.commit()
        logger.info(
            "SQLite schema migration: user_version 2 → 3 "
            "(v0.9.3 additions: audit + locks)",
        )
        current = 3

    return current


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dumps_utf8_safe(obj: Any | None) -> str | None:
    """result / error dict を UTF-8 で安全に round-trip 可能な JSON へ直列化する。

    背景 (v2.22.x 文字化け調査):
      wait 系ジョブの WaitConditionTimeout など、日本語を含む message を
      result_json として保存する。通常経路 (MockBackend 等) では
      ``json.dumps(ensure_ascii=False)`` の結果は妥当な UTF-8 であり、
      ``last_step_summary`` (素の TEXT) と result_json 内 message は
      同一バイト列になる。

      しかし実機 (日本語 Windows + NI-VISA) では、VisaError 等の例外
      メッセージが CP932 由来のバイトを ``surrogateescape`` で抱えた
      ``str`` として dict に紛れ込むことがある。その場合:
        - ``ensure_ascii=False`` の json.dumps は成功する
        - しかし直後の SQLite への保存 (TEXT = UTF-8 encode) が
          ``UnicodeEncodeError: surrogates not allowed`` で失敗し、
          steps_executed ごと結果が失われる / 破損する。

      ここで surrogate を含む文字列を UTF-8 へ lossless に畳み込んでから
      保存することで、message が壊れず・欠落せず round-trip できることを
      保証する。正常な UTF-8 文字列 (日本語含む) は一切変化しない。
    """
    if obj is None:
        return None
    text = json.dumps(obj, ensure_ascii=False, default=str)
    # surrogate (孤立サロゲート = 非UTF-8 バイト由来) を検知したら
    # surrogatepass で bytes 化 → replace で decode し、確実に
    # UTF-8 保存可能な文字列へ正規化する。
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        text = text.encode("utf-8", "surrogatepass").decode("utf-8", "replace")
    return text


class JobStore:
    """
    Job メタデータの SQLite 永続化 (スレッドセーフ)。

    SQLite 接続は per-thread で持つ。書き込みは Lock で排他化。
    """

    def __init__(self, db_path: Path | str | None = None) -> None:
        self._db_path = Path(db_path) if db_path else default_store_path()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        self._local = threading.local()
        # 初期化時に schema を作成 + migration 実行
        # v0.5.x の jobs テーブルがある場合も非破壊的に v0.7.0 schema へ上がる
        conn = self._connect()
        conn.executescript(_SCHEMA_V1)
        conn.commit()
        _apply_migrations(conn)

    @property
    def db_path(self) -> Path:
        return self._db_path

    # v2.14.2: 明示 close + context manager (test teardown / Windows
    # の SQLite WAL/SHM file lock 解放のため)
    def close(self) -> None:
        """このスレッドで開いた SQLite connection を閉じる。
        他スレッドの connection はそのスレッドで GC される。
        多重 close は no-op。
        """
        conn = getattr(self._local, "conn", None)
        if conn is None:
            return
        try:
            conn.close()
        except Exception:
            pass
        try:
            del self._local.conn
        except Exception:
            pass

    def __enter__(self) -> "JobStore":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def _connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                str(self._db_path),
                isolation_level=None,  # autocommit; we control transactions
                detect_types=sqlite3.PARSE_DECLTYPES,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            self._local.conn = conn
        return conn

    # ---------- create / update / get ----------

    def create_job(
        self,
        job_id: str,
        owner: str,
        resource_name: str,
        recipe: str,
        parameters: dict[str, Any],
    ) -> JobRecord:
        now = _now_iso()
        rec = JobRecord(
            job_id=job_id,
            owner=owner,
            resource_name=resource_name,
            recipe=recipe,
            parameters=parameters,
            status=JobStatus.QUEUED,
            created_at=now,
            updated_at=now,
        )
        with self._write_lock:
            self._connect().execute(
                """
                INSERT INTO jobs
                (job_id, owner, resource_name, recipe, parameters_json,
                 status, current_step_index, error_class, last_step_summary,
                 result_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id, owner, resource_name, recipe,
                    json.dumps(parameters, ensure_ascii=False),
                    rec.status.value, -1, "", "",
                    None, now, now,
                ),
            )
        return rec

    def get(self, job_id: str) -> JobRecord | None:
        row = self._connect().execute(
            "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        return self._row_to_record(row) if row else None

    def list_jobs(
        self,
        status_filter: list[str] | None = None,
        limit: int = 50,
        owner: str | None = None,
    ) -> list[JobRecord]:
        q = "SELECT * FROM jobs"
        clauses: list[str] = []
        params: list[Any] = []
        if status_filter:
            placeholders = ",".join("?" * len(status_filter))
            clauses.append(f"status IN ({placeholders})")
            params.extend(status_filter)
        if owner is not None:
            clauses.append("owner = ?")
            params.append(owner)
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        # rowid を二次ソートに使うことで、同秒に複数 INSERT されても安定順序を得る
        q += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(int(limit))

        rows = self._connect().execute(q, tuple(params)).fetchall()
        return [self._row_to_record(r) for r in rows]

    # ---------- state transition ----------

    def transition_status(
        self,
        job_id: str,
        to_status: JobStatus,
        *,
        current_step_index: int | None = None,
        error_class: str | None = None,
        last_step_summary: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> JobRecord:
        """状態遷移 + 関連フィールドを atomic に更新"""
        with self._write_lock:
            rec = self.get(job_id)
            if rec is None:
                raise KeyError(f"job not found: {job_id}")
            # state machine による遷移検証
            validate_transition(rec.status, to_status)

            now = _now_iso()
            new_step = rec.current_step_index if current_step_index is None else current_step_index
            new_err = rec.error_class if error_class is None else error_class
            new_summary = rec.last_step_summary if last_step_summary is None else last_step_summary
            new_result_json = _dumps_utf8_safe(result)

            self._connect().execute(
                """
                UPDATE jobs
                SET status = ?, current_step_index = ?, error_class = ?,
                    last_step_summary = ?, result_json = COALESCE(?, result_json),
                    updated_at = ?
                WHERE job_id = ?
                """,
                (to_status.value, new_step, new_err, new_summary,
                 new_result_json, now, job_id),
            )
            return self.get(job_id)  # type: ignore[return-value]

    def update_step(
        self,
        job_id: str,
        current_step_index: int,
        last_step_summary: str = "",
    ) -> None:
        """状態は変えず、現在ステップだけ更新 (頻繁な呼び出し用)"""
        now = _now_iso()
        with self._write_lock:
            self._connect().execute(
                "UPDATE jobs SET current_step_index = ?, last_step_summary = ?, updated_at = ? "
                "WHERE job_id = ?",
                (current_step_index, last_step_summary, now, job_id),
            )

    # ---------- restart semantics ----------

    def mark_interrupted_on_startup(self) -> int:
        """
        起動時に queued/running/waiting/cancelling だった Job を interrupted に遷移させる。
        返り値: 遷移させた Job 数。

        v0.5.0.2: queued も対象に追加。queued Job がプロセス落ち直前に登録されたケースを救う。
        """
        active = [
            JobStatus.QUEUED.value,
            JobStatus.RUNNING.value,
            JobStatus.WAITING.value,
            JobStatus.CANCELLING.value,
        ]
        now = _now_iso()
        with self._write_lock:
            cur = self._connect().execute(
                f"SELECT COUNT(*) AS n FROM jobs WHERE status IN ({','.join('?' * len(active))})",
                tuple(active),
            ).fetchone()
            count = int(cur["n"]) if cur else 0
            if count > 0:
                self._connect().execute(
                    f"""
                    UPDATE jobs
                    SET status = ?, error_class = 'interrupted',
                        last_step_summary = 'server_restarted', updated_at = ?
                    WHERE status IN ({','.join('?' * len(active))})
                    """,
                    (JobStatus.INTERRUPTED.value, now, *active),
                )
                logger.warning(
                    "起動時に %d 件の実行中 Job を interrupted に遷移させました。", count
                )
            return count

    # ---------- helpers ----------

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> JobRecord:
        return JobRecord(
            job_id=row["job_id"],
            owner=row["owner"] or "",
            resource_name=row["resource_name"] or "",
            recipe=row["recipe"] or "",
            parameters=json.loads(row["parameters_json"] or "{}"),
            status=JobStatus(row["status"]),
            current_step_index=int(row["current_step_index"]),
            error_class=row["error_class"] or "",
            last_step_summary=row["last_step_summary"] or "",
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "",
        )

    # =====================================================================
    # v0.7.0: job_events / job_steps / target_runs / measurement_cache /
    #         monitor_data API
    # =====================================================================

    def record_event(
        self,
        job_id: str,
        event_type: str,
        *,
        target_id: str | None = None,
        step_index: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """job_events に時系列イベントを 1 件追加。

        event_type の標準値 (v0.7.0):
          job_created / job_started / job_completed / job_failed /
          job_cancelled / job_interrupted / job_timeout
          step_started / step_completed / step_failed
          target_started / target_completed / target_failed
          poll_sample (summary mode は省略) / poll_condition_met
          barrier_arrived / barrier_completed / barrier_timeout
          stagger_wait_started / stagger_wait_completed
          safe_shutdown_started / safe_shutdown_completed
          verify_passed / verify_failed
        """
        now = _now_iso()
        payload_json = _dumps_utf8_safe(payload)
        with self._write_lock:
            try:
                self._connect().execute(
                    """
                    INSERT INTO job_events
                    (job_id, timestamp, event_type, target_id, step_index, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (job_id, now, event_type, target_id, step_index, payload_json),
                )
            except Exception as e:
                logger.warning("record_event failed (event_type=%s): %s", event_type, e)

    def list_events(
        self,
        job_id: str,
        limit: int = 200,
        offset: int = 0,
        event_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """job_events を新しい順に取得"""
        q = "SELECT * FROM job_events WHERE job_id = ?"
        params: list[Any] = [job_id]
        if event_type is not None:
            q += " AND event_type = ?"
            params.append(event_type)
        q += " ORDER BY event_id DESC LIMIT ? OFFSET ?"
        params.extend([int(limit), int(offset)])
        rows = self._connect().execute(q, tuple(params)).fetchall()
        return [
            {
                "event_id": r["event_id"],
                "job_id": r["job_id"],
                "timestamp": r["timestamp"],
                "event_type": r["event_type"],
                "target_id": r["target_id"],
                "step_index": r["step_index"],
                "payload": json.loads(r["payload_json"]) if r["payload_json"] else None,
            }
            for r in rows
        ]

    # --- job_steps ---

    def record_step_started(
        self,
        job_id: str,
        step_index: int,
        step_type: str,
        target_id: str | None = None,
    ) -> int:
        """job_steps に新規 step エントリを INSERT (status=started)。返り値 = rowid"""
        now = _now_iso()
        with self._write_lock:
            cur = self._connect().execute(
                """
                INSERT INTO job_steps
                (job_id, target_id, step_index, step_type, status, started_at)
                VALUES (?, ?, ?, ?, 'started', ?)
                """,
                (job_id, target_id, step_index, step_type, now),
            )
            return cur.lastrowid or 0

    def record_step_completed(
        self,
        step_row_id: int,
        status: str,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        """既存 job_steps エントリの終端 (status=ok/failed/cancelled)"""
        now = _now_iso()
        with self._write_lock:
            self._connect().execute(
                """
                UPDATE job_steps
                SET status = ?, ended_at = ?,
                    result_json = ?, error_json = ?
                WHERE id = ?
                """,
                (
                    status, now,
                    _dumps_utf8_safe(result) if result else None,
                    _dumps_utf8_safe(error) if error else None,
                    step_row_id,
                ),
            )

    def list_steps(self, job_id: str, target_id: str | None = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM job_steps WHERE job_id = ?"
        params: list[Any] = [job_id]
        if target_id is not None:
            q += " AND target_id = ?"
            params.append(target_id)
        q += " ORDER BY id ASC"
        rows = self._connect().execute(q, tuple(params)).fetchall()
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

    # --- target_runs ---

    def upsert_target_run(
        self,
        job_id: str,
        target_id: str,
        status: str,
        *,
        required_resources: list[str] | None = None,
        bindings: dict[str, str] | None = None,
        parameters: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        is_start: bool = False,
    ) -> None:
        """target_runs を UPSERT。is_start=True なら started_at 設定、それ以外は ended_at"""
        now = _now_iso()
        with self._write_lock:
            existing = self._connect().execute(
                "SELECT id, started_at FROM target_runs WHERE job_id=? AND target_id=?",
                (job_id, target_id),
            ).fetchone()
            if existing is None:
                self._connect().execute(
                    """
                    INSERT INTO target_runs
                    (job_id, target_id, status, started_at, ended_at,
                     required_resources_json, bindings_json, parameters_json,
                     result_json, error_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id, target_id, status,
                        now if is_start else None,
                        None if is_start else now,
                        json.dumps(required_resources or [], ensure_ascii=False),
                        json.dumps(bindings or {}, ensure_ascii=False),
                        json.dumps(parameters or {}, ensure_ascii=False, default=str),
                        _dumps_utf8_safe(result) if result else None,
                        _dumps_utf8_safe(error) if error else None,
                    ),
                )
            else:
                self._connect().execute(
                    """
                    UPDATE target_runs
                    SET status=?,
                        ended_at = CASE WHEN ? THEN ended_at ELSE ? END,
                        result_json = COALESCE(?, result_json),
                        error_json = COALESCE(?, error_json)
                    WHERE id=?
                    """,
                    (
                        status,
                        is_start, now,
                        _dumps_utf8_safe(result) if result else None,
                        _dumps_utf8_safe(error) if error else None,
                        existing["id"],
                    ),
                )

    def list_target_runs(self, job_id: str) -> list[dict[str, Any]]:
        rows = self._connect().execute(
            "SELECT * FROM target_runs WHERE job_id=? ORDER BY id ASC", (job_id,),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "job_id": r["job_id"],
                "target_id": r["target_id"],
                "status": r["status"],
                "started_at": r["started_at"],
                "ended_at": r["ended_at"],
                "required_resources":
                    json.loads(r["required_resources_json"]) if r["required_resources_json"] else [],
                "bindings": json.loads(r["bindings_json"]) if r["bindings_json"] else {},
                "parameters": json.loads(r["parameters_json"]) if r["parameters_json"] else {},
                "result": json.loads(r["result_json"]) if r["result_json"] else None,
                "error": json.loads(r["error_json"]) if r["error_json"] else None,
            }
            for r in rows
        ]

    # --- measurement_cache ---

    def upsert_measurement_cache(
        self,
        instrument: str,
        measurement: str,
        value: Any,
        unit: str = "",
        source_job_id: str = "",
    ) -> None:
        """測定値キャッシュを上書き (主キー: instrument + measurement)"""
        now = _now_iso()
        with self._write_lock:
            self._connect().execute(
                """
                INSERT INTO measurement_cache
                (instrument, measurement, value_json, unit, timestamp, source_job_id)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(instrument, measurement) DO UPDATE SET
                  value_json = excluded.value_json,
                  unit = excluded.unit,
                  timestamp = excluded.timestamp,
                  source_job_id = excluded.source_job_id
                """,
                (
                    instrument, measurement,
                    json.dumps(value, ensure_ascii=False, default=str),
                    unit, now, source_job_id,
                ),
            )

    def get_measurement_cache(
        self, instrument: str, measurement: str,
    ) -> dict[str, Any] | None:
        row = self._connect().execute(
            "SELECT * FROM measurement_cache WHERE instrument=? AND measurement=?",
            (instrument, measurement),
        ).fetchone()
        if row is None:
            return None
        return {
            "instrument": row["instrument"],
            "measurement": row["measurement"],
            "value": json.loads(row["value_json"]),
            "unit": row["unit"] or "",
            "timestamp": row["timestamp"],
            "source_job_id": row["source_job_id"] or "",
        }

    # --- monitor_data ---

    def append_monitor_data(
        self,
        monitor_id: str,
        instrument: str,
        value: Any,
        sample_count: int | None = None,
    ) -> None:
        """monitor 出力を時系列追加"""
        now = _now_iso()
        with self._write_lock:
            self._connect().execute(
                """
                INSERT INTO monitor_data
                (monitor_id, instrument, timestamp, value_json, sample_count)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    monitor_id, instrument, now,
                    json.dumps(value, ensure_ascii=False, default=str),
                    sample_count,
                ),
            )

    def list_monitor_data(
        self,
        monitor_id: str,
        limit: int = 1000,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        rows = self._connect().execute(
            """
            SELECT * FROM monitor_data WHERE monitor_id=?
            ORDER BY id ASC LIMIT ? OFFSET ?
            """,
            (monitor_id, int(limit), int(offset)),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "monitor_id": r["monitor_id"],
                "instrument": r["instrument"],
                "timestamp": r["timestamp"],
                "value": json.loads(r["value_json"]),
                "sample_count": r["sample_count"],
            }
            for r in rows
        ]

    def count_monitor_data(self, monitor_id: str) -> int:
        row = self._connect().execute(
            "SELECT COUNT(*) AS n FROM monitor_data WHERE monitor_id=?",
            (monitor_id,),
        ).fetchone()
        return int(row["n"]) if row else 0

    # --- v0.7.0.1: monitor_data prune / delete ---

    def delete_monitor_data(self, monitor_id: str) -> int:
        """指定 monitor_id の全 monitor_data を削除。返り値 = 削除行数"""
        with self._write_lock:
            cur = self._connect().execute(
                "DELETE FROM monitor_data WHERE monitor_id=?", (monitor_id,),
            )
            return cur.rowcount or 0

    def prune_monitor_data(self, older_than_days: float) -> int:
        """older_than_days 日より古い monitor_data 行を削除。返り値 = 削除行数

        timestamp は ISO8601 文字列で保存されているため、SQLite 上で datetime() 比較。
        """
        from datetime import datetime, timezone, timedelta
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=float(older_than_days)))
        cutoff_iso = cutoff.isoformat(timespec="seconds")
        with self._write_lock:
            cur = self._connect().execute(
                "DELETE FROM monitor_data WHERE timestamp < ?", (cutoff_iso,),
            )
            return cur.rowcount or 0

    def total_monitor_data_count(self) -> int:
        """全 monitor_data の総行数 (運用監視用)"""
        row = self._connect().execute(
            "SELECT COUNT(*) AS n FROM monitor_data",
        ).fetchone()
        return int(row["n"]) if row else 0

    # =====================================================================
    # v0.8.0: experiment_plans / experiment_templates
    # =====================================================================

    def save_experiment_plan(
        self,
        plan_id: str,
        *,
        job_id: str | None,
        name: str,
        dsl_version: str,
        original_plan: dict[str, Any],
        compiled_summary: dict[str, Any] | None = None,
        validation_result: dict[str, Any] | None = None,
    ) -> None:
        """DSL plan を永続化 (v0.8.0)。"""
        now = _now_iso()
        with self._write_lock:
            self._connect().execute(
                """
                INSERT INTO experiment_plans
                (plan_id, job_id, name, dsl_version,
                 original_plan_json, compiled_summary_json, validation_result_json,
                 created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan_id, job_id, name, dsl_version,
                    json.dumps(original_plan, ensure_ascii=False, default=str),
                    json.dumps(compiled_summary, ensure_ascii=False, default=str)
                        if compiled_summary else None,
                    json.dumps(validation_result, ensure_ascii=False, default=str)
                        if validation_result else None,
                    now,
                ),
            )

    def get_experiment_plan(self, plan_id: str) -> dict[str, Any] | None:
        row = self._connect().execute(
            "SELECT * FROM experiment_plans WHERE plan_id=?", (plan_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "plan_id": row["plan_id"],
            "job_id": row["job_id"],
            "name": row["name"],
            "dsl_version": row["dsl_version"],
            "original_plan": json.loads(row["original_plan_json"]),
            "compiled_summary": (
                json.loads(row["compiled_summary_json"])
                if row["compiled_summary_json"] else None
            ),
            "validation_result": (
                json.loads(row["validation_result_json"])
                if row["validation_result_json"] else None
            ),
            "created_at": row["created_at"],
        }

    def get_experiment_plan_for_job(self, job_id: str) -> dict[str, Any] | None:
        row = self._connect().execute(
            "SELECT * FROM experiment_plans WHERE job_id=? "
            "ORDER BY created_at DESC LIMIT 1",
            (job_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "plan_id": row["plan_id"],
            "job_id": row["job_id"],
            "name": row["name"],
            "dsl_version": row["dsl_version"],
            "original_plan": json.loads(row["original_plan_json"]),
            "compiled_summary": (
                json.loads(row["compiled_summary_json"])
                if row["compiled_summary_json"] else None
            ),
            "validation_result": (
                json.loads(row["validation_result_json"])
                if row["validation_result_json"] else None
            ),
            "created_at": row["created_at"],
        }

    def save_experiment_template(
        self,
        name: str,
        dsl_version: str,
        plan: dict[str, Any],
        description: str = "",
    ) -> None:
        """experiment_templates UPSERT"""
        now = _now_iso()
        with self._write_lock:
            self._connect().execute(
                """
                INSERT INTO experiment_templates
                (name, dsl_version, plan_json, description, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                  dsl_version = excluded.dsl_version,
                  plan_json   = excluded.plan_json,
                  description = excluded.description,
                  updated_at  = excluded.updated_at
                """,
                (
                    name, dsl_version,
                    json.dumps(plan, ensure_ascii=False, default=str),
                    description, now, now,
                ),
            )

    def get_experiment_template(self, name: str) -> dict[str, Any] | None:
        row = self._connect().execute(
            "SELECT * FROM experiment_templates WHERE name=?", (name,),
        ).fetchone()
        if row is None:
            return None
        return {
            "name": row["name"],
            "dsl_version": row["dsl_version"],
            "plan": json.loads(row["plan_json"]),
            "description": row["description"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_experiment_templates(self) -> list[dict[str, Any]]:
        rows = self._connect().execute(
            "SELECT name, dsl_version, description, created_at, updated_at "
            "FROM experiment_templates ORDER BY updated_at DESC"
        ).fetchall()
        return [
            {
                "name": r["name"],
                "dsl_version": r["dsl_version"],
                "description": r["description"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]

    # v2.14.3: duplicate `close()` definition removed.
    # The authoritative `close()` + `__enter__` / `__exit__` are at
    # line ~318 of this file.
