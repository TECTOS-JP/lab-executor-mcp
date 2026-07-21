"""End-to-end wiring of the monitor's safe-shutdown action.

The companion test module replaces ``_best_effort_safe_shutdown`` with a mock,
which proves the monitor calls it but never exercises the shutdown itself.
These tests keep the real implementation and assert on what actually reaches
the backend, so a breach that silently sends no command cannot pass.
"""

from __future__ import annotations

import asyncio

import pytest
import yaml
from unittest.mock import AsyncMock, MagicMock

from lab_executor.job import JobManager, JobStore
from lab_executor.job.state_machine import is_terminal
from lab_executor.models.instrument_def import InstrumentDefinition
from lab_visa_mcp.session_manager import InstrumentSession


DEFINITION_WITH_SHUTDOWN = """
metadata: {manufacturer: T, model: Monitor, category: other}
commands:
  measure:
    scpi: "MEAS?"
    type: query
    polling_safe: true
  go_safe:
    scpi: "OUTP OFF"
    type: write
safe_shutdown:
  - command: go_safe
"""

DEFINITION_WITHOUT_SHUTDOWN = """
metadata: {manufacturer: T, model: Monitor, category: other}
commands:
  measure:
    scpi: "MEAS?"
    type: query
    polling_safe: true
"""


def _session(document: str) -> InstrumentSession:
    return InstrumentSession(
        resource_name="inst0",
        idn_response="<x>",
        idn_parsed={},
        definition=InstrumentDefinition(**yaml.safe_load(document)),
    )


def _manager(tmp_path, session, *, write_fails: bool = False):
    """A manager whose backend records every write instead of performing one."""
    writes: list[str] = []

    async def _write(resource_name, command, **_kwargs):
        writes.append(command)
        if write_fails:
            raise RuntimeError("relay stuck")

    backend = MagicMock()
    backend.query = AsyncMock(return_value="100")
    backend.write = AsyncMock(side_effect=_write)

    sessions = MagicMock()
    sessions.get_session.side_effect = (
        lambda name: session if name == "inst0" else None
    )
    sessions.resolve_alias.side_effect = lambda name: None
    store = JobStore(db_path=tmp_path / "jobs.sqlite")
    return JobManager(backend=backend, session_mgr=sessions, store=store), store, writes


async def _terminal(manager, job_id):
    for _ in range(200):
        record = manager.get(job_id)
        if is_terminal(record.status):
            return record
        await asyncio.sleep(0.02)
    raise AssertionError("monitor did not terminate")


@pytest.mark.asyncio
async def test_breach_actually_sends_the_shutdown_command(tmp_path):
    """The declared shutdown command must reach the backend, not just be called."""
    manager, _store, writes = _manager(
        tmp_path, _session(DEFINITION_WITH_SHUTDOWN)
    )
    record = await manager.start_monitor_job(
        "inst0",
        "measure",
        interval_s=1.0,
        duration_s=5.0,
        stop_condition_expr="value > 50",
        on_stop_condition="safe_shutdown",
    )
    await _terminal(manager, record.job_id)
    assert "OUTP OFF" in writes, writes


@pytest.mark.asyncio
async def test_record_only_sends_no_command_to_the_backend(tmp_path):
    """The default must remain byte-for-byte the old behaviour."""
    manager, _store, writes = _manager(
        tmp_path, _session(DEFINITION_WITH_SHUTDOWN)
    )
    record = await manager.start_monitor_job(
        "inst0",
        "measure",
        interval_s=1.0,
        duration_s=5.0,
        stop_condition_expr="value > 50",
    )
    await _terminal(manager, record.job_id)
    assert writes == []


@pytest.mark.asyncio
async def test_a_shutdown_command_that_fails_fails_the_job(tmp_path):
    """A safety action that did not work must never look like a clean stop."""
    manager, _store, writes = _manager(
        tmp_path, _session(DEFINITION_WITH_SHUTDOWN), write_fails=True
    )
    record = await manager.start_monitor_job(
        "inst0",
        "measure",
        interval_s=1.0,
        duration_s=5.0,
        stop_condition_expr="value > 50",
        on_stop_condition="safe_shutdown",
    )
    final = await _terminal(manager, record.job_id)
    assert "OUTP OFF" in writes
    assert final.status.value == "failed", final.status
    assert final.error_class


@pytest.mark.asyncio
async def test_instrument_without_a_shutdown_sequence_is_refused_at_start(tmp_path):
    """Discovering this at breach time would be the worst possible moment."""
    manager, _store, writes = _manager(
        tmp_path, _session(DEFINITION_WITHOUT_SHUTDOWN)
    )
    record = await manager.start_monitor_job(
        "inst0",
        "measure",
        interval_s=1.0,
        duration_s=5.0,
        stop_condition_expr="value > 50",
        on_stop_condition="safe_shutdown",
    )
    assert record.status.value == "failed"
    assert record.error_class == "validation"
    assert writes == []


@pytest.mark.asyncio
async def test_no_shutdown_when_the_monitor_ends_by_duration(tmp_path):
    """Only a breach triggers the action; a normal end must not."""
    manager, _store, writes = _manager(
        tmp_path, _session(DEFINITION_WITH_SHUTDOWN)
    )
    record = await manager.start_monitor_job(
        "inst0",
        "measure",
        interval_s=1.0,
        duration_s=2.0,
        stop_condition_expr="value > 500",
        on_stop_condition="safe_shutdown",
    )
    await _terminal(manager, record.job_id)
    assert writes == []
