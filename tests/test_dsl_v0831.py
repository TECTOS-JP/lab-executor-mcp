"""v0.8.3.1: external review response (P1/P2)

レビュー指摘:
- P1-2: apply_template_override が shallow copy → deepcopy 化
- P2-8: start_experiment_job_from_template レスポンスに owner 明示
        + override.owner 指定時に Job owner として優先される
"""
from __future__ import annotations
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from lab_executor.dsl.template import apply_template_override
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


def _setup(tmp_path):
    d = InstrumentDefinition(**yaml.safe_load(YAML_PSU))
    sessions = {
        "psu001": InstrumentSession(
            resource_name="psu001", idn_response="<x>",
            idn_parsed={}, definition=d,
        ),
    }

    class _SM:
        def get_session(self, name):
            return sessions.get(name)

    sys_cfg = SystemConfig(
        instruments={"psu001": InstrumentBinding(resource="psu001")},
        experiment_units={
            "unit001": ExperimentUnit(bindings={"psu": "psu001"}),
        },
    )
    visa = MagicMock()
    visa.write = AsyncMock(return_value=None)
    visa.query = AsyncMock(return_value="5.0")
    store = JobStore(db_path=tmp_path / "j.sqlite")
    mgr = JobManager(visa, _SM(), store=store, system_config=sys_cfg)
    return _SM(), mgr, store


def _tpl() -> dict:
    return {
        "dsl_version": "0.8",
        "name": "tpl",
        "unit": "unit001",
        "variables": {"voltage": 1.0},
        "bindings": {},
        "steps": [
            {"type": "command", "instrument": "$psu",
             "command": "set_voltage", "args": {"voltage": "{voltage}"}},
        ],
    }


# =========================================================
# P1: deepcopy で template に副作用が出ない
# =========================================================


def test_apply_template_override_is_deepcopy():
    """expanded を mutate しても元 template が変化しないこと"""
    tpl = _tpl()
    expanded, _ = apply_template_override(
        tpl, {"parameters": {"voltage": 2.5}},
    )
    # expanded を破壊的に変更
    expanded["steps"].append({"type": "wait", "seconds": 0.1})
    expanded["variables"]["voltage"] = 99.9
    expanded["bindings"]["psu"] = "spoiled"
    # 元 template は無傷
    assert len(tpl["steps"]) == 1
    assert tpl["variables"]["voltage"] == 1.0
    assert tpl["bindings"] == {}


def test_apply_template_override_steps_not_shared():
    """expanded.steps と template.steps が **異なる list オブジェクト** であること"""
    tpl = _tpl()
    expanded, _ = apply_template_override(tpl, {})
    assert expanded["steps"] is not tpl["steps"]
    assert expanded["steps"][0] is not tpl["steps"][0]


# =========================================================
# P2: start_experiment_job_from_template レスポンスに owner
# =========================================================


@pytest.mark.asyncio
async def test_response_includes_effective_owner(tmp_path, monkeypatch):
    """owner 引数で渡した値が data.owner として返される"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    sm, mgr, store = _setup(tmp_path)
    try:
        store.save_experiment_template(
            name="t_owner", dsl_version="0.8", plan=_tpl(),
        )
        from fastmcp import FastMCP
        from lab_executor.tools.dsl import register_tools
        mcp = FastMCP("test")
        register_tools(mcp, sm, mgr)
        tool = await mcp.get_tool("start_experiment_job_from_template")
        result = await tool.run({
            "name": "t_owner", "owner": "agent_caller",
        })
        data = (result.structured_content or {}).get("data") or {}
        assert data.get("owner") == "agent_caller"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_override_owner_takes_precedence(tmp_path, monkeypatch):
    """override.owner が指定されたら関数引数 owner より優先される"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    sm, mgr, store = _setup(tmp_path)
    try:
        store.save_experiment_template(
            name="t_ovow", dsl_version="0.8", plan=_tpl(),
        )
        from fastmcp import FastMCP
        from lab_executor.tools.dsl import register_tools
        mcp = FastMCP("test")
        register_tools(mcp, sm, mgr)
        tool = await mcp.get_tool("start_experiment_job_from_template")
        result = await tool.run({
            "name": "t_ovow",
            "owner": "agent_caller",
            "override": {"owner": "override_owner"},
        })
        data = (result.structured_content or {}).get("data") or {}
        assert data.get("owner") == "override_owner"
        # Job 側にも反映 (JobRecord.owner)
        rec = mgr.get(data["job_id"])
        assert rec.owner == "override_owner"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_dry_run_does_not_modify_template(tmp_path, monkeypatch):
    """dry_run + include_expanded_plan で expanded を返した後、template が無事"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    sm, mgr, store = _setup(tmp_path)
    try:
        original = _tpl()
        store.save_experiment_template(
            name="t_dr", dsl_version="0.8", plan=original,
        )
        from fastmcp import FastMCP
        from lab_executor.tools.dsl import register_tools
        mcp = FastMCP("test")
        register_tools(mcp, sm, mgr)
        tool = await mcp.get_tool("start_experiment_job_from_template")
        result = await tool.run({
            "name": "t_dr",
            "override": {"parameters": {"voltage": 4.0}},
            "dry_run": True,
            "include_expanded_plan": True,
        })
        data = (result.structured_content or {}).get("data") or {}
        expanded = data["expanded_plan"]
        # template DB 側はそのまま
        reloaded = store.get_experiment_template("t_dr")
        assert reloaded["plan"]["variables"]["voltage"] == 1.0
        assert expanded["variables"]["voltage"] == 4.0
    finally:
        store.close()
