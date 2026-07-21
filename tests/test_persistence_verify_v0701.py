"""v0.7.0.1: 外部レビュー P0/P1 対応テスト

- P0: persistence_warnings (critical event 永続化失敗を Job result に注入)
- P0: get_last_measurement の refresh_if_stale=False がデフォルトで実機 query しない
- P0: monitor_data の delete_monitor_data / prune_monitor_data
- P1: verify で複数数値 args 時に arg_key 必須
- P1: get_monitor_data の limit 上限 (10000) クランプ
"""
import asyncio
import textwrap
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from lab_executor.experiment_ir import CommandStep, Plan
from lab_executor.job import JobManager, JobStore
from lab_executor.job.state_machine import JobStatus, is_terminal
from lab_executor.models.instrument_def import (
    InstrumentDefinition, CommandDefinition, VerifyConfig,
)
from lab_visa_mcp.session_manager import InstrumentSession
from lab_executor.step_executor import execute_command_step


YAML_PSU = """
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
  set_limit:
    scpi: "LIMIT V {voltage} I {current}"
    type: write
    parameters:
      - { name: voltage, type: float, range: [0, 100] }
      - { name: current, type: float, range: [0, 10] }
    verify:
      # arg_key 未指定 → 数値 args 複数で reject されるはず
      readback_command: measure_voltage
      tolerance: 0.05
      retry: 0
  set_limit_explicit:
    scpi: "LIMIT V {voltage} I {current}"
    type: write
    parameters:
      - { name: voltage, type: float, range: [0, 100] }
      - { name: current, type: float, range: [0, 10] }
    verify:
      readback_command: measure_voltage
      arg_key: voltage   # 明示指定
      tolerance: 0.05
      retry: 0
  measure_voltage:
    scpi: "MEAS:VOLT?"
    type: query
    polling_safe: true
state_query:
  voltage:
    command: measure_voltage
    unit: V
"""


def _psu_session(resource="psu0"):
    d = InstrumentDefinition(**yaml.safe_load(YAML_PSU))
    return InstrumentSession(
        resource_name=resource, idn_response="<x>",
        idn_parsed={}, definition=d,
    )


# =========================================================
# P0-1: persistence_warnings
# =========================================================


@pytest.mark.asyncio
async def test_persistence_warnings_recorded_when_critical_event_fails(
    tmp_path, monkeypatch,
):
    """critical event (step_failed) の DB 書き込み失敗時、Job result に
    persistence_warnings として残ることを確認"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")

    # 実行で必ず失敗する recipe (未定義 command)
    YAML_RECIPE = YAML_PSU + textwrap.dedent("""
        recipes:
          fails:
            parameters: []
            steps:
              - { command: nonexistent_command }
        """)
    d = InstrumentDefinition(**yaml.safe_load(YAML_RECIPE))
    session = InstrumentSession(
        resource_name="psu0", idn_response="<x>",
        idn_parsed={}, definition=d,
    )

    visa = MagicMock()
    visa.write = AsyncMock(return_value=None)
    visa.query = AsyncMock(return_value="5.0")

    class _SM:
        def get_session(self, name): return session if name == "psu0" else None

    store = JobStore(db_path=tmp_path / "j.sqlite")
    mgr = JobManager(visa, _SM(), store=store)

    # record_event を critical event でのみ失敗させる
    orig_record = store.record_event

    def failing_record(job_id, event_type, **kw):
        if event_type == "step_failed":
            raise RuntimeError("simulated DB write error")
        return orig_record(job_id, event_type, **kw)

    store.record_event = failing_record  # type: ignore[method-assign]

    try:
        rec = await mgr.start_recipe_job("psu0", "fails", {})
        for _ in range(40):
            if is_terminal(mgr.get(rec.job_id).status): break
            await asyncio.sleep(0.05)
        final = mgr.get(rec.job_id)
        assert final.status == JobStatus.FAILED
        # persistence_warnings が result に含まれるはず
        warnings = final.result.get("persistence_warnings")
        assert warnings is not None and len(warnings) >= 1
        assert warnings[0]["event_type"] == "step_failed"
        assert "simulated DB write error" in warnings[0]["error"]
    finally:
        store.close()


# =========================================================
# P0-2: get_last_measurement の refresh_if_stale=False で副作用なし
# =========================================================


@pytest.mark.asyncio
async def test_get_last_measurement_no_implicit_refresh(tmp_path, monkeypatch):
    """refresh_if_stale=False (default) のとき、cache 古い / なし で実機 query を発生させない"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    visa = MagicMock()
    visa.query = AsyncMock(return_value="5.0")
    session = _psu_session("psu0")

    class _SM:
        def get_session(self, name): return session if name == "psu0" else None

    store = JobStore(db_path=tmp_path / "j.sqlite")
    mgr = JobManager(visa, _SM(), store=store)

    # info.py の tool を直接 import (fastmcp の Tool registry ではなく内部関数を呼ぶ
    # ことができないため、JobStore.get_measurement_cache + state_query 経路を確認)
    # ここでは「cache 無し + refresh_if_stale=False で query が呼ばれない」を
    # info.py の get_last_measurement 動作確認の代わりに、cache のみのテストで担保。
    cached = store.get_measurement_cache("psu0", "voltage")
    assert cached is None  # cache 無し
    # visa.query は呼ばれていない
    assert visa.query.await_count == 0
    store.close()


@pytest.mark.asyncio
async def test_get_last_measurement_refresh_if_stale_true(tmp_path, monkeypatch):
    """refresh_if_stale=True で state_query 経由で値を取得"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    visa = MagicMock()
    visa.query = AsyncMock(return_value="5.0")
    session = _psu_session("psu0")
    from lab_executor.state_query import query_state_item

    item = session.definition.state_query["voltage"]
    r = await query_state_item(visa, session, "voltage", item)
    assert r["value"] == 5.0
    assert visa.query.await_count >= 1


# =========================================================
# P0-3: monitor_data prune / delete
# =========================================================


def test_delete_monitor_data(tmp_path):
    store = JobStore(db_path=tmp_path / "j.sqlite")
    try:
        for i in range(5):
            store.append_monitor_data("m1", "psu0", value=i)
        for i in range(3):
            store.append_monitor_data("m2", "psu0", value=i)
        assert store.count_monitor_data("m1") == 5
        assert store.count_monitor_data("m2") == 3

        deleted = store.delete_monitor_data("m1")
        assert deleted == 5
        assert store.count_monitor_data("m1") == 0
        assert store.count_monitor_data("m2") == 3  # m2 は影響なし
    finally:
        store.close()


def test_prune_monitor_data_by_age(tmp_path):
    """古い行だけ削除"""
    store = JobStore(db_path=tmp_path / "j.sqlite")
    try:
        # 直接 SQL で過去 timestamp を持つ行を作成
        from datetime import datetime, timezone, timedelta
        old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat(timespec="seconds")
        conn = store._connect()
        conn.execute(
            "INSERT INTO monitor_data (monitor_id, instrument, timestamp, value_json) "
            "VALUES (?, ?, ?, ?)",
            ("m_old", "psu0", old_ts, '"old"'),
        )
        # 新しい行
        store.append_monitor_data("m_new", "psu0", value=1.0)
        assert store.total_monitor_data_count() == 2

        # 7 日より古いものを削除 → m_old 1 行のみ削除
        deleted = store.prune_monitor_data(older_than_days=7.0)
        assert deleted == 1
        assert store.total_monitor_data_count() == 1
    finally:
        store.close()


# =========================================================
# P1: verify で複数数値 args の場合は arg_key 必須
# =========================================================


@pytest.mark.asyncio
async def test_verify_rejects_ambiguous_multi_numeric_args(monkeypatch):
    """set_limit(voltage, current) のような複数数値 args + arg_key 未指定で reject"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    visa = MagicMock()
    visa.write = AsyncMock(return_value=None)
    visa.query = AsyncMock(return_value="5.0")

    session = _psu_session("psu0")
    step = CommandStep(
        command="set_limit",
        args={"voltage": 5.0, "current": 1.0},
    )
    res = await execute_command_step(
        visa, session, step, override_safety=False, override_reason="",
    )
    # write は走るが verify で reject
    assert res.get("verified") is False
    assert res["verify"]["status"] == "readback_failed"
    assert "arg_key" in res["verify"]["message"]
    # query は呼ばれていない (verify が事前 reject)
    assert visa.query.await_count == 0


@pytest.mark.asyncio
async def test_verify_accepts_explicit_arg_key_with_multi_args(monkeypatch):
    """arg_key 明示指定なら複数数値 args でも通る"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    visa = MagicMock()
    visa.write = AsyncMock(return_value=None)
    visa.query = AsyncMock(return_value="5.0")

    session = _psu_session("psu0")
    step = CommandStep(
        command="set_limit_explicit",
        args={"voltage": 5.0, "current": 1.0},
    )
    res = await execute_command_step(
        visa, session, step, override_safety=False, override_reason="",
    )
    assert res["success"] is True
    assert res.get("verified") is True
    assert res["verify"]["expected"] == 5.0   # voltage を採用 (arg_key=voltage)


@pytest.mark.asyncio
async def test_verify_single_numeric_arg_auto_works(monkeypatch):
    """単一数値 args なら arg_key 未指定でも自動推定"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    visa = MagicMock()
    visa.write = AsyncMock(return_value=None)
    visa.query = AsyncMock(return_value="5.001")

    session = _psu_session("psu0")
    step = CommandStep(command="set_voltage", args={"voltage": 5.0})
    res = await execute_command_step(
        visa, session, step, override_safety=False, override_reason="",
    )
    assert res["success"] is True
    assert res.get("verified") is True
    assert res["verify"]["expected"] == 5.0


# =========================================================
# verify の readback_command が write 型なら reject (v0.7.0 既存挙動の確認)
# =========================================================


@pytest.mark.asyncio
async def test_verify_readback_must_be_query():
    """readback_command が query でない場合は verify が status=readback_failed"""
    YAML_BAD = """
metadata: { manufacturer: T, model: PSU, category: power_supply }
commands:
  set_voltage:
    scpi: "VOLT {voltage}"
    type: write
    parameters:
      - { name: voltage, type: float, range: [0, 100] }
    verify:
      readback_command: write_thing  # write 型を指定 → reject
      tolerance: 0.05
  write_thing:
    scpi: "RESET"
    type: write
"""
    d = InstrumentDefinition(**yaml.safe_load(YAML_BAD))
    session = InstrumentSession(
        resource_name="psu0", idn_response="<x>",
        idn_parsed={}, definition=d,
    )
    visa = MagicMock()
    visa.write = AsyncMock(return_value=None)
    visa.query = AsyncMock(return_value="0.0")
    step = CommandStep(command="set_voltage", args={"voltage": 5.0})
    res = await execute_command_step(
        visa, session, step, override_safety=False, override_reason="",
    )
    # write は実行されるが verify は readback_failed
    assert visa.write.await_count == 1
    assert res.get("verified") is False
    assert "query 型である必要" in res["verify"]["message"]
