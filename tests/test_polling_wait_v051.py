"""v0.5.1: Polling wait (wait_for_condition / wait_for_stable / wait_until) テスト"""
import asyncio
import textwrap
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from lab_executor.experiment_ir import (
    WaitForConditionStep, WaitForStableStep, WaitUntilStep, WaitStep,
)
from lab_executor.job import CancelMode, JobManager, JobStore
from lab_executor.job.state_machine import JobStatus, is_terminal
from lab_executor.models.instrument_def import InstrumentDefinition
from lab_executor.polling_executor import (
    execute_wait_for_condition,
    execute_wait_for_stable,
    execute_wait_until,
    extract_value,
)
from lab_visa_mcp.session_manager import InstrumentSession
from lab_executor.utils.condition import safe_eval_condition, ConditionError


# === safe_eval_condition ===

def test_condition_basic_comparisons():
    assert safe_eval_condition("value > 80", {"value": 90}) is True
    assert safe_eval_condition("value > 80", {"value": 50}) is False
    assert safe_eval_condition("value <= 0.5", {"value": 0.3}) is True
    assert safe_eval_condition("value == 5", {"value": 5}) is True
    assert safe_eval_condition("value != 5", {"value": 6}) is True


def test_condition_abs():
    assert safe_eval_condition("abs(value - 25) < 0.2", {"value": 25.1}) is True
    assert safe_eval_condition("abs(value - 25) < 0.2", {"value": 25.5}) is False


def test_condition_and_or():
    assert safe_eval_condition("value > 10 and value < 20", {"value": 15}) is True
    assert safe_eval_condition("value < 10 or value > 20", {"value": 25}) is True
    assert safe_eval_condition("value > 10 and value < 20", {"value": 25}) is False


def test_condition_forbids_function_call():
    with pytest.raises(ConditionError):
        safe_eval_condition("__import__('os')", {"value": 0})
    with pytest.raises(ConditionError):
        safe_eval_condition("min(value, 5)", {"value": 0})


def test_condition_forbids_attribute_access():
    with pytest.raises(ConditionError):
        safe_eval_condition("value.bit_length", {"value": 5})


def test_condition_forbids_undefined_var():
    with pytest.raises(ConditionError):
        safe_eval_condition("unknown > 5", {"value": 1})


# === extract_value ===

def test_extract_value_path_priority():
    parsed = {"temperature": "25.3", "status": "OK"}
    assert extract_value("raw", parsed, "temperature") == 25.3


def test_extract_value_key_priority():
    parsed = {"value": "27.1", "unit": "C"}
    assert extract_value("raw", parsed, None) == 27.1


def test_extract_value_single_numeric():
    parsed = {"temperature": "25.3", "status": "OK"}  # 数値 1 個
    assert extract_value("raw", parsed, None) == 25.3


def test_extract_value_raw_float():
    assert extract_value("3.14", None, None) == 3.14
    assert extract_value("  -2.5\n", None, None) == -2.5


def test_extract_value_unparseable():
    assert extract_value("not a number", None, None) is None
    assert extract_value("?", {"a": "x", "b": "y"}, None) is None


# === IR validation ===

def test_wait_for_stable_step_validates_window_le_timeout():
    with pytest.raises(Exception):
        WaitForStableStep(
            instrument="t1", command="m",
            tolerance=0.2, window_s=100.0, timeout_s=60.0,
            interval_s=1.0,
        )


def test_wait_for_stable_step_validates_interval_le_window():
    with pytest.raises(Exception):
        WaitForStableStep(
            instrument="t1", command="m",
            tolerance=0.2, window_s=10.0, timeout_s=60.0,
            interval_s=15.0,
        )


def test_wait_for_stable_step_validates_min_samples():
    # window=60, interval=120 → 1 sample 取れず、min_samples=3 を満たせない
    with pytest.raises(Exception):
        WaitForStableStep(
            instrument="t1", command="m",
            tolerance=0.2, window_s=60.0, timeout_s=120.0,
            interval_s=120.0, min_samples=3,
        )


def test_wait_until_validates_exactly_one():
    with pytest.raises(Exception):
        WaitUntilStep(timestamp="2026-01-01T00:00:00", seconds_from_now=10)
    with pytest.raises(Exception):
        WaitUntilStep()


# === polling executor ===

def _make_session_for_polling(value_sequence):
    """毎 query で value_sequence の次の値を返す session を作る"""
    yaml_str = """
metadata:
  manufacturer: "Test"
  model: "TC"
  category: "multimeter"
commands:
  measure:
    scpi: "MEAS?"
    type: "query"
    polling_safe: true
"""
    d = InstrumentDefinition(**yaml.safe_load(textwrap.dedent(yaml_str)))
    session = InstrumentSession(
        resource_name="TEST::INSTR", idn_response="<x>",
        idn_parsed={}, definition=d,
    )

    visa = MagicMock()
    idx = {"i": 0}

    async def fake_query(*args, **kwargs):
        i = idx["i"]
        idx["i"] = min(i + 1, len(value_sequence) - 1)
        return str(value_sequence[i])

    visa.query = fake_query
    return visa, session


@pytest.mark.asyncio
async def test_wait_for_condition_immediate_success():
    visa, session = _make_session_for_polling([90.0])  # 最初の値で条件達成
    step = WaitForConditionStep(
        instrument="TEST::INSTR", command="measure",
        condition_expr="value > 80",
        interval_s=0.05, timeout_s=2.0,
    )
    res = await execute_wait_for_condition(
        visa, lambda _name: session, step,
        cancel_check=lambda: None,
    )
    assert res["success"] is True
    assert res["last_value"] == 90.0
    assert res["poll_count"] == 1
    assert res["valid_sample_count"] == 1


@pytest.mark.asyncio
async def test_wait_for_condition_timeout():
    visa, session = _make_session_for_polling([10.0, 11.0, 12.0])
    step = WaitForConditionStep(
        instrument="TEST::INSTR", command="measure",
        condition_expr="value > 100",
        interval_s=0.05, timeout_s=0.3,
    )
    res = await execute_wait_for_condition(
        visa, lambda _n: session, step,
        cancel_check=lambda: None,
    )
    assert res["success"] is False
    assert res["error"] == "WaitConditionTimeout"


@pytest.mark.asyncio
async def test_wait_for_condition_cancel():
    visa, session = _make_session_for_polling([10.0, 11.0, 12.0])
    step = WaitForConditionStep(
        instrument="TEST::INSTR", command="measure",
        condition_expr="value > 100",
        interval_s=0.5, timeout_s=10.0,
    )
    cancel_flag = {"v": False}

    async def trigger_cancel():
        await asyncio.sleep(0.15)
        cancel_flag["v"] = True

    asyncio.create_task(trigger_cancel())
    res = await execute_wait_for_condition(
        visa, lambda _n: session, step,
        cancel_check=lambda: "cancel" if cancel_flag["v"] else None,
    )
    assert res["success"] is False
    assert res.get("interrupted_by_cancel") is True


@pytest.mark.asyncio
async def test_wait_for_stable_success_after_window():
    # 一定値を返し続け、window 内 max-min が 0 になる
    visa, session = _make_session_for_polling([25.0] * 50)
    step = WaitForStableStep(
        instrument="TEST::INSTR", command="measure",
        tolerance=0.1, window_s=0.3, timeout_s=5.0,
        interval_s=0.05, min_samples=3,
    )
    res = await execute_wait_for_stable(
        visa, lambda _n: session, step,
        cancel_check=lambda: None,
    )
    assert res["success"] is True
    assert res["final_delta"] == 0.0


def test_is_stable_rejects_before_window_elapsed():
    """internal review fix: window_s 経過前に min_samples だけ溜まっても stable と判定しない"""
    from lab_executor.polling_executor import _is_stable
    # window_s=60、interval=5 想定で、開始 10 秒で 3 サンプル (全て同値)
    samples = [(0.0, 25.0), (5.0, 25.0), (10.0, 25.0)]
    stable, delta = _is_stable(samples, tolerance=0.1, min_samples=3, window_s=60.0)
    assert stable is False, "window_s=60 に渡って観測していないのに stable になっている"
    # delta は参考値として返る
    assert delta == 0.0


def test_is_stable_accepts_after_window_elapsed():
    """window_s 経過後に min_samples 以上があり tolerance 以内なら stable"""
    from lab_executor.polling_executor import _is_stable
    # window_s=10、合計 15 秒観測、最後 10 秒に 3 サンプル
    samples = [(0.0, 24.0), (5.0, 25.0), (10.0, 25.0), (15.0, 25.05)]
    stable, delta = _is_stable(samples, tolerance=0.1, min_samples=3, window_s=10.0)
    assert stable is True
    # window 内のみ評価: t=[5,10,15] (15-10=5 <= window_s=10)
    assert delta is not None and delta <= 0.1


@pytest.mark.asyncio
async def test_wait_for_stable_timeout():
    # 値が振動し続けて安定しない
    seq = []
    for _ in range(200):
        seq.extend([20.0, 25.0])
    visa, session = _make_session_for_polling(seq)
    step = WaitForStableStep(
        instrument="TEST::INSTR", command="measure",
        tolerance=0.1, window_s=0.2, timeout_s=0.5,
        interval_s=0.05, min_samples=3,
    )
    res = await execute_wait_for_stable(
        visa, lambda _n: session, step,
        cancel_check=lambda: None,
    )
    assert res["success"] is False
    assert res["error"] == "WaitStableTimeout"


@pytest.mark.asyncio
async def test_wait_for_stable_cancel():
    seq = [20.0, 25.0] * 50
    visa, session = _make_session_for_polling(seq)
    step = WaitForStableStep(
        instrument="TEST::INSTR", command="measure",
        tolerance=0.01, window_s=1.0, timeout_s=10.0,
        interval_s=0.5, min_samples=3,
    )
    cancel_flag = {"v": False}

    async def trigger():
        await asyncio.sleep(0.15)
        cancel_flag["v"] = True

    asyncio.create_task(trigger())
    res = await execute_wait_for_stable(
        visa, lambda _n: session, step,
        cancel_check=lambda: "cancel" if cancel_flag["v"] else None,
    )
    assert res["success"] is False
    assert res.get("interrupted_by_cancel") is True


@pytest.mark.asyncio
async def test_polling_query_error_retry_then_succeed():
    """1 回 visa エラー、その retry で成功 → success"""
    yaml_str = """
metadata: { manufacturer: T, model: X, category: multimeter }
commands:
  measure: { scpi: "MEAS?", type: "query", polling_safe: true }
"""
    d = InstrumentDefinition(**yaml.safe_load(textwrap.dedent(yaml_str)))
    session = InstrumentSession(
        resource_name="TEST::INSTR", idn_response="<x>",
        idn_parsed={}, definition=d,
    )
    visa = MagicMock()
    calls = {"n": 0}
    from lab_visa_mcp.visa_manager import VisaError

    async def q(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise VisaError("transient")
        return "90.0"

    visa.query = q
    step = WaitForConditionStep(
        instrument="TEST::INSTR", command="measure",
        condition_expr="value > 80",
        interval_s=0.05, timeout_s=2.0,
        retry_on_error=1, max_consecutive_errors=3,
    )
    res = await execute_wait_for_condition(
        visa, lambda _n: session, step,
        cancel_check=lambda: None,
    )
    assert res["success"] is True
    assert calls["n"] >= 2


@pytest.mark.asyncio
async def test_polling_consecutive_errors_failed():
    """連続失敗が max_consecutive_errors を超えると step failed"""
    yaml_str = """
metadata: { manufacturer: T, model: X, category: multimeter }
commands:
  measure: { scpi: "MEAS?", type: "query", polling_safe: true }
"""
    d = InstrumentDefinition(**yaml.safe_load(textwrap.dedent(yaml_str)))
    session = InstrumentSession(
        resource_name="TEST::INSTR", idn_response="<x>",
        idn_parsed={}, definition=d,
    )
    from lab_visa_mcp.visa_manager import VisaError
    visa = MagicMock()

    async def q(*a, **kw):
        raise VisaError("always fail")

    visa.query = q
    step = WaitForConditionStep(
        instrument="TEST::INSTR", command="measure",
        condition_expr="value > 0",
        interval_s=0.02, timeout_s=5.0,
        retry_on_error=0, max_consecutive_errors=2,
    )
    res = await execute_wait_for_condition(
        visa, lambda _n: session, step,
        cancel_check=lambda: None,
    )
    assert res["success"] is False
    assert res["error"] == "PollingErrorExceeded"


@pytest.mark.asyncio
async def test_wait_until_seconds_from_now():
    step = WaitUntilStep(seconds_from_now=0.2)
    import time
    t = time.monotonic()
    res = await execute_wait_until(step, cancel_check=lambda: None)
    elapsed = time.monotonic() - t
    assert res["success"] is True
    assert 0.15 < elapsed < 1.0


# === Recipe with polling wait holds resource lock ===

RECIPE_WITH_POLLING = """
metadata:
  manufacturer: "Test"
  model: "PSU"
  category: "power_supply"
commands:
  reset:
    scpi: "*RST"
    type: "write"
  set_voltage:
    scpi: "VOLT {voltage}"
    type: "write"
    parameters:
      - { name: voltage, type: float, range: [0, 100] }
recipes:
  with_poll:
    parameters: []
    steps:
      - { command: "set_voltage", args: { voltage: 5 } }
      - wait_for_stable:
          instrument: "TEMP::INSTR"
          command: "measure"
          tolerance: 0.1
          window_s: 0.3
          timeout_s: 3.0
          interval_s: 0.05
          min_samples: 3
"""

TEMP_INSTR_YAML = """
metadata:
  manufacturer: "Test"
  model: "TC"
  category: "multimeter"
commands:
  measure:
    scpi: "MEAS?"
    type: "query"
    polling_safe: true
"""


@pytest.mark.asyncio
async def test_recipe_with_polling_holds_lock_on_temp_resource(tmp_path, monkeypatch):
    """polling 対象 instrument (TEMP::INSTR) も required_resources に含まれることを確認"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    visa = MagicMock()
    visa.write = AsyncMock(return_value=None)
    visa.query = AsyncMock(return_value="25.0")

    d_psu = InstrumentDefinition(**yaml.safe_load(textwrap.dedent(RECIPE_WITH_POLLING)))
    d_temp = InstrumentDefinition(**yaml.safe_load(textwrap.dedent(TEMP_INSTR_YAML)))

    s_psu = InstrumentSession(
        resource_name="PSU::INSTR", idn_response="<x>",
        idn_parsed={}, definition=d_psu,
    )
    s_temp = InstrumentSession(
        resource_name="TEMP::INSTR", idn_response="<x>",
        idn_parsed={}, definition=d_temp,
    )

    class _SM:
        def get_session(self, name):
            return {"PSU::INSTR": s_psu, "TEMP::INSTR": s_temp}.get(name)

    store = JobStore(db_path=tmp_path / "j.sqlite")
    mgr = JobManager(visa, _SM(), store=store)
    try:
        rec = await mgr.start_recipe_job("PSU::INSTR", "with_poll", {})
        # scheduler の job_resources に PSU + TEMP の両方が登録されているはず
        snap = mgr.scheduler.snapshot()
        # rec.job_id は job_resources に PSU::INSTR と TEMP::INSTR を持つはず
        assert rec.job_id in snap["job_resources"]
        resources = snap["job_resources"][rec.job_id]
        assert "PSU::INSTR" in resources
        assert "TEMP::INSTR" in resources

        # 終端まで待つ
        for _ in range(60):
            if is_terminal(mgr.get(rec.job_id).status):
                break
            await asyncio.sleep(0.1)
        final = mgr.get(rec.job_id)
        assert final.status == JobStatus.COMPLETED
    finally:
        store.close()


# === start_wait_job ===

@pytest.mark.asyncio
async def test_start_wait_job_seconds_no_resource(tmp_path, monkeypatch):
    """wait_type=seconds は resource を持たず即起動"""
    visa = MagicMock()
    visa.query = AsyncMock(return_value="0")

    class _SM:
        def get_session(self, name): return None

    store = JobStore(db_path=tmp_path / "j.sqlite")
    mgr = JobManager(visa, _SM(), store=store)
    try:
        rec = await mgr.start_wait_job(
            wait_type="seconds", params={"seconds": 0.1},
        )
        for _ in range(30):
            if is_terminal(mgr.get(rec.job_id).status): break
            await asyncio.sleep(0.05)
        final = mgr.get(rec.job_id)
        assert final.status == JobStatus.COMPLETED
        # required_resources が空 → scheduler 上 no resource (snapshot で確認)
        snap = mgr.scheduler.snapshot()
        assert rec.job_id not in snap["job_resources"]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_start_wait_job_condition_returns_job_id(tmp_path, monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    yaml_str = """
metadata: { manufacturer: T, model: X, category: multimeter }
commands:
  measure: { scpi: "MEAS?", type: "query", polling_safe: true }
"""
    d = InstrumentDefinition(**yaml.safe_load(textwrap.dedent(yaml_str)))
    session = InstrumentSession(
        resource_name="TEMP::INSTR", idn_response="<x>",
        idn_parsed={}, definition=d,
    )
    visa = MagicMock()
    visa.query = AsyncMock(return_value="90")

    class _SM:
        def get_session(self, name):
            return session if name == "TEMP::INSTR" else None

    store = JobStore(db_path=tmp_path / "j.sqlite")
    mgr = JobManager(visa, _SM(), store=store)
    try:
        rec = await mgr.start_wait_job(
            wait_type="condition",
            params={
                "instrument": "TEMP::INSTR",
                "command": "measure",
                "condition_expr": "value > 80",
                "interval_s": 0.05,
                "timeout_s": 2.0,
            },
        )
        assert rec.job_id.startswith("job_")
        for _ in range(40):
            if is_terminal(mgr.get(rec.job_id).status): break
            await asyncio.sleep(0.05)
        final = mgr.get(rec.job_id)
        assert final.status == JobStatus.COMPLETED
        assert final.result["steps_executed"][0]["last_value"] == 90.0
    finally:
        store.close()


# === polling progress in get_job_status ===

@pytest.mark.asyncio
async def test_get_progress_during_polling(tmp_path, monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    yaml_str = """
metadata: { manufacturer: T, model: X, category: multimeter }
commands:
  measure: { scpi: "MEAS?", type: "query", polling_safe: true }
"""
    d = InstrumentDefinition(**yaml.safe_load(textwrap.dedent(yaml_str)))
    session = InstrumentSession(
        resource_name="TEMP::INSTR", idn_response="<x>",
        idn_parsed={}, definition=d,
    )
    visa = MagicMock()
    visa.query = AsyncMock(return_value="50")  # 条件不達

    class _SM:
        def get_session(self, name):
            return session if name == "TEMP::INSTR" else None

    store = JobStore(db_path=tmp_path / "j.sqlite")
    mgr = JobManager(visa, _SM(), store=store)
    try:
        rec = await mgr.start_wait_job(
            wait_type="condition",
            params={
                "instrument": "TEMP::INSTR",
                "command": "measure",
                "condition_expr": "value > 100",
                "interval_s": 0.05,
                "timeout_s": 5.0,
            },
        )
        # しばらく polling させてから progress を取得
        await asyncio.sleep(0.25)
        prog = mgr.get_progress(rec.job_id)
        assert prog is not None
        assert prog["step_type"] == "wait_for_condition"
        assert "elapsed_s" in prog
        assert "poll_count" in prog
        assert "valid_sample_count" in prog
        assert prog["last_value"] == 50.0
        # cleanup
        await mgr.cancel(rec.job_id, CancelMode.IMMEDIATE, timeout_s=5)
    finally:
        store.close()
