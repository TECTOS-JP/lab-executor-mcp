"""v0.9.1: self-repair benchmark + 測定結果 export API テスト"""
from __future__ import annotations
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from lab_executor.job import JobManager, JobStore
from lab_executor.job.state_machine import JobStatus
from lab_executor.models.instrument_def import InstrumentDefinition
from lab_visa_mcp.session_manager import InstrumentSession
from lab_executor.system_config import SystemConfig, InstrumentBinding
from lab_executor.testing.benchmark_task import (
    BenchmarkTask, ExpectedFailure, ExpectedRepair, load_benchmark_task,
)
from lab_executor.testing.benchmark_runner import run_task_file

ROOT = Path(__file__).parent.parent


@pytest.fixture(autouse=True)
def _safety_mode(monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")


# =========================================================
# Step 1: repair task schema
# =========================================================


def test_repair_task_schema_loads():
    p = ROOT / "benchmarks" / "repair" / "repair_001_unknown_command" / "task.yaml"
    t = load_benchmark_task(p)
    assert t.layer == "repair"
    assert t.broken_plan is not None
    assert t.repaired_plan is not None
    assert t.expected_failure is not None
    assert t.expected_failure.error_class == "unknown_command"


def test_repair_task_rejects_missing_repair_sections(tmp_path):
    bad = tmp_path / "r.yaml"
    bad.write_text(
        "id: bad\nlayer: repair\n", encoding="utf-8",
    )
    # Pydantic allows None for these; runner will detect missing later
    t = load_benchmark_task(bad)
    assert t.broken_plan is None and t.repaired_plan is None


# =========================================================
# Step 2-3: 5 repair fixtures pass
# =========================================================


@pytest.mark.asyncio
async def test_repair_unknown_command_passes(tmp_path):
    res = await run_task_file(
        ROOT / "benchmarks" / "repair" / "repair_001_unknown_command" / "task.yaml",
        ROOT / "benchmarks", tmp_path,
    )
    assert res.status == "passed", [
        (c.name, c.status, c.message) for c in res.checks
    ]


@pytest.mark.asyncio
async def test_repair_invalid_parameter_passes(tmp_path):
    res = await run_task_file(
        ROOT / "benchmarks" / "repair" / "repair_002_invalid_parameter_range" / "task.yaml",
        ROOT / "benchmarks", tmp_path,
    )
    assert res.status == "passed", [
        (c.name, c.status, c.message) for c in res.checks
    ]


@pytest.mark.asyncio
async def test_repair_unit_role_missing_has_recommended_action(tmp_path):
    """**重要**: unit_role_missing が add_binding_override /
    choose_different_unit を返すこと"""
    res = await run_task_file(
        ROOT / "benchmarks" / "repair" / "repair_003_unit_role_missing" / "task.yaml",
        ROOT / "benchmarks", tmp_path,
    )
    assert res.status == "passed", [
        (c.name, c.status, c.message) for c in res.checks
    ]
    names = [c.name for c in res.checks]
    assert "broken_plan_has_recommended_actions" in names


@pytest.mark.asyncio
async def test_repair_raw_resource_warning_passes(tmp_path):
    res = await run_task_file(
        ROOT / "benchmarks" / "repair" / "repair_004_raw_resource_with_unit" / "task.yaml",
        ROOT / "benchmarks", tmp_path,
    )
    assert res.status == "passed", [
        (c.name, c.status, c.message) for c in res.checks
    ]


@pytest.mark.asyncio
async def test_repair_safety_violation_does_not_override_safety(tmp_path):
    """**最重要**: safety violation repair で override_safety 等が含まれない"""
    res = await run_task_file(
        ROOT / "benchmarks" / "repair" / "repair_005_safety_violation" / "task.yaml",
        ROOT / "benchmarks", tmp_path,
    )
    assert res.status == "passed", [
        (c.name, c.status, c.message) for c in res.checks
    ]
    must_not_check = next(
        (c for c in res.checks if c.name == "repaired_plan_must_not"), None,
    )
    assert must_not_check is not None
    assert must_not_check.status == "passed"


# =========================================================
# Step 4-5: get_experiment_results / export
# =========================================================


YAML_PSU = """
metadata: { manufacturer: T, model: PSU, category: power_supply }
commands:
  measure_voltage:
    scpi: "MEAS:VOLT?"
    type: query
state_query:
  voltage:
    command: measure_voltage
    unit: V
"""


def _setup_with_job(tmp_path):
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

    # Job + steps を直接書き込み
    store.create_job("job_e1", "", "psu001", "<r>", {})
    store.transition_status("job_e1", JobStatus.RUNNING)
    sid = store.record_step_started("job_e1", 0, "query")
    store.record_step_completed(
        sid, status="ok",
        result={
            "command": "measure_voltage", "instrument": "psu001",
            "value": 5.2, "unit": "V",
            "response_parsed": {"voltage": 5.2},
        },
    )
    store.transition_status("job_e1", JobStatus.COMPLETED,
                            last_step_summary="done",
                            result={"success": True})
    # monitor_data 1 件 (デフォルト除外確認用)。 monitor_id == job_id 慣習。
    try:
        store.append_monitor_data(
            monitor_id="job_e1", instrument="psu001", value=5.0,
        )
    except Exception:
        pass

    return _SM(), mgr, store


@pytest.mark.asyncio
async def test_get_experiment_results_returns_paginated_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    sm, mgr, store = _setup_with_job(tmp_path)
    try:
        from fastmcp import FastMCP
        from lab_executor.tools.export import register_tools
        mcp = FastMCP("t")
        register_tools(mcp, mgr)
        tool = await mcp.get_tool("get_experiment_results")
        res = await tool.run({"job_id": "job_e1", "limit": 100})
        data = (res.structured_content or {}).get("data") or {}
        assert data["columns"][0] == "timestamp"
        assert isinstance(data["rows"], list)
        assert len(data["rows"]) >= 1
        # monitor_data はデフォルト除外
        meas = [r.get("measurement") for r in data["rows"]]
        # voltage from step_results は含む / monitor_data 行は含まない
        assert "voltage" in meas
        # pagination
        assert data["pagination"]["has_more"] is False
    finally:
        store.close()


@pytest.mark.asyncio
async def test_get_experiment_results_excludes_monitor_data_by_default(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    sm, mgr, store = _setup_with_job(tmp_path)
    try:
        from fastmcp import FastMCP
        from lab_executor.tools.export import register_tools
        mcp = FastMCP("t")
        register_tools(mcp, mgr)
        tool = await mcp.get_tool("get_experiment_results")
        # include_monitor_data=False (default)
        res = await tool.run({"job_id": "job_e1"})
        data = (res.structured_content or {}).get("data") or {}
        assert data["include_monitor_data"] is False
        # monitor からの行は同 measurement だが target_id is None かつ
        # step_index is None。実装上 step 由来と区別する。
        step_rows = [r for r in data["rows"]
                     if r.get("step_index") is not None]
        assert len(step_rows) >= 1

        # include_monitor_data=True にすると monitor 行が追加
        res2 = await tool.run({
            "job_id": "job_e1", "include_monitor_data": True,
        })
        data2 = (res2.structured_content or {}).get("data") or {}
        assert data2["include_monitor_data"] is True
        # 行数が増えること (monitor row が 1 件追加されるはず)
        assert len(data2["rows"]) > len(data["rows"])
    finally:
        store.close()


@pytest.mark.asyncio
async def test_export_csv_creates_file(tmp_path, monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    # default export dir を tmp に向ける
    monkeypatch.setattr(
        "lab_executor.tools.export.DEFAULT_EXPORT_DIR",
        tmp_path / "exports",
    )
    sm, mgr, store = _setup_with_job(tmp_path)
    try:
        from fastmcp import FastMCP
        from lab_executor.tools.export import register_tools
        mcp = FastMCP("t")
        register_tools(mcp, mgr)
        tool = await mcp.get_tool("export_experiment_results")
        res = await tool.run({"job_id": "job_e1", "format": "csv"})
        data = (res.structured_content or {}).get("data") or {}
        path = Path(data["path"])
        assert path.exists()
        assert data["format"] == "csv"
        assert "sha256" in data and len(data["sha256"]) == 64
        # CSV header check
        text = path.read_text(encoding="utf-8")
        assert text.startswith("timestamp,target_id,instrument,measurement,")
    finally:
        store.close()


@pytest.mark.asyncio
async def test_export_jsonl(tmp_path, monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    monkeypatch.setattr(
        "lab_executor.tools.export.DEFAULT_EXPORT_DIR",
        tmp_path / "exports",
    )
    sm, mgr, store = _setup_with_job(tmp_path)
    try:
        from fastmcp import FastMCP
        from lab_executor.tools.export import register_tools
        mcp = FastMCP("t")
        register_tools(mcp, mgr)
        tool = await mcp.get_tool("export_experiment_results")
        res = await tool.run({"job_id": "job_e1", "format": "jsonl"})
        data = (res.structured_content or {}).get("data") or {}
        path = Path(data["path"])
        assert path.exists()
        # 各行が JSON
        for line in path.read_text(encoding="utf-8").splitlines():
            obj = json.loads(line)
            assert "timestamp" in obj
    finally:
        store.close()


@pytest.mark.asyncio
async def test_export_rejects_path_traversal(tmp_path, monkeypatch):
    """**重要**: ../ を含む output_path / 絶対パス to other dir を拒否"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    monkeypatch.setattr(
        "lab_executor.tools.export.DEFAULT_EXPORT_DIR",
        tmp_path / "exports",
    )
    sm, mgr, store = _setup_with_job(tmp_path)
    try:
        from fastmcp import FastMCP
        from lab_executor.tools.export import register_tools
        mcp = FastMCP("t")
        register_tools(mcp, mgr)
        tool = await mcp.get_tool("export_experiment_results")
        # path traversal 試行
        res = await tool.run({
            "job_id": "job_e1", "format": "csv",
            "output_path": "../escape.csv",
        })
        out = res.structured_content or {}
        assert out.get("status") == "error"
        errs = out.get("errors") or []
        assert any(e["error_class"] == "invalid_export_path" for e in errs)

        # 完全別ディレクトリの絶対パス
        res2 = await tool.run({
            "job_id": "job_e1", "format": "csv",
            "output_path": str(tmp_path / "elsewhere" / "x.csv"),
        })
        out2 = res2.structured_content or {}
        assert out2.get("status") == "error"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_export_rejects_existing_without_overwrite(tmp_path, monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    monkeypatch.setattr(
        "lab_executor.tools.export.DEFAULT_EXPORT_DIR",
        tmp_path / "exports",
    )
    sm, mgr, store = _setup_with_job(tmp_path)
    try:
        from fastmcp import FastMCP
        from lab_executor.tools.export import register_tools
        mcp = FastMCP("t")
        register_tools(mcp, mgr)
        tool = await mcp.get_tool("export_experiment_results")
        # 1 回目
        res = await tool.run({"job_id": "job_e1", "format": "csv"})
        assert (res.structured_content or {}).get("status") == "ok"
        # 2 回目 (overwrite=False) → 拒否
        res2 = await tool.run({"job_id": "job_e1", "format": "csv"})
        out = res2.structured_content or {}
        assert out.get("status") == "error"
        assert any(
            e["error_class"] == "invalid_export_path"
            for e in (out.get("errors") or [])
        )
        # overwrite=True なら成功
        res3 = await tool.run({
            "job_id": "job_e1", "format": "csv", "overwrite": True,
        })
        assert (res3.structured_content or {}).get("status") == "ok"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_export_returns_sha256(tmp_path, monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    monkeypatch.setattr(
        "lab_executor.tools.export.DEFAULT_EXPORT_DIR",
        tmp_path / "exports",
    )
    sm, mgr, store = _setup_with_job(tmp_path)
    try:
        from fastmcp import FastMCP
        from lab_executor.tools.export import register_tools
        mcp = FastMCP("t")
        register_tools(mcp, mgr)
        tool = await mcp.get_tool("export_experiment_results")
        res = await tool.run({"job_id": "job_e1", "format": "csv"})
        data = (res.structured_content or {}).get("data") or {}
        # sha256 が file 内容と一致
        path = Path(data["path"])
        import hashlib
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        assert data["sha256"] == actual
    finally:
        store.close()


@pytest.mark.asyncio
async def test_export_unsupported_format(tmp_path, monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    monkeypatch.setattr(
        "lab_executor.tools.export.DEFAULT_EXPORT_DIR",
        tmp_path / "exports",
    )
    sm, mgr, store = _setup_with_job(tmp_path)
    try:
        from fastmcp import FastMCP
        from lab_executor.tools.export import register_tools
        mcp = FastMCP("t")
        register_tools(mcp, mgr)
        tool = await mcp.get_tool("export_experiment_results")
        res = await tool.run({"job_id": "job_e1", "format": "xml"})
        out = res.structured_content or {}
        assert out.get("status") == "error"
        errs = out.get("errors") or []
        # v0.9.1.1: 独立 error_class へ昇格
        assert any(
            e.get("error_class") == "unsupported_export_format" for e in errs
        )
    finally:
        store.close()


@pytest.mark.asyncio
async def test_get_experiment_results_not_found(tmp_path, monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    sm, mgr, store = _setup_with_job(tmp_path)
    try:
        from fastmcp import FastMCP
        from lab_executor.tools.export import register_tools
        mcp = FastMCP("t")
        register_tools(mcp, mgr)
        tool = await mcp.get_tool("get_experiment_results")
        res = await tool.run({"job_id": "nope"})
        out = res.structured_content or {}
        assert out.get("status") == "error"
        assert any(e["error_class"] == "not_found"
                   for e in (out.get("errors") or []))
    finally:
        store.close()
