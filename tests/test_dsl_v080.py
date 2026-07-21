"""v0.8.0: Experiment DSL テスト

実装方針必須 3 件:
- test_dry_run_plan_no_visa_io
- test_sweep_rejects_too_many_points
- test_start_experiment_job_persists_original_plan
"""
import asyncio
import textwrap
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from lab_executor.dsl import (
    ExperimentPlan, SweepValues, CURRENT_DSL_VERSION,
    MAX_SWEEP_POINTS, MAX_PARALLEL_BRANCHES,
)
from lab_executor.dsl.compiler import validate_and_compile
from lab_executor.job import JobManager, JobStore
from lab_executor.job.state_machine import JobStatus, is_terminal
from lab_executor.models.instrument_def import InstrumentDefinition
from lab_visa_mcp.session_manager import InstrumentSession
from lab_executor.system_config import SystemConfig, InstrumentBinding


YAML_PSU = """
metadata: { manufacturer: T, model: PSU, category: power_supply }
commands:
  set_voltage:
    scpi: "VOLT {voltage}"
    type: write
    parameters:
      - { name: voltage, type: float, range: [0, 100] }
  measure_voltage:
    scpi: "MEAS:VOLT?"
    type: query
    polling_safe: true
  measure_current:
    scpi: "MEAS:CURR?"
    type: query
    polling_safe: true
  set_output:
    scpi: "OUTP {state}"
    type: write
    parameters:
      - { name: state, type: enum, choices: ["ON", "OFF"] }
"""


def _make_setup(tmp_path):
    """テスト用の session_mgr / job_mgr / system_config セット"""
    d = InstrumentDefinition(**yaml.safe_load(YAML_PSU))
    session = InstrumentSession(
        resource_name="psu0", idn_response="<x>",
        idn_parsed={}, definition=d,
    )

    class _SM:
        def get_session(self, name): return session if name == "psu0" else None

    sys_cfg = SystemConfig(
        instruments={"psu": InstrumentBinding(resource="psu0")},
    )
    visa = MagicMock()
    visa.write = AsyncMock(return_value=None)
    visa.query = AsyncMock(return_value="5.0")
    store = JobStore(db_path=tmp_path / "j.sqlite")
    mgr = JobManager(visa, _SM(), store=store, system_config=sys_cfg)
    return visa, _SM(), mgr, sys_cfg, store, session


# =========================================================
# Schema
# =========================================================


def test_dsl_version_default():
    p = ExperimentPlan(name="t", steps=[])
    assert p.dsl_version == "0.8"


def test_dsl_unknown_version_rejected():
    with pytest.raises(Exception):
        ExperimentPlan(dsl_version="0.9", name="t", steps=[])


def test_sweep_values_expand_values_list():
    sv = SweepValues(values=[1.0, 2.0, 3.0])
    assert sv.expand() == [1.0, 2.0, 3.0]


def test_sweep_values_expand_range():
    sv = SweepValues(start=0.0, stop=1.0, step=0.5)
    expanded = sv.expand()
    assert len(expanded) == 3
    assert expanded[0] == 0.0
    assert expanded[-1] == 1.0


def test_sweep_values_exclusive():
    with pytest.raises(Exception):
        SweepValues(start=0, stop=1, step=0.1, values=[1, 2])


# =========================================================
# Validator
# =========================================================


def test_validate_basic_plan(tmp_path, monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    _, sm, mgr, _, store, _ = _make_setup(tmp_path)
    try:
        plan = {
            "dsl_version": "0.8",
            "name": "test",
            "bindings": {"psu": "psu"},
            "steps": [
                {"type": "command", "instrument": "$psu",
                 "command": "set_voltage", "args": {"voltage": 5.0}},
                {"type": "wait", "seconds": 0.1},
                {"type": "query", "instrument": "$psu",
                 "command": "measure_voltage"},
            ],
        }
        result = validate_and_compile(plan, sm, mgr.system_config)
        assert result.valid is True
        assert result.summary["step_count_dsl"] == 3
        assert "psu0" in result.summary["required_resources"]
    finally:
        store.close()


def test_validate_unknown_step_type():
    plan = {"dsl_version": "0.8", "name": "t",
            "steps": [{"type": "unknown_type"}]}
    visa = MagicMock()
    class _SM:
        def get_session(self, name): return None
    sys_cfg = SystemConfig()
    result = validate_and_compile(plan, _SM(), sys_cfg)
    assert result.valid is False
    assert any("schema" in e.get("error_class", "") for e in result.errors)


def test_validate_unknown_instrument(tmp_path):
    _, sm, mgr, _, store, _ = _make_setup(tmp_path)
    try:
        plan = {
            "dsl_version": "0.8",
            "steps": [
                {"type": "command", "instrument": "missing_alias",
                 "command": "set_voltage", "args": {"voltage": 5}},
            ],
        }
        result = validate_and_compile(plan, sm, mgr.system_config)
        assert result.valid is False
        assert any(e["error_class"] == "unknown_instrument" for e in result.errors)
    finally:
        store.close()


def test_validate_unknown_command(tmp_path):
    _, sm, mgr, _, store, _ = _make_setup(tmp_path)
    try:
        plan = {
            "dsl_version": "0.8",
            "bindings": {"psu": "psu"},
            "steps": [
                {"type": "command", "instrument": "$psu",
                 "command": "set_volt_typo", "args": {"voltage": 5}},
            ],
        }
        result = validate_and_compile(plan, sm, mgr.system_config)
        assert result.valid is False
        assert any(e["error_class"] == "unknown_command" for e in result.errors)
        # recommended_next_actions が含まれる
        cmd_err = [e for e in result.errors if e["error_class"] == "unknown_command"][0]
        assert "recommended_next_actions" in cmd_err
        actions = [a["action"] for a in cmd_err["recommended_next_actions"]]
        assert "list_commands" in actions
    finally:
        store.close()


def test_validate_parameter_out_of_range(tmp_path, monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    _, sm, mgr, _, store, _ = _make_setup(tmp_path)
    try:
        plan = {
            "dsl_version": "0.8",
            "bindings": {"psu": "psu"},
            "steps": [
                {"type": "command", "instrument": "$psu",
                 "command": "set_voltage", "args": {"voltage": 999}},  # range max=100
            ],
        }
        result = validate_and_compile(plan, sm, mgr.system_config)
        assert result.valid is False
        assert any(e["error_class"] == "parameter_invalid" for e in result.errors)
    finally:
        store.close()


# =========================================================
# Sweep
# =========================================================


def test_sweep_expansion_values_list(tmp_path, monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    _, sm, mgr, _, store, _ = _make_setup(tmp_path)
    try:
        plan = {
            "dsl_version": "0.8",
            "bindings": {"psu": "psu"},
            "steps": [
                {
                    "type": "sweep",
                    "parameter": "voltage",
                    "values": {"values": [1.0, 2.0, 3.0]},
                    "body": [
                        {"type": "command", "instrument": "$psu",
                         "command": "set_voltage", "args": {"voltage": "{voltage}"}},
                    ],
                },
            ],
        }
        result = validate_and_compile(plan, sm, mgr.system_config)
        assert result.valid is True
        # 3 値 × 1 body step = 3 IR step
        assert len(result.main_plan.steps) == 3
    finally:
        store.close()


def test_sweep_rejects_too_many_points():
    """**必須**: sweep の上限を超えると validation 拒否"""
    plan = {
        "dsl_version": "0.8",
        "name": "huge_sweep",
        "steps": [
            {
                "type": "sweep",
                "parameter": "v",
                "values": {"start": 0, "stop": 10, "step": 0.001},  # 10001 点
                "body": [],
            },
        ],
    }
    # Pydantic validation で reject されるはず
    visa = MagicMock()
    class _SM:
        def get_session(self, name): return None
    result = validate_and_compile(plan, _SM(), SystemConfig())
    assert result.valid is False
    # schema_invalid または expanded_too_large
    assert any(
        e["error_class"] in ("schema_invalid", "expanded_too_large")
        for e in result.errors
    )


# =========================================================
# Dry-run: 実機 I/O 無し
# =========================================================


def test_dry_run_plan_no_visa_io(tmp_path, monkeypatch):
    """**必須**: dry_run 相当 (validate_and_compile) では visa.write / query が 0 回"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    visa, sm, mgr, _, store, _ = _make_setup(tmp_path)
    try:
        plan = {
            "dsl_version": "0.8",
            "bindings": {"psu": "psu"},
            "steps": [
                {"type": "command", "instrument": "$psu",
                 "command": "set_voltage", "args": {"voltage": 5}},
                {"type": "wait", "seconds": 1},
                {"type": "query", "instrument": "$psu",
                 "command": "measure_voltage"},
            ],
        }
        result = validate_and_compile(plan, sm, mgr.system_config)
        assert result.valid is True
        # visa は一切呼ばれていない
        assert visa.write.await_count == 0
        assert visa.query.await_count == 0
    finally:
        store.close()


# =========================================================
# Parallel
# =========================================================


def test_validate_parallel_respects_concurrency_limit():
    plan = {
        "dsl_version": "0.8",
        "steps": [
            {
                "type": "parallel",
                "concurrency": 100,  # 上限 10 超過
                "branches": [[{"type": "wait", "seconds": 0.1}]],
            },
        ],
    }
    visa = MagicMock()
    class _SM:
        def get_session(self, name): return None
    result = validate_and_compile(plan, _SM(), SystemConfig())
    assert result.valid is False
    assert any(e["error_class"] == "schema_invalid" for e in result.errors)


# =========================================================
# safe_shutdown
# =========================================================


def test_safe_shutdown_step_marker_only(tmp_path, monkeypatch):
    """safe_shutdown step は IR に直接落とさず、has_safe_shutdown フラグだけ立つ"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    _, sm, mgr, _, store, _ = _make_setup(tmp_path)
    try:
        plan = {
            "dsl_version": "0.8",
            "bindings": {"psu": "psu"},
            "steps": [
                {"type": "command", "instrument": "$psu",
                 "command": "set_voltage", "args": {"voltage": 5}},
                {"type": "safe_shutdown"},
            ],
        }
        result = validate_and_compile(plan, sm, mgr.system_config)
        assert result.valid is True
        assert result.has_safe_shutdown is True
        # IR には command step のみ (safe_shutdown は終端フック)
        assert len(result.main_plan.steps) == 1
    finally:
        store.close()


# =========================================================
# start_experiment_job + persistence
# =========================================================


@pytest.mark.asyncio
async def test_start_experiment_job_persists_original_plan(tmp_path, monkeypatch):
    """**必須**: start_experiment_job が experiment_plans テーブルに original plan を保存"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    _, _, mgr, _, store, _ = _make_setup(tmp_path)
    try:
        plan = {
            "dsl_version": "0.8",
            "name": "persist_test",
            "bindings": {"psu": "psu"},
            "steps": [
                {"type": "command", "instrument": "$psu",
                 "command": "set_voltage", "args": {"voltage": 5}},
            ],
        }
        rec = await mgr.start_experiment_job(plan)
        # 終端まで待つ
        for _ in range(40):
            if is_terminal(mgr.get(rec.job_id).status): break
            await asyncio.sleep(0.05)
        final = mgr.get(rec.job_id)
        assert final.status == JobStatus.COMPLETED, final.last_step_summary

        # experiment_plans テーブルに保存されているか
        saved = store.get_experiment_plan_for_job(rec.job_id)
        assert saved is not None
        assert saved["name"] == "persist_test"
        assert saved["dsl_version"] == "0.8"
        # original_plan が保持されている
        assert saved["original_plan"]["name"] == "persist_test"
        assert len(saved["original_plan"]["steps"]) == 1
        # compiled_summary も保存
        assert saved["compiled_summary"] is not None
        assert saved["compiled_summary"]["step_count_dsl"] == 1
        assert "psu0" in saved["compiled_summary"]["required_resources"]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_start_experiment_job_validation_failure_persists_errors(tmp_path):
    """validation 失敗時も plan を persist し、result に validation_errors を含める"""
    _, _, mgr, _, store, _ = _make_setup(tmp_path)
    try:
        plan = {
            "dsl_version": "0.8",
            "steps": [
                {"type": "command", "instrument": "unknown_alias",
                 "command": "set_voltage", "args": {"voltage": 5}},
            ],
        }
        rec = await mgr.start_experiment_job(plan)
        assert rec.status == JobStatus.FAILED
        # validation_errors が result に含まれる
        assert rec.result is not None
        verr = rec.result.get("validation_errors")
        assert verr is not None and len(verr) >= 1
        assert any(e["error_class"] == "unknown_instrument" for e in verr)
    finally:
        store.close()


# =========================================================
# Templates
# =========================================================


def test_experiment_template_save_and_get(tmp_path):
    _, _, mgr, _, store, _ = _make_setup(tmp_path)
    try:
        plan = {
            "dsl_version": "0.8",
            "name": "iv",
            "steps": [{"type": "wait", "seconds": 1}],
        }
        store.save_experiment_template(
            "iv_basic", "0.8", plan, description="basic IV",
        )
        got = store.get_experiment_template("iv_basic")
        assert got is not None
        assert got["name"] == "iv_basic"
        assert got["description"] == "basic IV"
        assert got["plan"]["name"] == "iv"

        # 上書き
        plan2 = {**plan, "name": "iv_v2"}
        store.save_experiment_template("iv_basic", "0.8", plan2)
        got2 = store.get_experiment_template("iv_basic")
        assert got2["plan"]["name"] == "iv_v2"

        # list
        items = store.list_experiment_templates()
        assert any(t["name"] == "iv_basic" for t in items)
    finally:
        store.close()


# =========================================================
# resolved_instruments / required_resources
# =========================================================


def test_compiled_summary_includes_resolved_instruments(tmp_path, monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    _, sm, mgr, _, store, _ = _make_setup(tmp_path)
    try:
        plan = {
            "dsl_version": "0.8",
            "bindings": {"psu": "psu"},
            "steps": [
                {"type": "command", "instrument": "$psu",
                 "command": "set_voltage", "args": {"voltage": 5}},
            ],
        }
        result = validate_and_compile(plan, sm, mgr.system_config)
        assert result.valid is True
        assert result.summary["resolved_instruments"]["$psu"] == "psu0"
    finally:
        store.close()


# =========================================================
# verify が summary に反映される
# =========================================================


def test_compiled_summary_uses_verify_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    YAML_WITH_VERIFY = YAML_PSU + textwrap.dedent("""
    """)
    # verify を追加した YAML
    YAML_VERIFY = """
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
  measure_voltage:
    scpi: "MEAS:VOLT?"
    type: query
    polling_safe: true
"""
    d = InstrumentDefinition(**yaml.safe_load(YAML_VERIFY))
    session = InstrumentSession(
        resource_name="psu0", idn_response="<x>",
        idn_parsed={}, definition=d,
    )

    class _SM:
        def get_session(self, name): return session if name == "psu0" else None

    sys_cfg = SystemConfig(instruments={"psu": InstrumentBinding(resource="psu0")})
    plan = {
        "dsl_version": "0.8",
        "bindings": {"psu": "psu"},
        "steps": [
            {"type": "command", "instrument": "$psu",
             "command": "set_voltage", "args": {"voltage": 5}},
        ],
    }
    result = validate_and_compile(plan, _SM(), sys_cfg)
    assert result.valid is True
    assert result.summary["uses_verify"] is True
    assert result.summary["uses_verify_count"] == 1
