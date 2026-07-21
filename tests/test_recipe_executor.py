"""recipe_executor のテスト (モック visa_mgr で実機なし検証)"""
import textwrap
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from lab_executor.models.instrument_def import InstrumentDefinition
from lab_visa_mcp.session_manager import InstrumentSession
from lab_executor.recipe_executor import execute_recipe


SAMPLE_YAML = """
metadata:
  manufacturer: "Test"
  model: "Supply"
commands:
  reset:
    scpi: "*RST"
    type: "write"
  set_voltage_protection:
    scpi: "VOLT:PROT {voltage}"
    type: "write"
    parameters:
      - name: voltage
        type: "float"
  set_voltage:
    scpi: "VOLT {voltage}"
    type: "write"
    parameters:
      - name: voltage
        type: "float"
        range: [0, 100]
  set_output:
    scpi: "OUTP {state}"
    type: "write"
    parameters:
      - name: state
        type: "enum"
        choices: ["ON", "OFF"]
recipes:
  safe_on:
    description: "Set OVP then voltage then output ON"
    parameters:
      - { name: "target_v", type: "float" }
    steps:
      - { command: "reset" }
      - { command: "set_voltage_protection", args: { voltage: "$target_v * 1.1" } }
      - { command: "set_voltage", args: { voltage: "$target_v" } }
      - { command: "set_output", args: { state: "ON" } }
"""


def _make_session():
    d = InstrumentDefinition(**yaml.safe_load(textwrap.dedent(SAMPLE_YAML)))
    return InstrumentSession(
        resource_name="TEST::INSTR",
        idn_response="<test>",
        idn_parsed={"manufacturer": "Test", "model": "Supply"},
        definition=d,
    )


@pytest.mark.asyncio
async def test_recipe_executes_all_steps(monkeypatch):
    visa = MagicMock()
    visa.write = AsyncMock(return_value=None)
    visa.query = AsyncMock(return_value="OK")
    session = _make_session()

    # 安全モードを permissive にして警告無視
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")

    result = await execute_recipe(visa, session, "safe_on", {"target_v": 10.0})
    assert result["success"] is True
    assert result["step_count"] == 4

    # 各ステップの SCPI が正しく生成されたか
    steps = result["steps_executed"]
    assert steps[0]["scpi_sent"] == "*RST"
    assert steps[1]["scpi_sent"] == "VOLT:PROT 11.0"  # 10 * 1.1
    assert steps[2]["scpi_sent"] == "VOLT 10.0"
    assert steps[3]["scpi_sent"] == "OUTP ON"


@pytest.mark.asyncio
async def test_recipe_not_found():
    visa = MagicMock()
    session = _make_session()
    result = await execute_recipe(visa, session, "nonexistent", {})
    assert result["success"] is False
    assert result["error"] == "RecipeNotFound"
    assert "safe_on" in result["available_recipes"]


@pytest.mark.asyncio
async def test_recipe_missing_parameter():
    visa = MagicMock()
    session = _make_session()
    result = await execute_recipe(visa, session, "safe_on", {})
    assert result["success"] is False
    assert result["error"] == "MissingParameter"


@pytest.mark.asyncio
async def test_recipe_halts_on_step_failure(monkeypatch):
    """途中ステップが失敗したら以降は実行されない"""
    from lab_visa_mcp.visa_manager import VisaError

    visa = MagicMock()
    visa.write = AsyncMock(side_effect=[None, VisaError("simulated"), None, None])
    session = _make_session()
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")

    result = await execute_recipe(visa, session, "safe_on", {"target_v": 10.0})
    assert result["success"] is False
    # 第1ステップは成功、第2で失敗、第3,4は実行されない
    assert len(result["steps_executed"]) == 2
    assert result["steps_executed"][0]["success"] is True
    assert result["steps_executed"][1]["success"] is False
