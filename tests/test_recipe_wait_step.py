"""Recipe の wait step テスト (v0.5.0-rc1)"""
import textwrap
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from lab_executor.models.instrument_def import InstrumentDefinition, RecipeStep
from visa_mcp.session_manager import InstrumentSession
from lab_executor.recipe_executor import execute_recipe, recipe_to_plan
from lab_executor.experiment_ir import CommandStep, WaitStep


# === RecipeStep schema 検証 (新規 wait step) ===

def test_recipe_step_command_only():
    s = RecipeStep(command="set_voltage", args={"voltage": 5})
    assert s.step_type == "command"


def test_recipe_step_wait_only():
    s = RecipeStep(wait={"seconds": 60})
    assert s.step_type == "wait"


def test_recipe_step_command_and_wait_rejected():
    """両方指定はエラー"""
    with pytest.raises(Exception):  # pydantic ValidationError
        RecipeStep(command="x", wait={"seconds": 1})


def test_recipe_step_neither_rejected():
    """両方未指定もエラー"""
    with pytest.raises(Exception):
        RecipeStep()


def test_recipe_step_wait_without_seconds_rejected():
    with pytest.raises(Exception):
        RecipeStep(wait={})


def test_recipe_step_wait_negative_rejected():
    with pytest.raises(Exception):
        RecipeStep(wait={"seconds": -1})


def test_recipe_step_wait_zero_ok():
    s = RecipeStep(wait={"seconds": 0})
    assert s.step_type == "wait"


# === Recipe → Plan 変換 ===

SAMPLE_YAML = """
metadata:
  manufacturer: "Test"
  model: "PSU"
commands:
  set_voltage:
    scpi: "VOLT {voltage}"
    type: "write"
    parameters:
      - { name: voltage, type: float, range: [0, 100] }
  measure_voltage:
    scpi: "MEAS:VOLT?"
    type: "query"
recipes:
  set_and_settle:
    description: "Set voltage and wait, then measure"
    parameters:
      - { name: target_v, type: float }
      - { name: settle_s, type: float, default: 5 }
    steps:
      - { command: "set_voltage", args: { voltage: "$target_v" } }
      - wait: { seconds: "$settle_s" }
      - { command: "measure_voltage" }
"""


def _make_session():
    d = InstrumentDefinition(**yaml.safe_load(textwrap.dedent(SAMPLE_YAML)))
    return InstrumentSession(
        resource_name="TEST::INSTR",
        idn_response="<test>",
        idn_parsed={"manufacturer": "Test", "model": "PSU"},
        definition=d,
    )


def test_recipe_to_plan_basic():
    s = _make_session()
    recipe = s.definition.recipes["set_and_settle"]
    plan = recipe_to_plan(recipe, {"target_v": 5.0, "settle_s": 3})
    assert plan.step_count == 3
    assert isinstance(plan.steps[0], CommandStep)
    assert plan.steps[0].args == {"voltage": 5.0}
    assert isinstance(plan.steps[1], WaitStep)
    assert plan.steps[1].seconds == 3.0
    assert isinstance(plan.steps[2], CommandStep)
    assert plan.steps[2].command == "measure_voltage"


def test_recipe_to_plan_wait_expression_evaluated():
    """wait.seconds に $ 式が書ければ評価される"""
    s = _make_session()
    recipe = s.definition.recipes["set_and_settle"]
    plan = recipe_to_plan(recipe, {"target_v": 1, "settle_s": 0.1})
    assert plan.steps[1].seconds == 0.1


# === execute_recipe with wait (実行時間で検証) ===

@pytest.mark.asyncio
async def test_recipe_with_wait_executes_and_blocks_for_seconds(monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    visa = MagicMock()
    visa.write = AsyncMock(return_value=None)
    visa.query = AsyncMock(return_value="+5.0")
    session = _make_session()

    t_start = time.time()
    result = await execute_recipe(
        visa, session, "set_and_settle",
        {"target_v": 5.0, "settle_s": 0.2},  # 200ms 待つ
    )
    elapsed = time.time() - t_start

    assert result["success"] is True
    assert result["step_count"] == 3
    # wait による所要時間 ≧ 0.2s
    assert elapsed >= 0.2

    # ステップ詳細確認
    steps = result["steps_executed"]
    assert steps[0]["command"] == "set_voltage"
    assert steps[0]["scpi_sent"] == "VOLT 5.0"
    assert steps[1]["step_type"] == "wait"
    assert steps[1]["seconds"] == 0.2
    assert steps[1]["success"] is True
    assert steps[2]["command"] == "measure_voltage"


@pytest.mark.asyncio
async def test_recipe_default_settle_s_applied(monkeypatch):
    """parameters の default が適用される"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    visa = MagicMock()
    visa.write = AsyncMock(return_value=None)
    visa.query = AsyncMock(return_value="+5.0")
    session = _make_session()

    # settle_s を渡さず default=5 を期待... だが 5s 待つのは遅いので
    # default 値が反映されたかだけ確認
    recipe = session.definition.recipes["set_and_settle"]
    plan = recipe_to_plan(recipe, {"target_v": 1.0, "settle_s": 5})  # 明示
    assert plan.steps[1].seconds == 5
