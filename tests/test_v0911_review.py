"""v0.9.1.1: external review response (P1)

- docs/benchmark_repair.md + docs/result_export.md 存在
- repair_006_partial_failure_retry が pass
- unsupported_export_format が独立 error_class
- invalid_export_path に recommended_next_actions
"""
from __future__ import annotations
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from lab_executor.job import JobManager, JobStore
from lab_executor.job.state_machine import JobStatus
from lab_executor.models.instrument_def import InstrumentDefinition
from visa_mcp.session_manager import InstrumentSession
from lab_executor.system_config import SystemConfig, InstrumentBinding
from lab_executor.testing.benchmark_runner import run_task_file

ROOT = Path(__file__).parent.parent


@pytest.fixture(autouse=True)
def _safety(monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")


# P1-2,3
def test_benchmark_repair_docs_exist():
    p = ROOT / "docs" / "benchmark_repair.md"
    assert p.exists()
    text = p.read_text(encoding="utf-8")
    assert "broken_plan" in text and "repaired_plan" in text
    assert "must_not" in text


def test_result_export_docs_exist():
    p = ROOT / "docs" / "result_export.md"
    assert p.exists()
    text = p.read_text(encoding="utf-8")
    assert "get_experiment_results" in text
    assert "export_experiment_results" in text
    assert "invalid_export_path" in text


# P1-4
@pytest.mark.asyncio
async def test_repair_006_partial_failure_retry_passes(tmp_path):
    res = await run_task_file(
        ROOT / "benchmarks" / "repair"
            / "repair_006_partial_failure_retry" / "task.yaml",
        ROOT / "benchmarks", tmp_path,
    )
    assert res.status == "passed", [
        (c.name, c.status, c.message) for c in res.checks
    ]
    must_not_check = next(
        (c for c in res.checks if c.name == "repaired_plan_must_not"), None,
    )
    assert must_not_check is not None and must_not_check.status == "passed"


# P1-6
YAML_PSU = """
metadata: { manufacturer: T, model: PSU, category: power_supply }
commands:
  measure_voltage:
    scpi: "MEAS:VOLT?"
    type: query
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
    )
    visa = MagicMock()
    visa.write = AsyncMock(return_value=None)
    visa.query = AsyncMock(return_value="5.0")
    store = JobStore(db_path=tmp_path / "j.sqlite")
    mgr = JobManager(visa, _SM(), store=store, system_config=sys_cfg)
    store.create_job("job_x", "", "psu001", "<r>", {})
    store.transition_status("job_x", JobStatus.RUNNING)
    store.transition_status("job_x", JobStatus.COMPLETED,
                            last_step_summary="ok",
                            result={"success": True})
    return _SM(), mgr, store


@pytest.mark.asyncio
async def test_unsupported_export_format_is_independent_error_class(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr("lab_executor.tools.export.DEFAULT_EXPORT_DIR",
                        tmp_path / "exports")
    sm, mgr, store = _setup(tmp_path)
    try:
        from fastmcp import FastMCP
        from lab_executor.tools.export import register_tools
        mcp = FastMCP("t")
        register_tools(mcp, mgr)
        tool = await mcp.get_tool("export_experiment_results")
        res = await tool.run({"job_id": "job_x", "format": "xml"})
        out = res.structured_content or {}
        errs = out.get("errors") or []
        # 独立 error_class へ昇格 (sub_class ではない)
        assert any(e["error_class"] == "unsupported_export_format" for e in errs)
        # recommended_next_actions が含まれる
        target = next(e for e in errs
                      if e["error_class"] == "unsupported_export_format")
        actions = [a["action"] for a in
                   (target.get("recommended_next_actions") or [])]
        assert "use_csv_format" in actions or "use_jsonl_format" in actions
    finally:
        store.close()


# P1-8
@pytest.mark.asyncio
async def test_invalid_export_path_returns_recommended_actions(
    tmp_path, monkeypatch,
):
    """既存ファイル拒否時に set_overwrite_true / choose_different_output_path
    が返ること"""
    monkeypatch.setattr("lab_executor.tools.export.DEFAULT_EXPORT_DIR",
                        tmp_path / "exports")
    sm, mgr, store = _setup(tmp_path)
    try:
        from fastmcp import FastMCP
        from lab_executor.tools.export import register_tools
        mcp = FastMCP("t")
        register_tools(mcp, mgr)
        tool = await mcp.get_tool("export_experiment_results")
        # 1 回目: 成功
        r1 = await tool.run({"job_id": "job_x", "format": "csv"})
        assert (r1.structured_content or {}).get("status") == "ok"
        # 2 回目: invalid_export_path + recommended_next_actions
        r2 = await tool.run({"job_id": "job_x", "format": "csv"})
        out = r2.structured_content or {}
        errs = out.get("errors") or []
        target = next(e for e in errs
                      if e["error_class"] == "invalid_export_path")
        actions = [a["action"] for a in
                   (target.get("recommended_next_actions") or [])]
        assert "set_overwrite_true" in actions
        assert "choose_different_output_path" in actions
    finally:
        store.close()


@pytest.mark.asyncio
async def test_invalid_export_path_traversal_returns_actions(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr("lab_executor.tools.export.DEFAULT_EXPORT_DIR",
                        tmp_path / "exports")
    sm, mgr, store = _setup(tmp_path)
    try:
        from fastmcp import FastMCP
        from lab_executor.tools.export import register_tools
        mcp = FastMCP("t")
        register_tools(mcp, mgr)
        tool = await mcp.get_tool("export_experiment_results")
        r = await tool.run({
            "job_id": "job_x", "format": "csv",
            "output_path": "../escape.csv",
        })
        out = r.structured_content or {}
        errs = out.get("errors") or []
        # invalid_export_path が独立 error_class、message には base_dir 情報
        assert any(e["error_class"] == "invalid_export_path" for e in errs)
    finally:
        store.close()
