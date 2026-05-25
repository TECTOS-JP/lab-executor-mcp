"""experiment_ir パッケージのテスト (v0.5.0-rc1)"""
import pytest
from pydantic import ValidationError

from lab_executor.experiment_ir import CommandStep, WaitStep, Plan


# === CommandStep ===

def test_command_step_basic():
    s = CommandStep(command="set_voltage", args={"voltage": 5.0})
    assert s.type == "command"
    assert s.command == "set_voltage"
    assert s.args == {"voltage": 5.0}
    assert s.result_as is None


def test_command_step_with_result_as():
    s = CommandStep(command="measure_voltage", result_as="v")
    assert s.result_as == "v"


def test_command_step_type_field_is_literal():
    """type フィールドは command 固定 (discriminator)"""
    s = CommandStep(command="x")
    assert s.type == "command"


# === WaitStep ===

def test_wait_step_basic():
    s = WaitStep(seconds=10)
    assert s.type == "wait"
    assert s.seconds == 10.0


def test_wait_step_zero_allowed():
    s = WaitStep(seconds=0)
    assert s.seconds == 0


def test_wait_step_float():
    s = WaitStep(seconds=0.5)
    assert s.seconds == 0.5


def test_wait_step_negative_rejected():
    """v0.5.0.1: field_validator で負数を拒否"""
    with pytest.raises(Exception):  # pydantic ValidationError
        WaitStep(seconds=-1)
    with pytest.raises(Exception):
        WaitStep(seconds=-0.001)


# === Plan ===

def test_plan_empty():
    p = Plan()
    assert p.steps == []
    assert p.step_count == 0
    assert p.parameters == {}


def test_plan_with_mixed_steps():
    p = Plan(
        name="test",
        parameters={"target_v": 5.0},
        steps=[
            CommandStep(command="set_voltage", args={"voltage": 5.0}),
            WaitStep(seconds=60),
            CommandStep(command="measure_voltage", result_as="v"),
        ],
    )
    assert p.step_count == 3
    assert p.steps[0].type == "command"
    assert p.steps[1].type == "wait"
    assert p.steps[2].type == "command"


def test_plan_discriminated_union_serialization():
    """Plan を dict 化 → 再構築でも step type が保持される"""
    p = Plan(
        steps=[
            CommandStep(command="x", args={}),
            WaitStep(seconds=5),
        ],
    )
    d = p.model_dump()
    p2 = Plan.model_validate(d)
    assert isinstance(p2.steps[0], CommandStep)
    assert isinstance(p2.steps[1], WaitStep)
    assert p2.steps[1].seconds == 5


def test_plan_resource_hint_optional():
    p = Plan(resource_hint="GPIB0::1::INSTR")
    assert p.resource_hint == "GPIB0::1::INSTR"
    p2 = Plan()
    assert p2.resource_hint is None


def test_plan_with_metadata():
    p = Plan(metadata={"source": "recipe:safe_output_on"})
    assert p.metadata["source"] == "recipe:safe_output_on"
