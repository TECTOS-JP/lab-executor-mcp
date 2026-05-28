"""v2.13.0: DSL validator predicted history (precondition fix).

Codex 実機 E2E で発覚した bug への対応:
plan walk 中、先行 CommandStep の command_name を resource 単位で
記録し、後続 step の precondition チェックで「session.command_history
+ predicted_history」を OR して使う。これにより、plan 内で
protection 設定 → set_output ON を順に並べた正しいプランが
strict mode の dry_run / validate_experiment_plan で
safety_violation にならない。

実機実行時 (start_experiment_job) は今までどおり
session.command_history が runner で更新されるので影響なし。
"""
from __future__ import annotations
from unittest.mock import MagicMock

import pytest

from lab_executor.models.instrument_def import (
    SafetyConfig, PreconditionCheck,
)


# ============================================================
# safety.validate 直接 test (baseline)
# ============================================================


def _make_definition_with_preconditions():
    """set_output に対し set_voltage_protection /
    set_current_protection の has_been_called を要求する最小
    InstrumentDefinition 風オブジェクト (safety.py は .safety.
    preconditions だけ参照するため duck-typing で OK)"""
    safety = SafetyConfig(
        ratings={},
        preconditions=[
            PreconditionCheck(
                command="set_output",
                when={"state": "ON"},
                requires=[
                    {"has_been_called": "set_voltage_protection"},
                    {"has_been_called": "set_current_protection"},
                ],
                severity="high",
                reason="出力 ON 前に保護を設定する",
            ),
        ],
    )
    return MagicMock(safety=safety)


def test_safety_validate_passes_when_history_satisfies_precondition():
    """baseline: safety.validate は session_history を見ている"""
    from lab_executor import safety as sf
    defn = _make_definition_with_preconditions()
    violations = sf.validate(
        defn, "set_output", {"state": "ON"},
        session_history=[
            "set_voltage_protection", "set_current_protection",
        ],
    )
    assert violations == []


def test_safety_validate_fails_when_history_empty():
    from lab_executor import safety as sf
    defn = _make_definition_with_preconditions()
    violations = sf.validate(
        defn, "set_output", {"state": "ON"},
        session_history=[],
    )
    assert len(violations) == 2
    assert all(v.get("violation_type") == "precondition_unmet"
                or getattr(v, "violation_type", None) ==
                "precondition_unmet"
                for v in violations)


# ============================================================
# _Context predicted history (compiler 内部仕組み test)
# ============================================================


def test_context_has_predicted_history_attr():
    """_Context が新 attribute を持つこと"""
    from lab_executor.dsl import compiler as comp
    ctx = comp._Context.__new__(comp._Context)
    # __init__ 経由でないと set されないが、attribute 命名が
    # 変わっていないことだけ確認
    src = open(comp.__file__, encoding="utf-8").read()
    assert "_predicted_history" in src
    assert "_parallel_depth" in src


def test_predicted_history_or_with_session_history():
    """compiler の predicted history merge ロジックを単独でなぞる:
    実機 history と予測 history を結合した状態で safety.validate
    を呼ぶ → precondition が満たされる"""
    from lab_executor import safety as sf
    defn = _make_definition_with_preconditions()

    # 実機 history は空、予測 history に protection が積まれている
    session_history = []
    predicted = ["set_voltage_protection", "set_current_protection"]
    combined = list(session_history) + predicted

    violations = sf.validate(
        defn, "set_output", {"state": "ON"},
        session_history=combined,
    )
    assert violations == []


def test_predicted_history_partial_does_not_satisfy():
    """予測 history が片方しかなければ依然として 1 件 violation"""
    from lab_executor import safety as sf
    defn = _make_definition_with_preconditions()
    combined = ["set_voltage_protection"]  # current_protection だけ抜け
    violations = sf.validate(
        defn, "set_output", {"state": "ON"},
        session_history=combined,
    )
    assert len(violations) == 1


# ============================================================
# regression
# ============================================================


def test_v213_version():
    import lab_executor
    parts = lab_executor.__version__.split(".")
    major = int(parts[0])
    minor = int(parts[1])
    assert (major, minor) >= (2, 13), (
        f"version {lab_executor.__version__} < 2.13")


def test_mcp_tool_surface_unchanged_v213():
    from lab_executor import stability
    stable = [t for ts in stability.STABLE_TOOLS.values() for t in ts]
    exp = [t for ts in stability.EXPERIMENTAL_TOOLS.values() for t in ts]
    assert len(stable) == 43
    assert len(exp) == 7


def test_compiler_module_has_predicted_history_logic():
    """compiler.py に combined_history を渡す code path があること
    を source check"""
    from lab_executor.dsl import compiler as comp
    src = open(comp.__file__, encoding="utf-8").read()
    assert "combined_history" in src
    assert "session_history=combined_history" in src
    # parallel_depth 制御もあること
    assert "_parallel_depth += 1" in src
    assert "_parallel_depth -= 1" in src
