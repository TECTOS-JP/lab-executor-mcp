"""safety モジュールのテスト"""
import os
import textwrap
from pathlib import Path

import pytest
import yaml

from lab_executor.models.instrument_def import InstrumentDefinition
from lab_executor import safety as sf


def _load_def(yaml_str: str) -> InstrumentDefinition:
    data = yaml.safe_load(textwrap.dedent(yaml_str))
    return InstrumentDefinition(**data)


SAMPLE_YAML = """
metadata:
  manufacturer: "Test"
  model: "PowerSupply"
commands:
  set_voltage:
    scpi: "VOLT {voltage}"
    type: "write"
    parameters:
      - name: voltage
        type: "float"
        range: [0, 100]
  set_voltage_protection:
    scpi: "VOLT:PROT {voltage}"
    type: "write"
    parameters:
      - name: voltage
        type: "float"
  set_output:
    scpi: "OUTP {state}"
    type: "write"
    parameters:
      - name: state
        type: "enum"
        choices: ["ON", "OFF"]
safety:
  ratings:
    voltage:
      rated: 35.0
      absolute_max: 36.75
      recommended_max: 35.0
      unit: "V"
  preconditions:
    - command: "set_output"
      when: { state: ["ON", "1"] }
      requires:
        - { has_been_called: "set_voltage_protection" }
      severity: "medium"
      reason: "出力 ON 前に OVP を設定すること"
"""


def test_no_violations_for_safe_value():
    d = _load_def(SAMPLE_YAML)
    v = sf.validate(d, "set_voltage", {"voltage": 5.0})
    assert v == []


def test_absolute_max_violation():
    d = _load_def(SAMPLE_YAML)
    v = sf.validate(d, "set_voltage", {"voltage": 50.0})
    assert any(x["violation_type"] == "absolute_max_exceeded" for x in v)
    assert all(x["severity"] == "high" for x in v)


def test_recommended_max_violation():
    d = _load_def(SAMPLE_YAML)
    v = sf.validate(d, "set_voltage", {"voltage": 35.5})
    assert any(x["violation_type"] == "recommended_max_exceeded" for x in v)


def test_precondition_unmet():
    d = _load_def(SAMPLE_YAML)
    # set_voltage_protection 未呼出で set_output ON
    v = sf.validate(d, "set_output", {"state": "ON"}, session_history=[])
    assert any(x["violation_type"] == "precondition_unmet" for x in v)


def test_precondition_met():
    d = _load_def(SAMPLE_YAML)
    v = sf.validate(
        d, "set_output", {"state": "ON"},
        session_history=["set_voltage_protection"],
    )
    assert v == []


def test_precondition_only_when_matching():
    """state=OFF の時は precondition チェックされない"""
    d = _load_def(SAMPLE_YAML)
    v = sf.validate(d, "set_output", {"state": "OFF"}, session_history=[])
    assert v == []


# === decide_action ===

def test_decide_proceed_no_violations():
    action, _ = sf.decide_action([], "advisory", False, None)
    assert action == "proceed"


def test_decide_block_advisory_without_override():
    viol = [sf.SafetyViolation("absolute_max_exceeded", "...", "high")]
    action, _ = sf.decide_action(viol, "advisory", False, None)
    assert action == "block_advisory"


def test_decide_proceed_with_valid_override():
    viol = [sf.SafetyViolation("absolute_max_exceeded", "...", "high")]
    action, _ = sf.decide_action(viol, "advisory", True, "意図的に上限超過テスト")
    assert action == "proceed"


def test_decide_block_advisory_override_without_reason():
    viol = [sf.SafetyViolation("absolute_max_exceeded", "...", "high")]
    action, msg = sf.decide_action(viol, "advisory", True, None)
    assert action == "block_advisory"
    assert "override_reason" in msg


def test_decide_strict_always_blocks():
    viol = [sf.SafetyViolation("absolute_max_exceeded", "...", "high")]
    # strict モードでは override が指定されてもブロック
    action, _ = sf.decide_action(viol, "strict", True, "理由あり")
    assert action == "block_strict"


def test_decide_permissive_always_proceeds():
    viol = [sf.SafetyViolation("absolute_max_exceeded", "...", "high")]
    action, _ = sf.decide_action(viol, "permissive", False, None)
    assert action == "proceed"


# === モード取得 ===

def test_get_safety_mode_default(monkeypatch):
    """v0.4.0 から既定モードは strict"""
    monkeypatch.delenv("VISA_MCP_SAFETY_MODE", raising=False)
    assert sf.get_safety_mode() == "strict"


def test_get_safety_mode_advisory_explicit(monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "advisory")
    assert sf.get_safety_mode() == "advisory"


def test_get_safety_mode_from_env(monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "strict")
    assert sf.get_safety_mode() == "strict"


def test_get_safety_mode_invalid_falls_back(monkeypatch):
    """v0.4.0 から不明値は strict にフォールバック"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "invalid")
    assert sf.get_safety_mode() == "strict"


# === 監査ログ ===

def test_audit_log_writes(tmp_path, monkeypatch):
    log_file = tmp_path / "audit.log"
    monkeypatch.setenv("VISA_MCP_AUDIT_LOG", str(log_file))
    sf.write_audit(
        "GPIB0::1::INSTR",
        "set_voltage",
        {"voltage": 50.0},
        [sf.SafetyViolation("absolute_max_exceeded", "test", "high")],
        action="proceed_with_override",
        mode="advisory",
        override_safety=True,
        override_reason="テスト用 override",
    )
    assert log_file.exists()
    lines = log_file.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1
    import json
    entry = json.loads(lines[0])
    assert entry["resource"] == "GPIB0::1::INSTR"
    assert entry["override_safety"] is True
    assert entry["override_reason"] == "テスト用 override"
