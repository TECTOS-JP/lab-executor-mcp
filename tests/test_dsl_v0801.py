"""v0.8.0.1: 外部レビュー P0/P1 対応テスト

- P0-1: CompiledPlan.rendered_steps が正式フィールドとして取れる
- P0-2: parallel placement 制約 (top-level 末尾 1 回のみ)
- P0-3: safe_shutdown.targets が CompiledPlan.safe_shutdown_targets に反映され、
        実行時にも対象 resource だけ shutdown される
"""
import asyncio
import textwrap
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

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
  set_output:
    scpi: "OUTP {state}"
    type: write
    parameters:
      - { name: state, type: enum, choices: ["ON", "OFF"] }
  measure_voltage:
    scpi: "MEAS:VOLT?"
    type: query
    polling_safe: true
safe_shutdown:
  - { command: set_output, args: { state: "OFF" } }
  - { command: set_voltage, args: { voltage: 0 } }
"""


def _make_setup(tmp_path, n_psus=1):
    """テスト用の setup (psu0/psu1/... の n 台)"""
    d = InstrumentDefinition(**yaml.safe_load(YAML_PSU))
    sessions = {
        f"psu{i}": InstrumentSession(
            resource_name=f"psu{i}", idn_response="<x>",
            idn_parsed={}, definition=d,
        )
        for i in range(n_psus)
    }

    class _SM:
        def get_session(self, name): return sessions.get(name)

    sys_cfg = SystemConfig(
        instruments={
            f"a{i}": InstrumentBinding(resource=f"psu{i}") for i in range(n_psus)
        },
    )
    visa = MagicMock()
    visa.write = AsyncMock(return_value=None)
    visa.query = AsyncMock(return_value="5.0")
    store = JobStore(db_path=tmp_path / "j.sqlite")
    mgr = JobManager(visa, _SM(), store=store, system_config=sys_cfg)
    return visa, _SM(), mgr, sys_cfg, store, sessions


# =========================================================
# P0-1: CompiledPlan.rendered_steps が正式フィールド
# =========================================================


def test_compiled_plan_includes_rendered_steps(tmp_path, monkeypatch):
    """validate_and_compile() の返り値に rendered_steps が含まれる"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    _, sm, mgr, _, store, _ = _make_setup(tmp_path)
    try:
        plan = {
            "dsl_version": "0.8",
            "bindings": {"psu": "a0"},
            "steps": [
                {"type": "command", "instrument": "$psu",
                 "command": "set_voltage", "args": {"voltage": 5.0}},
                {"type": "query", "instrument": "$psu",
                 "command": "measure_voltage"},
            ],
        }
        compiled = validate_and_compile(plan, sm, mgr.system_config)
        assert compiled.valid is True
        # rendered_steps が CompiledPlan に直接含まれる
        assert len(compiled.rendered_steps) == 2
        assert compiled.rendered_steps[0]["rendered_scpi"] == "VOLT 5.0"
        assert compiled.rendered_steps[1]["step_type"] == "query"
        assert compiled.rendered_steps[1]["rendered_scpi"] == "MEAS:VOLT?"
    finally:
        store.close()


def test_rendered_steps_includes_safety_and_verify(tmp_path, monkeypatch):
    """rendered_steps に safety / verify 情報が含まれる"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    _, sm, mgr, _, store, _ = _make_setup(tmp_path)
    try:
        plan = {
            "dsl_version": "0.8",
            "bindings": {"psu": "a0"},
            "steps": [
                {"type": "command", "instrument": "$psu",
                 "command": "set_voltage", "args": {"voltage": 5}},
            ],
        }
        compiled = validate_and_compile(plan, sm, mgr.system_config)
        assert compiled.valid is True
        step = compiled.rendered_steps[0]
        assert "safety" in step
        assert step["safety"]["status"] == "ok"
        # set_voltage に verify は定義されていない (YAML_PSU)
        assert step["verify"]["enabled"] is False
    finally:
        store.close()


# =========================================================
# P0-2: parallel placement 制約
# =========================================================


def test_parallel_in_middle_rejected():
    """parallel が top-level 中間にあると validation error"""
    plan = {
        "dsl_version": "0.8",
        "steps": [
            {"type": "wait", "seconds": 0.1},
            {"type": "parallel",
             "concurrency": 2,
             "branches": [[{"type": "wait", "seconds": 0.1}]]},
            {"type": "wait", "seconds": 0.1},   # parallel の後ろに step
        ],
    }
    visa = MagicMock()
    class _SM:
        def get_session(self, name): return None
    result = validate_and_compile(plan, _SM(), SystemConfig())
    assert result.valid is False
    assert any(e["error_class"] == "parallel_placement" for e in result.errors)


def test_multiple_parallel_rejected():
    """parallel が 2 回出現すると reject"""
    plan = {
        "dsl_version": "0.8",
        "steps": [
            {"type": "parallel", "concurrency": 2,
             "branches": [[{"type": "wait", "seconds": 0.1}]]},
            {"type": "parallel", "concurrency": 2,
             "branches": [[{"type": "wait", "seconds": 0.1}]]},
        ],
    }
    visa = MagicMock()
    class _SM:
        def get_session(self, name): return None
    result = validate_and_compile(plan, _SM(), SystemConfig())
    assert result.valid is False
    assert any(e["error_class"] == "parallel_placement" for e in result.errors)


def test_parallel_at_end_accepted(tmp_path, monkeypatch):
    """top-level 末尾の parallel は許可"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    _, sm, mgr, _, store, _ = _make_setup(tmp_path)
    try:
        plan = {
            "dsl_version": "0.8",
            "bindings": {"psu": "a0"},
            "steps": [
                {"type": "command", "instrument": "$psu",
                 "command": "set_voltage", "args": {"voltage": 5}},
                {"type": "parallel", "concurrency": 2, "branches": [
                    [{"type": "wait", "seconds": 0.01}],
                    [{"type": "wait", "seconds": 0.01}],
                ]},
            ],
        }
        result = validate_and_compile(plan, sm, mgr.system_config)
        assert result.valid is True
        assert result.summary["has_parallel"] is True
    finally:
        store.close()


# =========================================================
# P0-3: safe_shutdown.targets が CompiledPlan に反映 + 実行時に効く
# =========================================================


def test_safe_shutdown_targets_resolved_to_resources(tmp_path, monkeypatch):
    """DSL の safe_shutdown.targets が CompiledPlan.safe_shutdown_targets に
    解決済み resource list として入る"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    _, sm, mgr, _, store, _ = _make_setup(tmp_path, n_psus=2)
    try:
        plan = {
            "dsl_version": "0.8",
            "bindings": {"psu_a": "a0", "psu_b": "a1"},
            "steps": [
                {"type": "command", "instrument": "$psu_a",
                 "command": "set_voltage", "args": {"voltage": 5}},
                {"type": "command", "instrument": "$psu_b",
                 "command": "set_voltage", "args": {"voltage": 3}},
                # safe_shutdown は psu_a だけに適用
                {"type": "safe_shutdown", "targets": ["$psu_a"]},
            ],
        }
        compiled = validate_and_compile(plan, sm, mgr.system_config)
        assert compiled.valid is True
        assert compiled.has_safe_shutdown is True
        # safe_shutdown.targets が ["psu0"] に解決されている
        assert compiled.safe_shutdown_targets == ["psu0"]
    finally:
        store.close()


def test_safe_shutdown_default_is_all_used_resources(tmp_path, monkeypatch):
    """safe_shutdown.targets 未指定なら safe_shutdown_targets=None
    (実行時に required_resources を使う)"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    _, sm, mgr, _, store, _ = _make_setup(tmp_path)
    try:
        plan = {
            "dsl_version": "0.8",
            "bindings": {"psu": "a0"},
            "steps": [
                {"type": "command", "instrument": "$psu",
                 "command": "set_voltage", "args": {"voltage": 5}},
                {"type": "safe_shutdown"},   # targets 未指定
            ],
        }
        compiled = validate_and_compile(plan, sm, mgr.system_config)
        assert compiled.valid is True
        assert compiled.has_safe_shutdown is True
        assert compiled.safe_shutdown_targets is None  # → 実行時は required_resources
    finally:
        store.close()


@pytest.mark.asyncio
async def test_safe_shutdown_targets_respected_at_execution(tmp_path, monkeypatch):
    """**必須**: safe_shutdown.targets で指定した resource にだけ shutdown が走る"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    visa, sm, mgr, _, store, sessions = _make_setup(tmp_path, n_psus=2)
    try:
        plan = {
            "dsl_version": "0.8",
            "bindings": {"psu_a": "a0", "psu_b": "a1"},
            "steps": [
                {"type": "command", "instrument": "$psu_a",
                 "command": "set_voltage", "args": {"voltage": 5}},
                {"type": "command", "instrument": "$psu_b",
                 "command": "set_voltage", "args": {"voltage": 3}},
                {"type": "safe_shutdown", "targets": ["$psu_a"]},
            ],
        }
        rec = await mgr.start_experiment_job(plan)
        for _ in range(50):
            if is_terminal(mgr.get(rec.job_id).status): break
            await asyncio.sleep(0.05)
        final = mgr.get(rec.job_id)
        assert final.status == JobStatus.COMPLETED, final.last_step_summary

        # result の safe_shutdown 情報を確認
        sd = final.result.get("safe_shutdown")
        assert sd is not None
        assert sd["source"] == "explicit_targets"
        assert sd["targets"] == ["psu0"]
        assert len(sd["per_resource"]) == 1
        assert sd["per_resource"][0]["resource"] == "psu0"
        # psu1 (a1) には safe_shutdown が走っていない
        per_res_ids = {r["resource"] for r in sd["per_resource"]}
        assert "psu1" not in per_res_ids
    finally:
        store.close()


# =========================================================
# P1: list_experiment_templates / get_experiment_template (store level)
# =========================================================


def test_template_get_list_through_store(tmp_path):
    """store level の get_experiment_template / list_experiment_templates"""
    _, _, mgr, _, store, _ = _make_setup(tmp_path)
    try:
        plan = {"dsl_version": "0.8", "name": "t1", "steps": [
            {"type": "wait", "seconds": 1},
        ]}
        store.save_experiment_template("t1", "0.8", plan, description="d1")
        store.save_experiment_template("t2", "0.8", plan, description="d2")
        items = store.list_experiment_templates()
        names = {t["name"] for t in items}
        assert {"t1", "t2"}.issubset(names)
        # plan_json は list には含まれない
        for t in items:
            assert "plan" not in t
        # get で詳細取得
        got = store.get_experiment_template("t1")
        assert got["plan"] == plan
        # 存在しない
        assert store.get_experiment_template("missing") is None
    finally:
        store.close()
