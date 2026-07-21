"""v0.7.0: Persistence + self-awareness + verify テスト

実装方針必須 3 件:
- test_schema_migration_from_v050
- test_verify_numeric_mismatch_strict_fails_step
- test_job_events_record_step_and_target
"""
import asyncio
import sqlite3
import textwrap
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from lab_executor.experiment_ir import CommandStep, Plan
from lab_executor.group import FailurePolicy, TargetExecution
from lab_executor.group.executor import GroupExecutor
from lab_executor.job import CancelMode, JobManager, JobStore
from lab_executor.job.state_machine import JobStatus, is_terminal
from lab_executor.models.instrument_def import InstrumentDefinition
from lab_visa_mcp.session_manager import InstrumentSession
from lab_executor.state_query import query_all_state, query_state_item
from lab_executor.system_config import SystemConfig, InstrumentBinding


YAML_PSU_WITH_VERIFY = """
metadata: { manufacturer: T, model: PSU, category: power_supply }
commands:
  set_voltage:
    scpi: "VOLT {voltage}"
    type: write
    parameters:
      - { name: voltage, type: float, range: [0, 100] }
    verify:
      readback_command: measure_voltage
      tolerance: 0.05
      retry: 0
  measure_voltage:
    scpi: "MEAS:VOLT?"
    type: query
    polling_safe: true
  set_output:
    scpi: "OUTP {state}"
    type: write
    parameters:
      - { name: state, type: enum, choices: ["ON", "OFF"] }
  query_output:
    scpi: "OUTP?"
    type: query
    polling_safe: true
state_query:
  voltage:
    command: measure_voltage
    unit: V
  output:
    command: query_output
    unit: ""
    map:
      "1": "ON"
      "0": "OFF"
recipes:
  set_5v:
    parameters: []
    steps:
      - { command: set_voltage, args: { voltage: 5.0 } }
"""


def _psu_session(resource="psu0"):
    d = InstrumentDefinition(**yaml.safe_load(YAML_PSU_WITH_VERIFY))
    return InstrumentSession(
        resource_name=resource, idn_response="<x>",
        idn_parsed={}, definition=d,
    )


# =========================================================
# Schema migration
# =========================================================


def test_schema_migration_from_v050(tmp_path):
    """**必須**: v0.5.x の jobs テーブルのみの DB を v0.7.0 起動で migration
    する。既存 jobs データを保持しつつ、新規テーブルが追加される。
    """
    db_path = tmp_path / "old_v050.sqlite"
    # 旧 v0.5.x スキーマを手動作成 (PRAGMA user_version=0)
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE jobs (
            job_id TEXT PRIMARY KEY,
            owner TEXT NOT NULL DEFAULT '',
            resource_name TEXT NOT NULL DEFAULT '',
            recipe TEXT NOT NULL DEFAULT '',
            parameters_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL,
            current_step_index INTEGER NOT NULL DEFAULT -1,
            error_class TEXT NOT NULL DEFAULT '',
            last_step_summary TEXT NOT NULL DEFAULT '',
            result_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
    """)
    conn.execute(
        "INSERT INTO jobs (job_id, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?)",
        ("old_job_001", "completed", "2026-01-01T00:00:00+00:00",
         "2026-01-01T00:00:01+00:00"),
    )
    conn.execute("PRAGMA user_version = 0")
    conn.commit()
    conn.close()

    # v0.7.0 起動 (migration が走る)
    store = JobStore(db_path=db_path)
    try:
        # 旧 job が残っている
        rec = store.get("old_job_001")
        assert rec is not None
        assert rec.status == JobStatus.COMPLETED
        # 新規テーブルが作成されている
        conn = store._connect()
        for table in (
            "job_steps", "target_runs", "job_events",
            "measurement_cache", "monitor_data",
        ):
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            assert row is not None, f"テーブル {table} が作成されていない"
        # user_version が 1 になっている
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
        # v0.7.0 で user_version=1、v0.8.0 で user_version=2 へ migration
        assert ver >= 1
    finally:
        store.close()


def test_schema_init_fresh_db(tmp_path):
    """新規 DB でも v0.7.0 schema が初回起動で揃う"""
    store = JobStore(db_path=tmp_path / "new.sqlite")
    try:
        conn = store._connect()
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
        # v0.7.0 で user_version=1、v0.8.0 で user_version=2 へ migration
        assert ver >= 1
        # 全テーブル存在
        for table in (
            "jobs", "job_steps", "target_runs", "job_events",
            "measurement_cache", "monitor_data",
        ):
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            assert row is not None
    finally:
        store.close()


# =========================================================
# job_events / job_steps / target_runs
# =========================================================


@pytest.mark.asyncio
async def test_job_events_record_step_and_target(tmp_path, monkeypatch):
    """**必須**: Job 実行で job_events に step_started / step_completed が記録される"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    visa = MagicMock()
    visa.write = AsyncMock(return_value=None)
    visa.query = AsyncMock(return_value="5.0")

    session = _psu_session("psu0")

    class _SM:
        def get_session(self, name):
            return session if name == "psu0" else None

    store = JobStore(db_path=tmp_path / "j.sqlite")
    mgr = JobManager(visa, _SM(), store=store)
    try:
        rec = await mgr.start_recipe_job("psu0", "set_5v", {})
        for _ in range(40):
            if is_terminal(mgr.get(rec.job_id).status): break
            await asyncio.sleep(0.05)
        final = mgr.get(rec.job_id)
        assert final.status == JobStatus.COMPLETED

        events = store.list_events(rec.job_id, limit=50)
        types = [e["event_type"] for e in events]
        assert "job_started" in types
        assert "step_started" in types
        assert "step_completed" in types

        # job_steps テーブル
        steps = store.list_steps(rec.job_id)
        assert len(steps) >= 1
        assert steps[0]["status"] == "ok"
        assert steps[0]["step_type"] == "command"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_target_runs_recorded_for_map_job(tmp_path, monkeypatch):
    """Map Job で target_runs にレコードが入る"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    visa = MagicMock()
    visa.write = AsyncMock(return_value=None)
    visa.query = AsyncMock(return_value="5.0")

    d = InstrumentDefinition(**yaml.safe_load(YAML_PSU_WITH_VERIFY))
    sessions = {
        f"psu{i}": InstrumentSession(
            resource_name=f"psu{i}", idn_response="<x>",
            idn_parsed={}, definition=d,
        )
        for i in range(2)
    }

    class _SM:
        def get_session(self, name): return sessions.get(name)

    sys_cfg = SystemConfig(
        instruments={
            f"a{i}": InstrumentBinding(resource=f"psu{i}") for i in range(2)
        },
    )
    from lab_executor.system_config import ExperimentUnit
    sys_cfg.experiment_units = {
        f"u{i}": ExperimentUnit(bindings={"psu": f"a{i}"}) for i in range(2)
    }
    store = JobStore(db_path=tmp_path / "j.sqlite")
    mgr = JobManager(visa, _SM(), store=store, system_config=sys_cfg)
    try:
        rec = await mgr.start_map_recipe_job(
            "set_5v",
            [
                {"target_id": "s1", "unit": "u0"},
                {"target_id": "s2", "unit": "u1"},
            ],
            primary_role="psu",
        )
        for _ in range(40):
            if is_terminal(mgr.get(rec.job_id).status): break
            await asyncio.sleep(0.05)
        final = mgr.get(rec.job_id)
        assert final.status == JobStatus.COMPLETED, final.last_step_summary

        target_runs = store.list_target_runs(rec.job_id)
        assert len(target_runs) == 2
        statuses = {t["target_id"]: t["status"] for t in target_runs}
        assert statuses == {"s1": "ok", "s2": "ok"}

        # target_started / target_completed event
        events = store.list_events(rec.job_id, limit=100)
        types = [e["event_type"] for e in events]
        assert "target_started" in types
        assert "target_completed" in types
    finally:
        store.close()


# =========================================================
# verify (write 後 read-back)
# =========================================================


@pytest.mark.asyncio
async def test_verify_numeric_success(tmp_path, monkeypatch):
    """write → readback で値が一致 → verified=True"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    from lab_executor.step_executor import execute_command_step

    visa = MagicMock()
    visa.write = AsyncMock(return_value=None)
    visa.query = AsyncMock(return_value="5.001")  # tolerance 0.05 以内

    session = _psu_session("psu0")
    step = CommandStep(command="set_voltage", args={"voltage": 5.0})
    res = await execute_command_step(
        visa, session, step, override_safety=False, override_reason="",
    )
    assert res["success"] is True
    assert res.get("verified") is True
    assert res["verify"]["status"] == "ok"
    assert res["verify"]["actual"] == 5.001


@pytest.mark.asyncio
async def test_verify_numeric_mismatch_strict_fails_step(tmp_path, monkeypatch):
    """**必須**: strict mode + verify 失敗で step が failed"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "strict")
    from lab_executor.step_executor import execute_command_step

    visa = MagicMock()
    visa.write = AsyncMock(return_value=None)
    visa.query = AsyncMock(return_value="4.0")  # 5.0 とは差 1.0 (tolerance 0.05 を大幅超え)

    session = _psu_session("psu0")
    step = CommandStep(command="set_voltage", args={"voltage": 5.0})
    res = await execute_command_step(
        visa, session, step, override_safety=False, override_reason="",
    )
    assert res["success"] is False
    assert res.get("verified") is False
    assert res["error"] == "VerifyMismatch"
    assert res["verify"]["status"] == "mismatch"
    assert res["verify"]["expected"] == 5.0
    assert res["verify"]["actual"] == 4.0


@pytest.mark.asyncio
async def test_verify_numeric_mismatch_advisory_warns_only(tmp_path, monkeypatch):
    """advisory mode では verify 失敗でも step success (verified=False のみ)"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "advisory")
    from lab_executor.step_executor import execute_command_step

    visa = MagicMock()
    visa.write = AsyncMock(return_value=None)
    visa.query = AsyncMock(return_value="4.0")

    session = _psu_session("psu0")
    step = CommandStep(command="set_voltage", args={"voltage": 5.0})
    res = await execute_command_step(
        visa, session, step, override_safety=False, override_reason="",
    )
    # advisory: step success だが verified=False
    assert res["success"] is True
    assert res.get("verified") is False
    assert res["verify"]["status"] == "mismatch"


# =========================================================
# state_query
# =========================================================


@pytest.mark.asyncio
async def test_query_state_item_basic():
    visa = MagicMock()
    visa.query = AsyncMock(return_value="5.0")

    session = _psu_session("psu0")
    item = session.definition.state_query["voltage"]
    r = await query_state_item(visa, session, "voltage", item)
    assert r["key"] == "voltage"
    assert r["value"] == 5.0
    assert r["unit"] == "V"


@pytest.mark.asyncio
async def test_query_state_item_with_map():
    visa = MagicMock()
    visa.query = AsyncMock(return_value="1")

    session = _psu_session("psu0")
    item = session.definition.state_query["output"]
    r = await query_state_item(visa, session, "output", item)
    assert r["value"] == "ON"   # map で変換


@pytest.mark.asyncio
async def test_query_all_state():
    visa = MagicMock()

    counter = {"i": 0}

    async def q(*a, **kw):
        counter["i"] += 1
        return "5.0" if counter["i"] == 1 else "1"

    visa.query = q
    session = _psu_session("psu0")
    r = await query_all_state(visa, session)
    assert "voltage" in r
    assert "output" in r


# =========================================================
# measurement_cache
# =========================================================


def test_measurement_cache_upsert_and_get(tmp_path):
    store = JobStore(db_path=tmp_path / "j.sqlite")
    try:
        store.upsert_measurement_cache("psu0", "voltage", 5.0, unit="V")
        cached = store.get_measurement_cache("psu0", "voltage")
        assert cached is not None
        assert cached["value"] == 5.0
        assert cached["unit"] == "V"

        # 上書き
        store.upsert_measurement_cache("psu0", "voltage", 5.001, unit="V")
        cached2 = store.get_measurement_cache("psu0", "voltage")
        assert cached2["value"] == 5.001
    finally:
        store.close()


# =========================================================
# monitor
# =========================================================


@pytest.mark.asyncio
async def test_monitor_records_data(tmp_path, monkeypatch):
    """start_monitor が monitor_data テーブルに値を蓄積"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    visa = MagicMock()
    counter = {"i": 0}

    async def q(*a, **kw):
        counter["i"] += 1
        return f"{20 + counter['i']}"

    visa.query = q

    session = _psu_session("psu0")

    class _SM:
        def get_session(self, name):
            return session if name == "psu0" else None

    store = JobStore(db_path=tmp_path / "j.sqlite")
    mgr = JobManager(visa, _SM(), store=store)
    try:
        rec = await mgr.start_monitor_job(
            "psu0", "measure_voltage",
            interval_s=1.0,         # 最小制限
            duration_s=2.5,         # 約 2-3 sample
        )
        for _ in range(50):
            if is_terminal(mgr.get(rec.job_id).status): break
            await asyncio.sleep(0.1)
        final = mgr.get(rec.job_id)
        assert final.status == JobStatus.COMPLETED

        # monitor_data テーブルに値がある
        total = store.count_monitor_data(rec.job_id)
        assert total >= 1
        data = store.list_monitor_data(rec.job_id, limit=10)
        assert len(data) == total
        assert data[0]["instrument"] == "psu0"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_monitor_stop_condition(tmp_path, monkeypatch):
    """stop_condition_expr が True になったら早期終了"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    visa = MagicMock()
    counter = {"i": 0}

    async def q(*a, **kw):
        counter["i"] += 1
        return "100"   # 即 stop_condition 成立

    visa.query = q
    session = _psu_session("psu0")

    class _SM:
        def get_session(self, name): return session if name == "psu0" else None

    store = JobStore(db_path=tmp_path / "j.sqlite")
    mgr = JobManager(visa, _SM(), store=store)
    try:
        t0 = time.monotonic()
        rec = await mgr.start_monitor_job(
            "psu0", "measure_voltage",
            interval_s=1.0, duration_s=60.0,    # duration 大
            stop_condition_expr="value > 50",
        )
        for _ in range(50):
            if is_terminal(mgr.get(rec.job_id).status): break
            await asyncio.sleep(0.1)
        elapsed = time.monotonic() - t0
        final = mgr.get(rec.job_id)
        assert final.status == JobStatus.COMPLETED
        assert final.result["stopped_by_condition"] is True
        assert elapsed < 5.0, f"stop_condition で早期停止していない: {elapsed}s"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_monitor_cancel(tmp_path, monkeypatch):
    """cancel_job で monitor を停止"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    visa = MagicMock()
    visa.query = AsyncMock(return_value="5.0")
    session = _psu_session("psu0")

    class _SM:
        def get_session(self, name): return session if name == "psu0" else None

    store = JobStore(db_path=tmp_path / "j.sqlite")
    mgr = JobManager(visa, _SM(), store=store)
    try:
        rec = await mgr.start_monitor_job(
            "psu0", "measure_voltage",
            interval_s=1.0, duration_s=60.0,
        )
        await asyncio.sleep(1.5)
        await mgr.cancel(rec.job_id, CancelMode.AFTER_CURRENT_STEP, timeout_s=5.0)
        final = mgr.get(rec.job_id)
        assert final.status == JobStatus.CANCELLED
    finally:
        store.close()


@pytest.mark.asyncio
async def test_monitor_interval_validation(tmp_path):
    """interval_s < 1.0 は validation error"""
    visa = MagicMock()
    class _SM:
        def get_session(self, name): return None
    store = JobStore(db_path=tmp_path / "j.sqlite")
    mgr = JobManager(visa, _SM(), store=store)
    try:
        rec = await mgr.start_monitor_job(
            "psu0", "measure_voltage", interval_s=0.5, duration_s=10.0,
        )
        assert rec.status == JobStatus.FAILED
        assert rec.error_class == "validation"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_monitor_duration_validation(tmp_path):
    """duration_s > 24h は validation error"""
    visa = MagicMock()
    class _SM:
        def get_session(self, name): return None
    store = JobStore(db_path=tmp_path / "j.sqlite")
    mgr = JobManager(visa, _SM(), store=store)
    try:
        rec = await mgr.start_monitor_job(
            "psu0", "measure_voltage", interval_s=1.0, duration_s=100_000,
        )
        assert rec.status == JobStatus.FAILED
        assert rec.error_class == "validation"
    finally:
        store.close()
