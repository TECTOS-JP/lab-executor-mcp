"""v0.5.1.1: 外部レビュー対応テスト

- wait_for_condition 側の polling_safe_warning
- execute_recipe で polling step を踏んだ場合の AsyncStepRequiresJob
- wait_until の naive timestamp 拒否
- poll_count / valid_sample_count / consecutive_errors 分離
"""
import textwrap
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from lab_executor.experiment_ir import (
    WaitForConditionStep, WaitForStableStep, WaitUntilStep,
)
from lab_executor.models.instrument_def import InstrumentDefinition
from lab_executor.polling_executor import (
    execute_wait_for_condition,
    execute_wait_until,
)
from lab_visa_mcp.session_manager import InstrumentSession


# === wait_for_condition の polling_safe_warning ===


def _make_session(polling_safe: bool):
    yaml_str = f"""
metadata:
  manufacturer: T
  model: X
  category: multimeter
commands:
  measure:
    scpi: "MEAS?"
    type: query
    polling_safe: {str(polling_safe).lower()}
"""
    d = InstrumentDefinition(**yaml.safe_load(textwrap.dedent(yaml_str)))
    s = InstrumentSession(
        resource_name="TEMP::INSTR", idn_response="<x>",
        idn_parsed={}, definition=d,
    )
    visa = MagicMock()
    visa.query = AsyncMock(return_value="90.0")
    return visa, s


@pytest.mark.asyncio
async def test_wait_for_condition_emits_polling_safe_warning():
    """polling_safe=False の command を condition で使うと結果に警告"""
    visa, session = _make_session(polling_safe=False)
    step = WaitForConditionStep(
        instrument="TEMP::INSTR", command="measure",
        condition_expr="value > 80",
        interval_s=0.05, timeout_s=2.0,
    )
    res = await execute_wait_for_condition(
        visa, lambda _n: session, step,
        cancel_check=lambda: None,
    )
    assert res["success"] is True
    assert res.get("polling_safe_warning") is not None
    assert "polling_safe=False" in res["polling_safe_warning"]


@pytest.mark.asyncio
async def test_wait_for_condition_no_warning_when_polling_safe():
    visa, session = _make_session(polling_safe=True)
    step = WaitForConditionStep(
        instrument="TEMP::INSTR", command="measure",
        condition_expr="value > 80",
        interval_s=0.05, timeout_s=2.0,
    )
    res = await execute_wait_for_condition(
        visa, lambda _n: session, step,
        cancel_check=lambda: None,
    )
    assert res["success"] is True
    assert res.get("polling_safe_warning") is None


# === execute_recipe + polling = AsyncStepRequiresJob ===


RECIPE_WITH_POLLING_YAML = """
metadata: { manufacturer: T, model: PSU, category: power_supply }
commands:
  set_voltage:
    scpi: "VOLT {voltage}"
    type: write
    parameters:
      - { name: voltage, type: float, range: [0, 100] }
  measure:
    scpi: "MEAS?"
    type: query
    polling_safe: true
recipes:
  with_poll:
    parameters: []
    steps:
      - { command: set_voltage, args: { voltage: 5 } }
      - wait_for_condition:
          instrument: "PSU::INSTR"
          command: "measure"
          condition_expr: "value > 1"
          timeout_s: 5
"""


@pytest.mark.asyncio
async def test_execute_recipe_rejects_polling_step():
    """同期 execute_recipe で polling step を踏むと AsyncStepRequiresJob を返す"""
    from lab_executor.recipe_executor import execute_recipe
    d = InstrumentDefinition(**yaml.safe_load(textwrap.dedent(RECIPE_WITH_POLLING_YAML)))
    session = InstrumentSession(
        resource_name="PSU::INSTR", idn_response="<x>",
        idn_parsed={}, definition=d,
    )
    visa = MagicMock()
    visa.write = AsyncMock(return_value=None)
    visa.query = AsyncMock(return_value="5.0")
    res = await execute_recipe(visa, session, "with_poll", {})
    assert res["success"] is False
    assert res["error"] == "AsyncStepRequiresJob"
    assert "start_recipe_job" in res["message"]
    assert res["recommended_action"]["tool"] == "start_recipe_job"
    # 実機 write は 1 度も実行されていない (polling 検出 → 即座にエラー)
    visa.write.assert_not_called()


# === wait_until naive timestamp 拒否 ===


@pytest.mark.asyncio
async def test_wait_until_rejects_naive_timestamp():
    step = WaitUntilStep(timestamp="2026-05-22T15:00:00")  # naive
    res = await execute_wait_until(step, cancel_check=lambda: None)
    assert res["success"] is False
    assert res["error"] == "TimezoneRequired"
    assert "+09:00" in res["message"] or "timezone" in res["message"].lower()


@pytest.mark.asyncio
async def test_wait_until_accepts_tz_aware_timestamp_already_passed():
    """timezone 付き、かつ過去なら success (waited_s=0)"""
    step = WaitUntilStep(timestamp="2020-01-01T00:00:00+00:00")
    res = await execute_wait_until(step, cancel_check=lambda: None)
    assert res["success"] is True
    assert res["waited_s"] == 0.0


# === poll_count / valid_sample_count 分離 ===


@pytest.mark.asyncio
async def test_poll_count_and_valid_sample_count_differ_on_errors():
    """エラー発生時、poll_count は増えるが valid_sample_count は増えない"""
    yaml_str = """
metadata: { manufacturer: T, model: X, category: multimeter }
commands:
  measure: { scpi: "MEAS?", type: query, polling_safe: true }
"""
    d = InstrumentDefinition(**yaml.safe_load(textwrap.dedent(yaml_str)))
    session = InstrumentSession(
        resource_name="TEMP::INSTR", idn_response="<x>",
        idn_parsed={}, definition=d,
    )
    from lab_visa_mcp.visa_manager import VisaError

    visa = MagicMock()
    calls = {"n": 0}

    async def q(*a, **kw):
        calls["n"] += 1
        # 1, 2 回目はエラー、3 回目から成功 (retry_on_error=0 想定なので 3 回 poll)
        if calls["n"] <= 2:
            raise VisaError("transient")
        return "90.0"

    visa.query = q

    step = WaitForConditionStep(
        instrument="TEMP::INSTR", command="measure",
        condition_expr="value > 80",
        interval_s=0.02, timeout_s=2.0,
        retry_on_error=0, max_consecutive_errors=5,
    )
    res = await execute_wait_for_condition(
        visa, lambda _n: session, step,
        cancel_check=lambda: None,
    )
    assert res["success"] is True
    # poll_count: 3 (1 失敗 + 1 失敗 + 1 成功)
    # valid_sample_count: 1 (3 回目だけ有効)
    assert res["poll_count"] == 3
    assert res["valid_sample_count"] == 1
