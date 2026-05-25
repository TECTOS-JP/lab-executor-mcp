"""v0.9.0.1: external review response (P1)

- task_005 rename → task_005_partial_failure_parallel
- task_004 strict mode + random_seed
- resume_job safe_shutdown_before_resume 失敗時の挙動
- BenchmarkTask に random_seed / safety_mode フィールド
- resume_job(dry_run) の steps_to_execute に step_path 含む
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
from lab_executor.testing.benchmark_task import (
    BenchmarkTask, Fixtures, load_benchmark_task,
)


ROOT = Path(__file__).parent.parent


# =========================================================
# P1: Benchmark Fixtures に random_seed / safety_mode
# =========================================================


def test_fixtures_has_random_seed_and_safety_mode():
    f = Fixtures(random_seed=12345, safety_mode="strict")
    assert f.random_seed == 12345
    assert f.safety_mode == "strict"


def test_task_004_uses_strict_mode_and_seed():
    p = ROOT / "benchmarks" / "tasks" / "task_004_verify_mismatch.yaml"
    t = load_benchmark_task(p)
    assert t.fixtures.safety_mode == "strict"
    assert t.fixtures.random_seed == 12345
    assert t.expected.success_criteria.job_status == "failed"


def test_task_005_renamed_to_parallel():
    p = ROOT / "benchmarks" / "tasks" / "task_005_partial_failure_parallel.yaml"
    assert p.exists()
    t = load_benchmark_task(p)
    assert t.id == "task_005_partial_failure_parallel"
    # task_005_partial_failure_group は削除されている
    old = ROOT / "benchmarks" / "tasks" / "task_005_partial_failure_group.yaml"
    assert not old.exists()


# =========================================================
# P1: resume_job step_path
# =========================================================


YAML_PSU = """
metadata: { manufacturer: T, model: PSU, category: power_supply }
commands:
  set_voltage:
    scpi: "VOLT {voltage}"
    type: write
    parameters:
      - { name: voltage, type: float, range: [0, 100] }
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


def _seed_interrupted(store: JobStore, job_id: str) -> None:
    store.create_job(
        job_id=job_id, owner="agent",
        resource_name="psu001", recipe="<dsl:r>",
        parameters={"plan_id": "p1"},
    )
    store.transition_status(job_id, JobStatus.RUNNING, current_step_index=0,
                            last_step_summary="1")
    store.transition_status(job_id, JobStatus.INTERRUPTED,
                            last_step_summary="boom")
    plan = {
        "dsl_version": "0.8",
        "name": "r",
        "bindings": {"psu": "psu001"},
        "steps": [
            {"type": "command", "instrument": "$psu", "command": "set_voltage",
             "args": {"voltage": 1.0}},
            {"type": "command", "instrument": "$psu", "command": "set_voltage",
             "args": {"voltage": 2.0}},
        ],
    }
    store.save_experiment_plan(
        plan_id="p1", job_id=job_id, name="r", dsl_version="0.8",
        original_plan=plan, compiled_summary={"used_resources": ["psu001"]},
    )


@pytest.mark.asyncio
async def test_resume_dry_run_includes_step_path(tmp_path, monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    sm, mgr, store = _setup(tmp_path)
    try:
        _seed_interrupted(store, "job_sp")
        res = await mgr.resume_job("job_sp", from_step=1, dry_run=True)
        assert res["resume_ready"] is True
        st = res["steps_to_execute"]
        assert len(st) == 1
        assert st[0]["step_index"] == 1
        assert st[0]["step_path"] == "steps[1]"
    finally:
        store.close()


# =========================================================
# P1: safe_shutdown_before_resume failure → resume 中止
# =========================================================


@pytest.mark.asyncio
async def test_resume_aborts_when_safe_shutdown_fails(tmp_path, monkeypatch):
    """safe_shutdown_before_resume=True で全 resource の shutdown が失敗
    した場合、resume は中止され safe_shutdown_failed が error として返る"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    sm, mgr, store = _setup(tmp_path)
    try:
        _seed_interrupted(store, "job_ss")
        # _best_effort_safe_shutdown が失敗を返すように mock
        async def _fail(session):
            return {"success": False, "ok": False, "error": "mock fail"}
        mgr._best_effort_safe_shutdown = _fail  # type: ignore[assignment]

        res = await mgr.resume_job(
            "job_ss", from_step=1, dry_run=False,
            safe_shutdown_before_resume=True,
        )
        assert res["resume_ready"] is False
        errs = res.get("errors") or []
        assert any(e["error_class"] == "safe_shutdown_failed" for e in errs)
        # 新 Job は作られていない
        assert "resumed_job_id" not in res
    finally:
        store.close()


@pytest.mark.asyncio
async def test_resume_proceeds_when_safe_shutdown_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    sm, mgr, store = _setup(tmp_path)
    try:
        _seed_interrupted(store, "job_ok")
        async def _ok(session):
            return {"success": True, "ok": True}
        mgr._best_effort_safe_shutdown = _ok  # type: ignore[assignment]

        res = await mgr.resume_job(
            "job_ok", from_step=1, dry_run=False,
            safe_shutdown_before_resume=True,
        )
        assert res["resume_ready"] is True
        assert "resumed_job_id" in res
    finally:
        store.close()


# =========================================================
# Benchmark runner picks up safety_mode override
# =========================================================


@pytest.mark.asyncio
async def test_benchmark_runner_applies_safety_mode_override(
    tmp_path, monkeypatch,
):
    """task.fixtures.safety_mode が runner で OS env に設定される"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    from lab_executor.testing.benchmark_runner import run_task_file
    res = await run_task_file(
        ROOT / "benchmarks" / "tasks" / "task_004_verify_mismatch.yaml",
        ROOT / "benchmarks", tmp_path,
    )
    # strict mode で job_status=failed が観測される
    assert res.artifacts.get("job_status") == "failed"
