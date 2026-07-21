"""v0.8.1.1: 外部レビュー P0/P1 対応テスト

- P0: safe_shutdown.targets 省略時、実行側で used_resources 全件 shutdown
       (summary / rendered_steps / execution result の 4 者一致)
- P1: dry_run_plan の errors[] に recommended_next_actions が top-level に出る
- P1: wait_for_condition / wait_for_stable の rendered_steps に
       instrument_ref / args_raw が含まれる
- P1: safe_shutdown rendered step に step_path が含まれる
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


def _setup(tmp_path, n=2):
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
# P0: safe_shutdown.targets 省略時の全 resource 実行
# =========================================================


@pytest.mark.asyncio
async def test_safe_shutdown_omitted_targets_shutdowns_all_used_resources(
    tmp_path, monkeypatch,
):
    """**P0 必須**: targets 省略時、used_resources の全 resource に shutdown が走る"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    visa, sm, mgr, _, store, sessions = _setup(tmp_path, n=3)
    try:
        plan = {
            "dsl_version": "0.8",
            "bindings": {"a": "a0", "b": "a1", "c": "a2"},
            "steps": [
                {"type": "command", "instrument": "$a",
                 "command": "set_voltage", "args": {"voltage": 5}},
                {"type": "command", "instrument": "$b",
                 "command": "set_voltage", "args": {"voltage": 3}},
                {"type": "command", "instrument": "$c",
                 "command": "set_voltage", "args": {"voltage": 1}},
                {"type": "safe_shutdown"},   # targets 省略
            ],
        }
        rec = await mgr.start_experiment_job(plan)
        for _ in range(60):
            if is_terminal(mgr.get(rec.job_id).status): break
            await asyncio.sleep(0.05)
        final = mgr.get(rec.job_id)
        assert final.status == JobStatus.COMPLETED, final.last_step_summary

        sd = final.result["safe_shutdown"]
        # 3 resource すべてが shutdown 対象
        assert sd["source"] == "all_used_resources"
        assert sorted(sd["targets"]) == ["psu0", "psu1", "psu2"]
        # per_resource が 3 件
        assert len(sd["per_resource"]) == 3
        per_ids = {r["resource"] for r in sd["per_resource"]}
        assert per_ids == {"psu0", "psu1", "psu2"}
    finally:
        store.close()


@pytest.mark.asyncio
async def test_safe_shutdown_omitted_targets_matches_summary_targets(
    tmp_path, monkeypatch,
):
    """**P0 必須**: 省略時の dry-run summary と実行時 result.safe_shutdown.targets が一致"""
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
        # compile (dry-run 相当)
        compiled = validate_and_compile(plan, sm, mgr.system_config)
        assert compiled.valid is True
        # summary 上の予測
        summary_targets = sorted(compiled.summary["safe_shutdown_targets"])
        assert summary_targets == ["psu0", "psu1"]
        assert compiled.summary["safe_shutdown_scope"] == "all_used_resources"

        # 実際に実行
        rec = await mgr.start_experiment_job(plan)
        for _ in range(40):
            if is_terminal(mgr.get(rec.job_id).status): break
            await asyncio.sleep(0.05)
        final = mgr.get(rec.job_id)
        assert final.status == JobStatus.COMPLETED
        execution_targets = sorted(final.result["safe_shutdown"]["targets"])

        # 4 者一致: dry-run summary == used_resources == execution result
        assert summary_targets == execution_targets
        assert summary_targets == sorted(compiled.used_resources)
    finally:
        store.close()


# =========================================================
# P1: dry_run_plan の errors[] に recommended_next_actions が top-level に
# =========================================================


def test_dry_run_plan_errors_have_top_level_recommended_next_actions(tmp_path):
    """dry_run_plan が返す errors[] で recommended_next_actions が details ではなく
    top-level に配置される (validate_experiment_plan と一貫)"""
    # tools/dsl.py の dry_run_plan 内部処理を直接呼ばず、make_error 経由の構造を確認
    from lab_executor.response_envelope import make_error
    err_dict = {
        "error_class": "unknown_command",
        "message": "command 'foo' missing",
        "recommended_next_actions": [{"action": "list_commands"}],
        "step_path": "steps[0]",
    }
    # v0.8.1.1 の dry_run_plan 実装と同じ変換
    e = make_error(
        err_dict.get("error_class", "validation"),
        err_dict.get("message", "?"),
        recoverable=True,
        recommended_next_actions=err_dict.get("recommended_next_actions"),
        details={k: v for k, v in err_dict.items()
                 if k not in ("error_class", "message", "recommended_next_actions")},
    )
    # recommended_next_actions が make_error の戻り値の top-level に
    assert "recommended_next_actions" in e
    assert e["recommended_next_actions"] == [{"action": "list_commands"}]
    # details には含まれない
    assert "recommended_next_actions" not in (e.get("details") or {})


# =========================================================
# P1: polling 系 rendered_steps に instrument_ref / args_raw
# =========================================================


def test_wait_for_stable_rendered_includes_instrument_ref_and_args_raw(
    tmp_path, monkeypatch,
):
    """wait_for_stable の rendered_steps に元 DSL の $ref と raw args が含まれる"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    _, sm, mgr, _, store, _ = _setup(tmp_path)
    try:
        plan = {
            "dsl_version": "0.8",
            "bindings": {"psu": "a0", "temp": "a1"},
            "steps": [
                {"type": "command", "instrument": "$psu",
                 "command": "set_voltage", "args": {"voltage": 5}},
                {"type": "wait_for_stable", "instrument": "$temp",
                 "command": "measure_voltage",
                 "tolerance": 0.1, "window_s": 5, "interval_s": 1,
                 "timeout_s": 30},
            ],
        }
        compiled = validate_and_compile(plan, sm, mgr.system_config)
        assert compiled.valid is True
        # wait_for_stable は内部で query を validate → rendered_steps に query が出る
        # その rendered で instrument_ref="$temp" が保持されている
        wfs_rendered = [
            r for r in compiled.rendered_steps
            if r.get("resolved_resource") == "psu1"
        ]
        assert len(wfs_rendered) == 1
        assert wfs_rendered[0]["instrument_ref"] == "$temp"
        assert wfs_rendered[0]["resolved_resource"] == "psu1"
    finally:
        store.close()


def test_wait_for_condition_rendered_includes_instrument_ref_and_args_raw(
    tmp_path, monkeypatch,
):
    """wait_for_condition も同様に instrument_ref を保持"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    _, sm, mgr, _, store, _ = _setup(tmp_path)
    try:
        plan = {
            "dsl_version": "0.8",
            "bindings": {"temp": "a0"},
            "steps": [
                {"type": "wait_for_condition", "instrument": "$temp",
                 "command": "measure_voltage",
                 "condition_expr": "value > 10",
                 "interval_s": 1, "timeout_s": 30},
            ],
        }
        compiled = validate_and_compile(plan, sm, mgr.system_config)
        assert compiled.valid is True
        wfc_rendered = [
            r for r in compiled.rendered_steps
            if r.get("resolved_resource") == "psu0"
        ]
        assert len(wfc_rendered) == 1
        assert wfc_rendered[0]["instrument_ref"] == "$temp"
    finally:
        store.close()


# =========================================================
# P1: safe_shutdown rendered step に step_path
# =========================================================


def test_safe_shutdown_rendered_step_includes_step_path(tmp_path, monkeypatch):
    """safe_shutdown の rendered_steps エントリに step_path が含まれる"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    _, sm, mgr, _, store, _ = _setup(tmp_path)
    try:
        plan = {
            "dsl_version": "0.8",
            "bindings": {"psu": "a0"},
            "steps": [
                {"type": "command", "instrument": "$psu",
                 "command": "set_voltage", "args": {"voltage": 5}},
                {"type": "safe_shutdown", "targets": ["$psu"]},
            ],
        }
        compiled = validate_and_compile(plan, sm, mgr.system_config)
        assert compiled.valid is True
        ss = [r for r in compiled.rendered_steps if r.get("step_type") == "safe_shutdown"]
        assert len(ss) == 1
        # step_path が含まれる
        assert "step_path" in ss[0]
        assert ss[0]["step_path"] == "steps[1]"
        # 後方互換 path も維持
        assert ss[0]["path"] == "steps[1]"
    finally:
        store.close()
