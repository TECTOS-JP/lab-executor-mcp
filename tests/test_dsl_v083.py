"""v0.8.3: DSL usability refinement テスト

カバー範囲:
- ExperimentPlan.unit 直接対応
  - unit_bindings populates effective_bindings
  - explicit bindings override
  - unknown_unit / unit_role_missing
  - dry-run summary.unit_resolution
  - raw_resource_used_with_unit warning
- Template override (apply_template_override + start_experiment_job_from_template)
  - allowed: name / unit / bindings / parameters / owner
  - rejected: steps / dsl_version
  - validate after merge
  - dry_run mode
  - template_source recorded in job metadata
- JSON schema preview に unit が含まれる
"""
from __future__ import annotations
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from lab_executor.dsl.compiler import validate_and_compile
from lab_executor.dsl.template import (
    apply_template_override, TemplateOverrideError, ALLOWED_OVERRIDE_KEYS,
)
from lab_executor.job import JobManager, JobStore
from lab_executor.models.instrument_def import InstrumentDefinition
from visa_mcp.session_manager import InstrumentSession
from lab_executor.system_config import (
    SystemConfig, InstrumentBinding, ExperimentUnit,
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


def _setup_with_units(tmp_path):
    """psu001/psu002/dmm001/dmm_backup を持ち、unit001/unit002 を定義"""
    d = InstrumentDefinition(**yaml.safe_load(YAML_PSU))
    names = ["psu001", "psu002", "dmm001", "dmm_backup"]
    sessions = {
        n: InstrumentSession(
            resource_name=n, idn_response="<x>",
            idn_parsed={}, definition=d,
        )
        for n in names
    }

    class _SM:
        def get_session(self, name):
            return sessions.get(name)

    sys_cfg = SystemConfig(
        instruments={n: InstrumentBinding(resource=n) for n in names},
        experiment_units={
            "unit001": ExperimentUnit(bindings={"psu": "psu001", "dmm": "dmm001"}),
            "unit002": ExperimentUnit(bindings={"psu": "psu002", "dmm": "dmm001"}),
        },
    )
    visa = MagicMock()
    visa.write = AsyncMock(return_value=None)
    visa.query = AsyncMock(return_value="5.0")
    store = JobStore(db_path=tmp_path / "j.sqlite")
    mgr = JobManager(visa, _SM(), store=store, system_config=sys_cfg)
    return visa, _SM(), mgr, sys_cfg, store


# =========================================================
# 1. ExperimentPlan.unit が bindings を populate する
# =========================================================


def test_experiment_plan_unit_populates_bindings(tmp_path):
    _, sm, mgr, sys_cfg, store = _setup_with_units(tmp_path)
    try:
        plan = {
            "dsl_version": "0.8",
            "name": "t",
            "unit": "unit001",
            "steps": [
                {"type": "command", "instrument": "$psu",
                 "command": "set_voltage", "args": {"voltage": 1.0}},
                {"type": "query", "instrument": "$dmm",
                 "command": "measure_voltage"},
            ],
        }
        compiled = validate_and_compile(plan, sm, sys_cfg)
        assert compiled.valid, compiled.errors
        ur = compiled.unit_resolution
        assert ur["unit"] == "unit001"
        assert ur["unit_bindings"] == {"psu": "psu001", "dmm": "dmm001"}
        assert ur["effective_bindings"] == {"psu": "psu001", "dmm": "dmm001"}
        assert ur["overridden_roles"] == []
    finally:
        store.close()


def test_experiment_plan_unit_explicit_binding_override(tmp_path):
    """重要: explicit bindings は unit を override する"""
    _, sm, mgr, sys_cfg, store = _setup_with_units(tmp_path)
    try:
        plan = {
            "dsl_version": "0.8",
            "unit": "unit001",
            "bindings": {"dmm": "dmm_backup"},  # unit001.dmm を override
            "steps": [
                {"type": "query", "instrument": "$dmm",
                 "command": "measure_voltage"},
            ],
        }
        compiled = validate_and_compile(plan, sm, sys_cfg)
        assert compiled.valid, compiled.errors
        ur = compiled.unit_resolution
        assert ur["effective_bindings"]["dmm"] == "dmm_backup"
        assert ur["effective_bindings"]["psu"] == "psu001"
        assert "dmm" in ur["overridden_roles"]
        # resolved_instruments も dmm_backup を指す
        assert compiled.resolved_instruments["$dmm"] == "dmm_backup"
    finally:
        store.close()


def test_experiment_plan_unknown_unit_error(tmp_path):
    _, sm, mgr, sys_cfg, store = _setup_with_units(tmp_path)
    try:
        plan = {
            "dsl_version": "0.8",
            "unit": "no_such_unit",
            "steps": [
                {"type": "command", "instrument": "$psu",
                 "command": "set_voltage", "args": {"voltage": 1.0}},
            ],
        }
        compiled = validate_and_compile(plan, sm, sys_cfg)
        assert not compiled.valid
        classes = [e.get("error_class") for e in compiled.errors]
        assert "unknown_unit" in classes
    finally:
        store.close()


def test_experiment_plan_unit_missing_role_error(tmp_path):
    """unit001 に 'temp' role が無いのに $temp を参照 → unit_role_missing"""
    _, sm, mgr, sys_cfg, store = _setup_with_units(tmp_path)
    try:
        plan = {
            "dsl_version": "0.8",
            "unit": "unit001",
            "steps": [
                {"type": "query", "instrument": "$temp",
                 "command": "measure_voltage"},
            ],
        }
        compiled = validate_and_compile(plan, sm, sys_cfg)
        assert not compiled.valid
        classes = [e.get("error_class") for e in compiled.errors]
        assert "unit_role_missing" in classes
    finally:
        store.close()


def test_experiment_plan_unit_resolution_summary_in_dry_run(tmp_path):
    """重要: dry-run/validate の summary に unit_resolution が出る"""
    _, sm, mgr, sys_cfg, store = _setup_with_units(tmp_path)
    try:
        plan = {
            "dsl_version": "0.8",
            "unit": "unit001",
            "bindings": {"dmm": "dmm_backup"},
            "steps": [
                {"type": "query", "instrument": "$dmm",
                 "command": "measure_voltage"},
            ],
        }
        compiled = validate_and_compile(plan, sm, sys_cfg)
        assert compiled.valid
        s = compiled.summary
        assert "unit_resolution" in s
        ur = s["unit_resolution"]
        assert ur["unit"] == "unit001"
        assert "dmm" in ur["overridden_roles"]
        assert ur["effective_bindings"]["dmm"] == "dmm_backup"
        assert ur["explicit_bindings"] == {"dmm": "dmm_backup"}
    finally:
        store.close()


def test_unit_resolution_present_even_when_no_unit(tmp_path):
    """unit 未指定でも unit_resolution は (unit=None で) 出る"""
    _, sm, mgr, sys_cfg, store = _setup_with_units(tmp_path)
    try:
        plan = {
            "dsl_version": "0.8",
            "bindings": {"psu": "psu001"},
            "steps": [
                {"type": "command", "instrument": "$psu",
                 "command": "set_voltage", "args": {"voltage": 1.0}},
            ],
        }
        compiled = validate_and_compile(plan, sm, sys_cfg)
        assert compiled.valid
        ur = compiled.summary["unit_resolution"]
        assert ur["unit"] is None
        assert ur["unit_bindings"] == {}
        assert ur["effective_bindings"] == {"psu": "psu001"}
    finally:
        store.close()


def test_unit_plan_raw_resource_warning(tmp_path):
    """unit 指定 Plan で raw resource を直接使う → raw_resource_used_with_unit warning"""
    _, sm, mgr, sys_cfg, store = _setup_with_units(tmp_path)
    try:
        plan = {
            "dsl_version": "0.8",
            "unit": "unit001",
            "steps": [
                {"type": "query", "instrument": "USB0::0x0::0x0::SN1::INSTR",
                 "command": "measure_voltage"},
            ],
        }
        # raw resource は alias 解決失敗するので unknown_instrument になる可能性大。
        # ただ warning が出るのは resolve できた場合のため、registry に raw resource
        # を入れた状況を作るのは難しい。代わりに alias 名そのもの (instrument
        # 解決成功 + "::" 含む) を確認: psu001 を resource として登録済みのため、
        # alias = "psu001" で「resource そのもの」を直接指定したケースで簡略化。
        # raw_resource_used 系 warning は resolve に "::" を含む resource のみで
        # 出るため、ここでは plan 自体は valid だが unknown_instrument を期待する。
        compiled = validate_and_compile(plan, sm, sys_cfg)
        # USB resource は session_mgr に未登録 -> not_identified
        # (resolve_resource は "::" 含む文字列を素通しするため unknown_instrument
        #  にはならず、後段の get_session で not_identified になる)
        classes = [e.get("error_class") for e in compiled.errors]
        assert "not_identified" in classes
    finally:
        store.close()


def test_unit_plan_raw_resource_warning_actual(tmp_path):
    """実際に raw resource を解決成功させて raw_resource_used_with_unit warning を確認"""
    d = InstrumentDefinition(**yaml.safe_load(YAML_PSU))
    raw = "USB0::0x1234::0x5678::SN::INSTR"
    sessions = {
        raw: InstrumentSession(
            resource_name=raw, idn_response="<x>",
            idn_parsed={}, definition=d,
        ),
        "psu001": InstrumentSession(
            resource_name="psu001", idn_response="<x>",
            idn_parsed={}, definition=d,
        ),
    }

    class _SM:
        def get_session(self, name):
            return sessions.get(name)

    sys_cfg = SystemConfig(
        instruments={
            "psu001": InstrumentBinding(resource="psu001"),
        },
        experiment_units={
            "unit001": ExperimentUnit(bindings={"psu": "psu001"}),
        },
    )
    store = JobStore(db_path=tmp_path / "j.sqlite")
    mgr = JobManager(MagicMock(), _SM(), store=store, system_config=sys_cfg)
    try:
        plan = {
            "dsl_version": "0.8",
            "unit": "unit001",
            "steps": [
                {"type": "query", "instrument": raw,
                 "command": "measure_voltage"},
            ],
        }
        compiled = validate_and_compile(plan, _SM(), sys_cfg)
        warn_classes = [w.get("warning_class") for w in compiled.warnings]
        assert "raw_resource_used_with_unit" in warn_classes
    finally:
        store.close()


# =========================================================
# 2. Template override (apply_template_override 純関数)
# =========================================================


def _template_plan() -> dict:
    return {
        "dsl_version": "0.8",
        "name": "voltage_sweep_basic",
        "unit": "unit001",
        "variables": {"voltage": 1.0, "wait_s": 0.5},
        "bindings": {},
        "steps": [
            {"type": "command", "instrument": "$psu",
             "command": "set_voltage", "args": {"voltage": "{voltage}"}},
            {"type": "wait", "seconds": 0.5},
        ],
    }


def test_template_override_parameters():
    tpl = _template_plan()
    expanded, summary = apply_template_override(tpl, {"parameters": {"voltage": 3.5}})
    assert expanded["variables"]["voltage"] == 3.5
    assert expanded["variables"]["wait_s"] == 0.5
    assert "parameters.voltage" in summary["override_keys"]
    assert summary["override_applied"] is True


def test_template_override_unit():
    tpl = _template_plan()
    expanded, summary = apply_template_override(tpl, {"unit": "unit002"})
    assert expanded["unit"] == "unit002"
    assert "unit" in summary["override_keys"]


def test_template_override_bindings():
    tpl = _template_plan()
    expanded, summary = apply_template_override(
        tpl, {"bindings": {"dmm": "dmm_backup"}},
    )
    assert expanded["bindings"] == {"dmm": "dmm_backup"}
    assert "bindings.dmm" in summary["override_keys"]


def test_template_override_name_and_owner():
    tpl = _template_plan()
    expanded, summary = apply_template_override(
        tpl, {"name": "run_001", "owner": "agent_a"},
    )
    assert expanded["name"] == "run_001"
    assert summary["owner"] == "agent_a"
    # owner は plan 本体に入らない
    assert "owner" not in expanded


def test_template_override_rejects_steps_override():
    """**重要**: steps の直接上書きは拒否される"""
    tpl = _template_plan()
    with pytest.raises(TemplateOverrideError) as ei:
        apply_template_override(tpl, {"steps": []})
    assert "steps" in ei.value.rejected_keys


def test_template_override_rejects_dsl_version_override():
    tpl = _template_plan()
    with pytest.raises(TemplateOverrideError) as ei:
        apply_template_override(tpl, {"dsl_version": "0.9"})
    assert "dsl_version" in ei.value.rejected_keys


def test_template_override_validates_after_merge(tmp_path):
    """override 後の Plan を validate_and_compile に通すと結果が override に依存する"""
    _, sm, mgr, sys_cfg, store = _setup_with_units(tmp_path)
    try:
        tpl = _template_plan()
        expanded, _ = apply_template_override(tpl, {"unit": "unit002"})
        compiled = validate_and_compile(expanded, sm, sys_cfg)
        assert compiled.valid
        assert compiled.summary["unit_resolution"]["unit"] == "unit002"
        # unit002.psu は psu002 を指すはず
        assert compiled.summary["unit_resolution"]["effective_bindings"]["psu"] == "psu002"
    finally:
        store.close()


def test_template_override_empty_override():
    """override が空でも valid に動く"""
    tpl = _template_plan()
    expanded, summary = apply_template_override(tpl, {})
    assert expanded == tpl
    assert summary["override_applied"] is False
    assert summary["override_keys"] == []


def test_template_allowed_keys_constant():
    """互換上、ALLOWED_OVERRIDE_KEYS は v1.x で削減されないこと"""
    for k in ("name", "unit", "bindings", "parameters", "owner"):
        assert k in ALLOWED_OVERRIDE_KEYS


# =========================================================
# 3. start_experiment_job_from_template MCP tool
# =========================================================


@pytest.mark.asyncio
async def test_template_source_recorded_in_job_metadata(tmp_path, monkeypatch):
    """**重要**: template から起動した Job の metadata に template_source が残る"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    _, sm, mgr, sys_cfg, store = _setup_with_units(tmp_path)
    try:
        # template を保存
        tpl = _template_plan()
        store.save_experiment_template(
            name="voltage_sweep_basic", dsl_version="0.8",
            plan=tpl, description="test",
        )

        # MCP tool 経由 (FastMCP)
        from fastmcp import FastMCP
        from lab_executor.tools.dsl import register_tools
        mcp = FastMCP("test")
        register_tools(mcp, sm, mgr)
        tool = await mcp.get_tool("start_experiment_job_from_template")
        result = await tool.run({
            "name": "voltage_sweep_basic",
            "override": {"parameters": {"voltage": 2.5}},
            "owner": "agent_x",
        })
        data = (result.structured_content or {}).get("data") or {}
        assert "job_id" in data
        job_id = data["job_id"]
        assert data["template"]["name"] == "voltage_sweep_basic"
        assert "parameters.voltage" in data["template"]["override_keys"]

        # job metadata の parameters に template_source が入っている
        rec = mgr.get(job_id)
        assert rec.parameters.get("template_source", {}).get("template_name") \
            == "voltage_sweep_basic"

        # experiment_plans.compiled_summary にも入っている
        plan_row = store.get_experiment_plan_for_job(job_id)
        assert plan_row is not None
        cs = plan_row.get("compiled_summary") or {}
        assert cs.get("template_source", {}).get("template_name") \
            == "voltage_sweep_basic"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_template_override_dry_run_mode(tmp_path, monkeypatch):
    """dry_run=True なら Job は始まらず rendered_steps を返す"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    _, sm, mgr, sys_cfg, store = _setup_with_units(tmp_path)
    try:
        tpl = _template_plan()
        store.save_experiment_template(
            name="t1", dsl_version="0.8", plan=tpl,
        )
        from fastmcp import FastMCP
        from lab_executor.tools.dsl import register_tools
        mcp = FastMCP("test")
        register_tools(mcp, sm, mgr)
        tool = await mcp.get_tool("start_experiment_job_from_template")
        result = await tool.run({
            "name": "t1",
            "override": {"parameters": {"voltage": 4.0}},
            "dry_run": True,
            "include_expanded_plan": True,
        })
        data = (result.structured_content or {}).get("data") or {}
        assert data["dry_run"] is True
        assert "job_id" not in data
        assert "rendered_steps" in data
        # expanded_plan も含まれる
        assert data["expanded_plan"]["variables"]["voltage"] == 4.0
    finally:
        store.close()


@pytest.mark.asyncio
async def test_template_override_steps_rejected_via_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    _, sm, mgr, sys_cfg, store = _setup_with_units(tmp_path)
    try:
        store.save_experiment_template(
            name="t2", dsl_version="0.8", plan=_template_plan(),
        )
        from fastmcp import FastMCP
        from lab_executor.tools.dsl import register_tools
        mcp = FastMCP("test")
        register_tools(mcp, sm, mgr)
        tool = await mcp.get_tool("start_experiment_job_from_template")
        result = await tool.run({
            "name": "t2",
            "override": {"steps": []},
        })
        out = result.structured_content or {}
        assert out.get("status") == "error"
        errors = out.get("errors") or []
        assert any(
            e.get("details", {}).get("sub_class") == "template_override_invalid"
            for e in errors
        )
    finally:
        store.close()


@pytest.mark.asyncio
async def test_template_not_found(tmp_path, monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    _, sm, mgr, sys_cfg, store = _setup_with_units(tmp_path)
    try:
        from fastmcp import FastMCP
        from lab_executor.tools.dsl import register_tools
        mcp = FastMCP("test")
        register_tools(mcp, sm, mgr)
        tool = await mcp.get_tool("start_experiment_job_from_template")
        result = await tool.run({"name": "no_such"})
        out = result.structured_content or {}
        assert out.get("status") == "error"
        errors = out.get("errors") or []
        assert any(e.get("error_class") == "not_found" for e in errors)
    finally:
        store.close()


# =========================================================
# 4. JSON schema preview に unit が含まれる
# =========================================================


def test_schema_preview_includes_unit():
    """dsl.schema.json に unit プロパティが含まれる (preview)"""
    p = Path(__file__).parent.parent / "schemas" / "dsl.schema.json"
    schema = json.loads(p.read_text(encoding="utf-8"))
    assert "unit" in schema["properties"]
    assert schema["x-visa-mcp-status"] in ("preview", "stable")


def test_examples_unit_based_validate():
    """examples/unit_based_voltage_sweep/plan.json が JSON として読める"""
    p = Path(__file__).parent.parent / "docs" / "dsl" / "examples" \
        / "unit_based_voltage_sweep" / "plan.json"
    plan = json.loads(p.read_text(encoding="utf-8"))
    assert plan["unit"] == "unit001"
    assert plan["dsl_version"] == "0.8"


def test_examples_template_override_validate():
    base = Path(__file__).parent.parent / "docs" / "dsl" / "examples" \
        / "template_override"
    tpl = json.loads((base / "template.json").read_text(encoding="utf-8"))
    ov = json.loads((base / "override.json").read_text(encoding="utf-8"))
    expanded, summary = apply_template_override(tpl, ov)
    assert summary["override_applied"]
    assert expanded["unit"] == "unit002"
    assert expanded["bindings"]["dmm"] == "dmm_backup_001"
    assert expanded["variables"]["voltage"] == 3.5
