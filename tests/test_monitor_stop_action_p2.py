import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from lab_executor.job import JobManager, JobStore
from lab_executor.job.state_machine import JobStatus, is_terminal
from lab_executor.models.instrument_def import InstrumentDefinition
from lab_visa_mcp.session_manager import InstrumentSession


def _session(category: str = "power_supply") -> InstrumentSession:
    definition = InstrumentDefinition(**yaml.safe_load(f"""
metadata: {{manufacturer: T, model: Monitor, category: {category}}}
commands:
  measure:
    scpi: "MEAS?"
    type: query
    polling_safe: true
"""))
    return InstrumentSession(
        resource_name="inst0", idn_response="<x>", idn_parsed={},
        definition=definition,
    )


def _manager(tmp_path, session, value="100"):
    visa = MagicMock()
    visa.query = AsyncMock(return_value=value)

    sessions = MagicMock()
    sessions.get_session.side_effect = lambda name: session if name == "inst0" else None
    sessions.resolve_alias.side_effect = lambda name: None
    store = JobStore(db_path=tmp_path / "jobs.sqlite")
    return JobManager(visa, sessions, store=store), store


async def _terminal(mgr, job_id):
    for _ in range(100):
        record = mgr.get(job_id)
        if is_terminal(record.status):
            return record
        await asyncio.sleep(0.02)
    raise AssertionError("monitor did not terminate")


def _stop_event(store, job_id):
    return next(
        event for event in store.list_events(job_id)
        if event["event_type"] == "monitor_stop_condition_met"
    )


@pytest.mark.asyncio
async def test_record_only_default_does_not_attempt_shutdown(tmp_path):
    mgr, store = _manager(tmp_path, _session())
    mgr._best_effort_safe_shutdown = AsyncMock()
    try:
        rec = await mgr.start_monitor_job(
            "inst0", "measure", interval_s=1.0, duration_s=10,
            stop_condition_expr="value > 50",
        )
        final = await _terminal(mgr, rec.job_id)
        assert final.status == JobStatus.COMPLETED
        mgr._best_effort_safe_shutdown.assert_not_awaited()
        assert "safe_shutdown" not in _stop_event(store, rec.job_id)["payload"]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_safe_shutdown_success_is_recorded(tmp_path):
    mgr, store = _manager(tmp_path, _session())
    shutdown = {
        "attempted": True, "source": "yaml", "success": True,
        "steps": [{"step": 0, "kind": "command", "success": True}],
        "skipped_reason": None,
    }
    mgr._best_effort_safe_shutdown = AsyncMock(return_value=shutdown)
    try:
        rec = await mgr.start_monitor_job(
            "inst0", "measure", interval_s=1.0, duration_s=10,
            stop_condition_expr="value > 50", on_stop_condition="safe_shutdown",
        )
        final = await _terminal(mgr, rec.job_id)
        assert final.status == JobStatus.COMPLETED
        mgr._best_effort_safe_shutdown.assert_awaited_once()
        assert _stop_event(store, rec.job_id)["payload"]["safe_shutdown"] == shutdown
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("raises", [False, True])
async def test_safe_shutdown_failure_fails_closed(tmp_path, raises):
    mgr, store = _manager(tmp_path, _session())
    if raises:
        mgr._best_effort_safe_shutdown = AsyncMock(side_effect=RuntimeError("relay stuck"))
    else:
        mgr._best_effort_safe_shutdown = AsyncMock(return_value={
            "attempted": True, "source": "yaml", "success": False,
            "steps": [{
                "step": 0, "kind": "command", "command": "output_off",
                "success": False, "error": "WriteFailed",
            }],
            "skipped_reason": None,
        })
    try:
        rec = await mgr.start_monitor_job(
            "inst0", "measure", interval_s=1.0, duration_s=10,
            stop_condition_expr="value > 50", on_stop_condition="safe_shutdown",
        )
        final = await _terminal(mgr, rec.job_id)
        assert final.status == JobStatus.FAILED
        assert final.error_class
        assert "safe shutdown failed" in final.last_step_summary
        result = _stop_event(store, rec.job_id)["payload"]["safe_shutdown"]
        assert result["success"] is False
        assert (result.get("message") == "relay stuck") if raises else result["steps"]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_safe_shutdown_without_capability_is_refused(tmp_path):
    mgr, store = _manager(tmp_path, _session("thermometer"))
    try:
        rec = await mgr.start_monitor_job(
            "inst0", "measure", on_stop_condition="safe_shutdown",
        )
        assert rec.status == JobStatus.FAILED
        assert rec.error_class == "validation"
        assert "fallback" in rec.last_step_summary
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["cancel_job", "unknown"])
async def test_invalid_stop_action_is_refused(tmp_path, mode):
    mgr, store = _manager(tmp_path, _session())
    try:
        rec = await mgr.start_monitor_job("inst0", "measure", on_stop_condition=mode)
        assert rec.status == JobStatus.FAILED
        assert rec.error_class == "validation"
        assert mode in rec.last_step_summary
        if mode == "cancel_job":
            assert "どの job" in rec.last_step_summary
    finally:
        store.close()


@pytest.mark.asyncio
async def test_duration_end_does_not_attempt_safe_shutdown(tmp_path):
    mgr, store = _manager(tmp_path, _session(), value="1")
    mgr._best_effort_safe_shutdown = AsyncMock()
    try:
        rec = await mgr.start_monitor_job(
            "inst0", "measure", interval_s=1.0, duration_s=0.05,
            stop_condition_expr="value > 50", on_stop_condition="safe_shutdown",
        )
        final = await _terminal(mgr, rec.job_id)
        assert final.status == JobStatus.COMPLETED
        assert final.result["stopped_by_condition"] is False
        mgr._best_effort_safe_shutdown.assert_not_awaited()
    finally:
        store.close()
