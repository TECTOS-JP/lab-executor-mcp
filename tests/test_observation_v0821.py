"""v0.8.2.1: Observation API レビュー対応テスト

レビュー指摘事項:
- P1-2: timeline pagination cursor 複合化 (next_cursor: {timestamp, event_id})
- P1-3: since/until datetime 比較 + invalid validation error
- P1-4: session_manager public プロパティ経由でアクセス
- P1-5: latest_measurements 範囲拡張 (experiment_plans / target_runs)
- P1-6: partial_failure を job_outcome に分離
- P2-7: monitor_stop_condition_met severity を info に
- P2-8: inspect_state → inspect_job_result rename
"""
from __future__ import annotations
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from lab_executor.job import JobManager, JobStore
from lab_executor.job.state_machine import JobStatus
from lab_executor.models.instrument_def import InstrumentDefinition
from lab_executor.observation import (
    compute_current_phase, compute_job_outcome, event_severity,
    build_run_summary,
)
from lab_visa_mcp.session_manager import InstrumentSession
from lab_executor.system_config import SystemConfig, InstrumentBinding


YAML_PSU = """
metadata: { manufacturer: T, model: PSU, category: power_supply }
commands:
  measure_voltage:
    scpi: "MEAS:VOLT?"
    type: query
    polling_safe: true
state_query:
  voltage:
    command: measure_voltage
    unit: V
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
        def get_session(self, name):
            return sessions.get(name)

    sys_cfg = SystemConfig(
        instruments={
            f"a{i}": InstrumentBinding(resource=f"psu{i}") for i in range(n)
        },
    )
    visa = MagicMock()
    visa.write = AsyncMock(return_value=None)
    visa.query = AsyncMock(return_value="5.0")
    store = JobStore(db_path=tmp_path / "j.sqlite")
    mgr = JobManager(visa, _SM(), store=store, system_config=sys_cfg)
    return mgr, store, sessions


# =========================================================
# P1-4: session_manager public property
# =========================================================


def test_session_manager_public_property(tmp_path):
    """JobManager.session_manager が public でアクセス可能"""
    mgr, store, _ = _setup(tmp_path)
    try:
        sm = mgr.session_manager
        assert sm is not None
        # private と同じインスタンス
        assert sm is mgr._sessions
        # interface に get_session が露出している
        assert hasattr(sm, "get_session")
    finally:
        store.close()


# =========================================================
# P1-6: compute_job_outcome
# =========================================================


def test_compute_job_outcome_success_no_targets():
    assert compute_job_outcome("completed", []) == "success"
    assert compute_job_outcome("completed", None) == "success"


def test_compute_job_outcome_partial_failure():
    target_runs = [
        {"target_id": "s1", "status": "ok"},
        {"target_id": "s2", "status": "failed"},
    ]
    assert compute_job_outcome("completed", target_runs) == "partial_failure"


def test_compute_job_outcome_failure():
    assert compute_job_outcome("failed", []) == "failure"
    assert compute_job_outcome("timeout", []) == "failure"
    assert compute_job_outcome("cancelled", []) == "cancelled"
    assert compute_job_outcome("interrupted", []) == "interrupted"


def test_compute_job_outcome_running_returns_none():
    assert compute_job_outcome("running", []) is None
    assert compute_job_outcome("queued", []) is None


def test_compute_current_phase_partial_failure_via_outcome():
    """job_outcome="partial_failure" + job_status="completed" の組合せで
    current_phase が "partial_failure" になる"""
    phase = compute_current_phase(
        "completed", None, None, None, job_outcome="partial_failure",
    )
    assert phase == "partial_failure"
    # job_outcome=success なら completed
    phase2 = compute_current_phase(
        "completed", None, None, None, job_outcome="success",
    )
    assert phase2 == "completed"


# =========================================================
# P2-7: monitor_stop_condition_met severity → info
# =========================================================


def test_monitor_stop_condition_severity_is_info():
    """v0.8.2 では warning だったが、v0.8.2.1 で info に変更"""
    assert event_severity("monitor_stop_condition_met") == "info"


# =========================================================
# P2-8: inspect_state → inspect_job_result rename
# =========================================================


def test_recommended_action_uses_inspect_job_result(tmp_path):
    mgr, store, _ = _setup(tmp_path)
    try:
        store.create_job("job_pf", "", "psu0", "<recipe>", {})
        store.upsert_target_run("job_pf", "s1", "failed", is_start=False,
                                error={"error_class": "timeout"})
        store.transition_status("job_pf", JobStatus.RUNNING)
        store.transition_status(
            "job_pf", JobStatus.COMPLETED,
            last_step_summary="done",
            result={"summary": {}},
        )
        rec = mgr.get("job_pf")
        target_runs = store.list_target_runs("job_pf")
        summary = build_run_summary(rec.to_dict(), [], target_runs)
        # job_outcome が出る (P1-6)
        assert summary["job_outcome"] == "partial_failure"
        # action 名は inspect_job_result (v0.8.2 までは inspect_state)
        actions = [a["action"] for a in summary["recommended_next_actions"]]
        assert "inspect_job_result" in actions
        assert "inspect_state" not in actions
    finally:
        store.close()


# =========================================================
# P1-2 / P1-3: timeline pagination cursor + datetime 比較
# =========================================================


@pytest.mark.asyncio
async def test_timeline_invalid_since_returns_validation_error(tmp_path,
                                                               monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    from fastmcp import FastMCP
    from lab_executor.tools.observation import register_tools
    mgr, store, _ = _setup(tmp_path)
    try:
        store.create_job("job_t", "", "psu0", "<r>", {})
        mcp = FastMCP("test")
        register_tools(mcp, mgr)
        # mcp.tool registration: 直接 helper 関数を取得するのは困難なので
        # FastMCP の _tool_manager から取り出す or 直接 import 不可。
        # 代わりに tool 関数を再構築せず、register_tools 経由で
        # FastMCP.get_tool で取得する。
        tool = await mcp.get_tool("get_experiment_timeline")
        result = await tool.run({
            "job_id": "job_t", "since": "not-an-iso-timestamp",
        })
        # FastMCP の Tool.run は ToolResult を返す。content/structured_content から取得
        data = result.structured_content or {}
        assert data.get("status") == "error"
        errors = data.get("errors") or []
        assert len(errors) >= 1
        assert errors[0]["error_class"] == "validation"
        assert errors[0]["details"]["sub_class"] == "invalid_since_timestamp"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_timeline_invalid_until_returns_validation_error(tmp_path,
                                                               monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    from fastmcp import FastMCP
    from lab_executor.tools.observation import register_tools
    mgr, store, _ = _setup(tmp_path)
    try:
        store.create_job("job_t2", "", "psu0", "<r>", {})
        mcp = FastMCP("test")
        register_tools(mcp, mgr)
        tool = await mcp.get_tool("get_experiment_timeline")
        result = await tool.run({"job_id": "job_t2", "until": "xxx"})
        data = result.structured_content or {}
        assert data.get("status") == "error"
        errors = data.get("errors") or []
        assert errors[0]["details"]["sub_class"] == "invalid_until_timestamp"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_timeline_pagination_returns_next_cursor(tmp_path, monkeypatch):
    """next_since は廃止 / next_cursor: {timestamp, event_id} を返す"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    from fastmcp import FastMCP
    from lab_executor.tools.observation import register_tools
    mgr, store, _ = _setup(tmp_path)
    try:
        store.create_job("job_pc", "", "psu0", "<r>", {})
        # 10 件記録
        for i in range(10):
            store.record_event("job_pc", "step_started",
                               payload={"command": f"c{i}"})
        mcp = FastMCP("test")
        register_tools(mcp, mgr)
        tool = await mcp.get_tool("get_experiment_timeline")
        result = await tool.run({"job_id": "job_pc", "limit": 3})
        data = result.structured_content or {}
        pg = data["data"]["pagination"]
        assert pg["returned"] == 3
        assert pg["has_more"] is True
        # next_since は無い、next_cursor が出る
        assert "next_since" not in pg
        assert pg["next_cursor"] is not None
        assert "timestamp" in pg["next_cursor"]
        assert "event_id" in pg["next_cursor"]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_timeline_pagination_no_more_when_within_limit(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    from fastmcp import FastMCP
    from lab_executor.tools.observation import register_tools
    mgr, store, _ = _setup(tmp_path)
    try:
        store.create_job("job_pcc", "", "psu0", "<r>", {})
        for i in range(2):
            store.record_event("job_pcc", "step_started",
                               payload={"command": f"c{i}"})
        mcp = FastMCP("test")
        register_tools(mcp, mgr)
        tool = await mcp.get_tool("get_experiment_timeline")
        result = await tool.run({"job_id": "job_pcc", "limit": 100})
        data = result.structured_content or {}
        pg = data["data"]["pagination"]
        assert pg["has_more"] is False
        assert pg["next_cursor"] is None
    finally:
        store.close()


# =========================================================
# P1-6: get_job_summary returns job_outcome
# =========================================================


def test_build_run_summary_includes_job_outcome_success(tmp_path):
    mgr, store, _ = _setup(tmp_path)
    try:
        store.create_job("job_s", "", "psu0", "<r>", {})
        store.transition_status("job_s", JobStatus.RUNNING)
        store.transition_status(
            "job_s", JobStatus.COMPLETED,
            last_step_summary="done", result={"success": True},
        )
        rec = mgr.get("job_s")
        summary = build_run_summary(rec.to_dict(), [], [])
        assert summary["job_outcome"] == "success"
    finally:
        store.close()


def test_build_run_summary_includes_job_outcome_partial_failure(tmp_path):
    mgr, store, _ = _setup(tmp_path)
    try:
        store.create_job("job_pf2", "", "psu0", "<r>", {})
        store.upsert_target_run("job_pf2", "s1", "ok", is_start=False)
        store.upsert_target_run("job_pf2", "s2", "failed", is_start=False,
                                error={"error_class": "timeout"})
        store.transition_status("job_pf2", JobStatus.RUNNING)
        store.transition_status(
            "job_pf2", JobStatus.COMPLETED,
            last_step_summary="2/2 success?", result={},
        )
        rec = mgr.get("job_pf2")
        target_runs = store.list_target_runs("job_pf2")
        summary = build_run_summary(rec.to_dict(), [], target_runs)
        assert summary["job_outcome"] == "partial_failure"
        # job_status はあくまで completed
        assert summary["job_status"] == "completed"
    finally:
        store.close()
