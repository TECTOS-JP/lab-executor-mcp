"""v0.9.0: resume_job MVP テスト"""
from __future__ import annotations
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from lab_executor.job import JobManager, JobStore
from lab_executor.job.state_machine import JobStatus
from lab_executor.models.instrument_def import InstrumentDefinition
from visa_mcp.session_manager import InstrumentSession
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
    return _SM(), mgr, store


def _plan() -> dict:
    return {
        "dsl_version": "0.8",
        "name": "resumable",
        "bindings": {"psu": "psu001"},
        "steps": [
            {"type": "command", "instrument": "$psu", "command": "set_voltage",
             "args": {"voltage": 1.0}},
            {"type": "command", "instrument": "$psu", "command": "set_voltage",
             "args": {"voltage": 2.0}},
            {"type": "command", "instrument": "$psu", "command": "set_voltage",
             "args": {"voltage": 3.0}},
        ],
    }


def _seed_interrupted_job(store: JobStore, job_id: str) -> None:
    """interrupted 状態の Job + experiment_plan を直接書き込む"""
    import uuid
    store.create_job(
        job_id=job_id, owner="agent",
        resource_name="psu001", recipe="<dsl:resumable>",
        parameters={"plan_id": f"plan_{uuid.uuid4().hex[:8]}"},
    )
    store.transition_status(
        job_id, JobStatus.RUNNING, current_step_index=1,
        last_step_summary="step 1 done",
    )
    store.transition_status(
        job_id, JobStatus.INTERRUPTED,
        last_step_summary="server restart",
    )
    store.save_experiment_plan(
        plan_id=f"plan_{job_id}", job_id=job_id,
        name="resumable", dsl_version="0.8",
        original_plan=_plan(),
        compiled_summary={"used_resources": ["psu001"]},
    )


# =========================================================
# resume_job: 入力 validation
# =========================================================


@pytest.mark.asyncio
async def test_resume_rejects_completed_job(tmp_path, monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    sm, mgr, store = _setup(tmp_path)
    try:
        store.create_job("job_c", "", "psu001", "<r>", {})
        store.transition_status("job_c", JobStatus.RUNNING)
        store.transition_status("job_c", JobStatus.COMPLETED,
                                last_step_summary="ok",
                                result={"success": True})
        res = await mgr.resume_job("job_c")
        assert res["resume_ready"] is False
        classes = [e["error_class"] for e in res.get("errors", [])]
        assert "resume_not_allowed" in classes
    finally:
        store.close()


@pytest.mark.asyncio
async def test_resume_rejects_running_job(tmp_path, monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    sm, mgr, store = _setup(tmp_path)
    try:
        store.create_job("job_r", "", "psu001", "<r>", {})
        store.transition_status("job_r", JobStatus.RUNNING)
        res = await mgr.resume_job("job_r")
        assert res["resume_ready"] is False
        assert any(
            e["error_class"] == "resume_not_allowed" for e in res.get("errors", [])
        )
    finally:
        store.close()


@pytest.mark.asyncio
async def test_resume_not_found(tmp_path, monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    sm, mgr, store = _setup(tmp_path)
    try:
        res = await mgr.resume_job("nope")
        assert res["resume_ready"] is False
        assert any(e["error_class"] == "not_found"
                   for e in res.get("errors", []))
    finally:
        store.close()


@pytest.mark.asyncio
async def test_resume_requires_experiment_plan(tmp_path, monkeypatch):
    """experiment_plan が無い (DSL Job 以外) は resume 不可"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    sm, mgr, store = _setup(tmp_path)
    try:
        store.create_job("job_norec", "", "psu001", "<recipe>", {})
        store.transition_status("job_norec", JobStatus.RUNNING)
        store.transition_status("job_norec", JobStatus.INTERRUPTED,
                                last_step_summary="x")
        res = await mgr.resume_job("job_norec", from_step=1)
        assert res["resume_ready"] is False
    finally:
        store.close()


# =========================================================
# resume_job: from_step / dry_run
# =========================================================


@pytest.mark.asyncio
async def test_resume_requires_explicit_from_step(tmp_path, monkeypatch):
    """**重要**: from_step=None なら Job を起動せず suggested を返す"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    sm, mgr, store = _setup(tmp_path)
    try:
        _seed_interrupted_job(store, "job_i")
        res = await mgr.resume_job("job_i")  # from_step=None
        assert res["resume_ready"] is False
        assert res["suggested_from_step"] == 2  # current_step_index=1 + 1
        assert any(
            (e.get("details") or {}).get("requires_explicit_from_step")
            for e in res.get("errors", [])
        )
    finally:
        store.close()


@pytest.mark.asyncio
async def test_resume_dry_run_returns_steps_to_execute(tmp_path, monkeypatch):
    """**重要**: dry_run=True で Job 起動せず remaining steps を返す"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    sm, mgr, store = _setup(tmp_path)
    try:
        _seed_interrupted_job(store, "job_dr")
        res = await mgr.resume_job("job_dr", from_step=2, dry_run=True)
        assert res["resume_ready"] is True
        assert res["requested_from_step"] == 2
        assert len(res["steps_to_execute"]) == 1  # steps[2] 1 個
        assert res["steps_to_execute"][0]["step_index"] == 2
        # Job は起動されていない
        assert "resumed_job_id" not in res
        # warning に side effect 注意
        assert any(
            w.get("warning_class") == "resume_may_repeat_side_effects"
            for w in res.get("warnings", [])
        )
    finally:
        store.close()


@pytest.mark.asyncio
async def test_resume_rejects_invalid_from_step(tmp_path, monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    sm, mgr, store = _setup(tmp_path)
    try:
        _seed_interrupted_job(store, "job_inv")
        res = await mgr.resume_job("job_inv", from_step=999, dry_run=False)
        assert res["resume_ready"] is False
        assert any(e["error_class"] == "validation"
                   for e in res.get("errors", []))
    finally:
        store.close()


# =========================================================
# resume_job: 実行 (新規 Job が作られる)
# =========================================================


@pytest.mark.asyncio
async def test_resume_creates_new_job_with_resumed_from_job_id(
    tmp_path, monkeypatch,
):
    """**重要**: 新 Job が作られ、元 Job とは別 ID。"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    sm, mgr, store = _setup(tmp_path)
    try:
        _seed_interrupted_job(store, "job_orig")
        res = await mgr.resume_job("job_orig", from_step=2, dry_run=False)
        assert res["resume_ready"] is True
        assert "resumed_job_id" in res
        new_id = res["resumed_job_id"]
        assert new_id != "job_orig"
        # 新 Job の parameters に resume marker が記録される
        new_rec = mgr.get(new_id)
        ts = new_rec.parameters.get("template_source") or {}
        assert ts.get("resume", {}).get("resumed_from_job_id") == "job_orig"
        # 元 Job は変化していない
        orig = mgr.get("job_orig")
        assert orig.status == JobStatus.INTERRUPTED
    finally:
        store.close()


@pytest.mark.asyncio
async def test_resume_records_job_events(tmp_path, monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    sm, mgr, store = _setup(tmp_path)
    try:
        _seed_interrupted_job(store, "job_ev")
        res = await mgr.resume_job("job_ev", from_step=2, dry_run=False)
        new_id = res["resumed_job_id"]

        # 元 Job 側に resume_started、新 Job 側に job_resumed
        orig_events = store.list_events("job_ev", limit=20)
        new_events = store.list_events(new_id, limit=20)
        orig_types = [e["event_type"] for e in orig_events]
        new_types = [e["event_type"] for e in new_events]
        assert "resume_started" in orig_types
        assert "job_resumed" in new_types
    finally:
        store.close()
