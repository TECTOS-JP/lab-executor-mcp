"""v0.8.1: DSL 安定化テスト

実装方針必須 3 件:
- test_dry_run_uses_compiled_rendered_steps_without_recompile
- test_safe_shutdown_targets_match_execution_targets
- test_parallel_only_allowed_at_top_level_tail (v0.8.0.1 で対応済み、互換確認)
"""
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from lab_executor.dsl.compiler import validate_and_compile, CompiledPlan
from lab_executor.job import JobManager, JobStore
from lab_executor.job.state_machine import JobStatus, is_terminal
from lab_executor.models.instrument_def import InstrumentDefinition
from lab_visa_mcp.session_manager import InstrumentSession
from lab_executor.system_config import (
    SystemConfig, InstrumentBinding,
)


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
"""


def _setup(tmp_path, n=1):
    d = InstrumentDefinition(**yaml.safe_load(YAML_PSU))
    sessions = {
        f"psu{i}": InstrumentSession(
            resource_name=f"psu{i}", idn_response="<x>",
            idn_parsed={}, definition=d,
        )
        for i in range(n)
    }

    class _SM:
        def get_session(self, name): return sessions.get(name)

    sys_cfg = SystemConfig(
        instruments={f"a{i}": InstrumentBinding(resource=f"psu{i}") for i in range(n)},
    )
    visa = MagicMock()
    visa.write = AsyncMock(return_value=None)
    visa.query = AsyncMock(return_value="5.0")
    store = JobStore(db_path=tmp_path / "j.sqlite")
    mgr = JobManager(visa, _SM(), store=store, system_config=sys_cfg)
    return visa, _SM(), mgr, sys_cfg, store, sessions


# =========================================================
# 必須 1: dry-run は再 compile しない
# =========================================================


def test_dry_run_uses_compiled_rendered_steps_without_recompile(tmp_path, monkeypatch):
    """**必須**: validate_and_compile 1 回で rendered_steps が CompiledPlan に
    含まれており、tools/dsl.py から _Context を再構築しない設計を確認"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    _, sm, mgr, _, store, _ = _setup(tmp_path)
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
        # rendered_steps が CompiledPlan の正式フィールドに含まれる
        assert isinstance(compiled.rendered_steps, list)
        assert len(compiled.rendered_steps) == 2
        # 1 回 compile しただけで rendered_scpi が取得できる (再 compile 不要)
        scpi = [r["rendered_scpi"] for r in compiled.rendered_steps]
        assert scpi == ["VOLT 5.0", "MEAS:VOLT?"]
    finally:
        store.close()


# =========================================================
# 必須 2: safe_shutdown.targets dry-run / execution / rendered_steps 3 者一致
# =========================================================


@pytest.mark.asyncio
async def test_safe_shutdown_targets_match_execution_targets(tmp_path, monkeypatch):
    """**必須**: DSL 指定 targets, CompiledPlan.safe_shutdown_targets,
    rendered_steps.targets, 実行時 Job result.safe_shutdown.targets の 4 者が一致"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    _, sm, mgr, _, store, _ = _setup(tmp_path, n=2)
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
        # compile 結果
        compiled = validate_and_compile(plan, sm, mgr.system_config)
        assert compiled.valid is True
        assert compiled.safe_shutdown_targets == ["psu0"]

        # rendered_steps の safe_shutdown エントリ
        ss_rendered = [
            r for r in compiled.rendered_steps if r.get("step_type") == "safe_shutdown"
        ]
        assert len(ss_rendered) == 1
        assert ss_rendered[0]["targets"] == ["psu0"]

        # 実行時
        rec = await mgr.start_experiment_job(plan)
        for _ in range(50):
            if is_terminal(mgr.get(rec.job_id).status): break
            await asyncio.sleep(0.05)
        final = mgr.get(rec.job_id)
        assert final.status == JobStatus.COMPLETED
        sd = final.result["safe_shutdown"]
        assert sd["targets"] == ["psu0"]
        # 4 者一致
        assert (compiled.safe_shutdown_targets == ss_rendered[0]["targets"]
                == sd["targets"] == ["psu0"])
    finally:
        store.close()


# =========================================================
# 必須 3: parallel placement (v0.8.0.1 互換確認)
# =========================================================


def test_parallel_only_allowed_at_top_level_tail():
    """v0.8.0.1 で導入した制約が v0.8.1 でも維持される"""
    plan = {
        "dsl_version": "0.8",
        "steps": [
            {"type": "wait", "seconds": 0.01},
            {"type": "parallel", "concurrency": 2,
             "branches": [[{"type": "wait", "seconds": 0.01}]]},
            {"type": "wait", "seconds": 0.01},
        ],
    }
    visa = MagicMock()
    class _SM:
        def get_session(self, name): return None
    result = validate_and_compile(plan, _SM(), SystemConfig())
    assert result.valid is False
    assert any(e["error_class"] == "parallel_placement" for e in result.errors)


# =========================================================
# rendered_steps 構造強化
# =========================================================


def test_rendered_steps_include_step_path_and_resolved_args(tmp_path, monkeypatch):
    """rendered_steps に step_path / args_raw / args_resolved / instrument_ref / resolved_resource が含まれる"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    _, sm, mgr, _, store, _ = _setup(tmp_path)
    try:
        plan = {
            "dsl_version": "0.8",
            "bindings": {"psu": "a0"},
            "steps": [
                {
                    "type": "sweep",
                    "parameter": "voltage",
                    "values": {"values": [1.0, 2.0]},
                    "body": [
                        {"type": "command", "instrument": "$psu",
                         "command": "set_voltage",
                         "args": {"voltage": "{voltage}"}},
                    ],
                },
            ],
        }
        compiled = validate_and_compile(plan, sm, mgr.system_config)
        assert compiled.valid is True
        assert len(compiled.rendered_steps) == 2
        r0 = compiled.rendered_steps[0]
        # step_path (階層内位置)
        assert "step_path" in r0
        assert "sweep[0].body[0]" in r0["step_path"]
        # instrument_ref (元 DSL の $psu) と resolved_resource (psu0) が区別される
        assert r0["instrument_ref"] == "$psu"
        assert r0["resolved_resource"] == "psu0"
        # args_raw はテンプレ展開前、args_resolved は展開後
        assert r0["args_raw"] == {"voltage": "{voltage}"}
        assert r0["args_resolved"] == {"voltage": 1.0}
        # command_type
        assert r0["command_type"] == "write"
    finally:
        store.close()


def test_rendered_steps_include_safety_and_verify_summary(tmp_path, monkeypatch):
    """safety / verify が rendered_steps に含まれる"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    _, sm, mgr, _, store, _ = _setup(tmp_path)
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
        r0 = compiled.rendered_steps[0]
        assert r0["safety"]["status"] == "ok"
        assert r0["safety"]["mode"] == "permissive"
        # verify は無効 (YAML_PSU に verify 定義なし)
        assert r0["verify"]["enabled"] is False
    finally:
        store.close()


# =========================================================
# warning / error の位置情報
# =========================================================


def test_warning_contains_field_path_and_recommended_next_actions(tmp_path):
    """unknown_command の error に step_path / field_path / recommended_next_actions が
    含まれる"""
    _, sm, mgr, _, store, _ = _setup(tmp_path)
    try:
        plan = {
            "dsl_version": "0.8",
            "bindings": {"psu": "a0"},
            "steps": [
                {"type": "command", "instrument": "$psu",
                 "command": "nonexistent", "args": {"voltage": 5}},
            ],
        }
        compiled = validate_and_compile(plan, sm, mgr.system_config)
        assert compiled.valid is False
        err = next(e for e in compiled.errors if e["error_class"] == "unknown_command")
        # step_path がある (path も backward compat で残る)
        assert "step_path" in err
        assert err["step_path"] == "steps[0]"
        # recommended_next_actions
        assert "recommended_next_actions" in err
        actions = [a["action"] for a in err["recommended_next_actions"]]
        assert "list_commands" in actions
    finally:
        store.close()


# =========================================================
# safe_shutdown.targets=[] reject
# =========================================================


def test_safe_shutdown_empty_targets_rejected(tmp_path):
    """targets=[] は曖昧なため validation error"""
    _, sm, mgr, _, store, _ = _setup(tmp_path)
    try:
        plan = {
            "dsl_version": "0.8",
            "bindings": {"psu": "a0"},
            "steps": [
                {"type": "command", "instrument": "$psu",
                 "command": "set_voltage", "args": {"voltage": 5}},
                {"type": "safe_shutdown", "targets": []},
            ],
        }
        compiled = validate_and_compile(plan, sm, mgr.system_config)
        assert compiled.valid is False
        err = next(
            e for e in compiled.errors
            if e["error_class"] == "safe_shutdown_targets_empty"
        )
        assert "omit_targets" in [
            a["action"] for a in err["recommended_next_actions"]
        ]
    finally:
        store.close()


def test_safe_shutdown_targets_default_all_used_resources(tmp_path, monkeypatch):
    """targets 未指定なら summary.safe_shutdown_targets == used_resources"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    _, sm, mgr, _, store, _ = _setup(tmp_path, n=2)
    try:
        plan = {
            "dsl_version": "0.8",
            "bindings": {"psu_a": "a0", "psu_b": "a1"},
            "steps": [
                {"type": "command", "instrument": "$psu_a",
                 "command": "set_voltage", "args": {"voltage": 5}},
                {"type": "command", "instrument": "$psu_b",
                 "command": "set_voltage", "args": {"voltage": 3}},
                {"type": "safe_shutdown"},
            ],
        }
        compiled = validate_and_compile(plan, sm, mgr.system_config)
        assert compiled.valid is True
        # safe_shutdown_targets は None (= used_resources を使う)
        assert compiled.safe_shutdown_targets is None
        # summary は scope=all_used_resources, targets=used_resources を含む
        assert compiled.summary["safe_shutdown_scope"] == "all_used_resources"
        assert set(compiled.summary["safe_shutdown_targets"]) == {"psu0", "psu1"}
    finally:
        store.close()


# =========================================================
# used_resources と required_resources の区別
# =========================================================


def test_used_resources_field_present(tmp_path, monkeypatch):
    """CompiledPlan.used_resources が独立フィールドとして存在"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    _, sm, mgr, _, store, _ = _setup(tmp_path, n=2)
    try:
        plan = {
            "dsl_version": "0.8",
            "bindings": {"psu_a": "a0", "psu_b": "a1"},
            "steps": [
                {"type": "command", "instrument": "$psu_a",
                 "command": "set_voltage", "args": {"voltage": 5}},
                {"type": "command", "instrument": "$psu_b",
                 "command": "set_voltage", "args": {"voltage": 3}},
            ],
        }
        compiled = validate_and_compile(plan, sm, mgr.system_config)
        assert compiled.valid is True
        assert isinstance(compiled.used_resources, list)
        assert compiled.used_resources == ["psu0", "psu1"]
    finally:
        store.close()


# =========================================================
# parallel 追加 validation
# =========================================================


def test_nested_parallel_rejected():
    """parallel.branches 内の nested parallel は reject"""
    plan = {
        "dsl_version": "0.8",
        "steps": [
            {
                "type": "parallel",
                "concurrency": 2,
                "branches": [
                    [
                        {"type": "parallel", "concurrency": 2,
                         "branches": [[{"type": "wait", "seconds": 0.01}]]},
                    ],
                    [{"type": "wait", "seconds": 0.01}],
                ],
            },
        ],
    }
    visa = MagicMock()
    class _SM:
        def get_session(self, name): return None
    result = validate_and_compile(plan, _SM(), SystemConfig())
    assert result.valid is False
    assert any(e["error_class"] == "nested_parallel" for e in result.errors)


def test_parallel_inside_sweep_rejected():
    """sweep.body 内に parallel があると reject"""
    plan = {
        "dsl_version": "0.8",
        "steps": [
            {
                "type": "sweep",
                "parameter": "v",
                "values": {"values": [1, 2]},
                "body": [
                    {"type": "parallel", "concurrency": 2,
                     "branches": [[{"type": "wait", "seconds": 0.01}]]},
                ],
            },
        ],
    }
    visa = MagicMock()
    class _SM:
        def get_session(self, name): return None
    result = validate_and_compile(plan, _SM(), SystemConfig())
    assert result.valid is False
    assert any(e["error_class"] == "parallel_inside_sweep" for e in result.errors)


def test_parallel_shared_resource_warns(tmp_path, monkeypatch):
    """parallel.branches が同じ resource を使うと warning"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    _, sm, mgr, _, store, _ = _setup(tmp_path)
    try:
        plan = {
            "dsl_version": "0.8",
            "bindings": {"psu": "a0"},
            "steps": [
                {
                    "type": "parallel",
                    "concurrency": 2,
                    "branches": [
                        [{"type": "command", "instrument": "$psu",
                          "command": "set_voltage", "args": {"voltage": 1.0}}],
                        [{"type": "command", "instrument": "$psu",
                          "command": "set_voltage", "args": {"voltage": 2.0}}],
                    ],
                },
            ],
        }
        compiled = validate_and_compile(plan, sm, mgr.system_config)
        assert compiled.valid is True
        # warning に shared resource あり
        shared_warnings = [
            w for w in compiled.warnings
            if w["warning_class"] == "parallel_shared_resource"
        ]
        assert len(shared_warnings) >= 1
        assert shared_warnings[0]["resource"] == "psu0"
    finally:
        store.close()


# =========================================================
# DSL examples が validate を通る
# =========================================================


@pytest.mark.parametrize("example_name", [
    "basic_voltage_set_and_measure",
    "voltage_sweep_with_wait",
    "voltage_sweep_with_wait_for_stable",
    "partial_failure_group_measurement",
    "safe_shutdown_explicit_targets",
])
def test_dsl_examples_parse_as_schema(example_name):
    """examples の plan.json が Pydantic schema を通る (機器 binding は実環境依存なので
    schema 適合のみ確認)"""
    from lab_executor.dsl.schema import ExperimentPlan
    base = Path(__file__).parent.parent / "docs" / "dsl" / "examples"
    p = base / example_name / "plan.json"
    if not p.exists():
        pytest.skip(f"example file missing: {p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    plan = ExperimentPlan(**data)
    assert plan.dsl_version == "0.8"
    assert plan.name == example_name


# =========================================================
# JSON Schema preview ファイルが存在
# =========================================================


def test_schema_files_generated():
    """schemas/*.schema.json が存在し、preview status を含む"""
    schemas_dir = Path(__file__).parent.parent / "schemas"
    for name in ("dsl", "instrument", "system_config"):
        p = schemas_dir / f"{name}.schema.json"
        assert p.exists(), f"{p} が生成されていない"
        data = json.loads(p.read_text(encoding="utf-8"))
        # v1.0 で stable へ昇格 (preview / stable いずれも許可)
        assert data.get("x-visa-mcp-status") in ("preview", "stable")
        compat = data.get("x-compatibility", "")
        assert ("subject-to-change-before-v1.0" in compat
                or "v1.x-compatible" in compat)
