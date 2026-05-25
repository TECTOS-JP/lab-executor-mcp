"""JobStore (SQLite 永続化) のテスト (v0.5.0-rc2)"""
import os
import pytest

from lab_executor.job.state_machine import JobStatus, IllegalTransitionError
from lab_executor.job.store import JobStore


@pytest.fixture
def store(tmp_path):
    db = tmp_path / "test_state.sqlite"
    s = JobStore(db_path=db)
    yield s
    s.close()


def test_create_and_get(store):
    rec = store.create_job(
        job_id="job_001",
        owner="agent_a",
        resource_name="GPIB0::1::INSTR",
        recipe="safe_output_on",
        parameters={"target_v": 5.0},
    )
    assert rec.job_id == "job_001"
    assert rec.status == JobStatus.QUEUED
    assert rec.parameters == {"target_v": 5.0}

    fetched = store.get("job_001")
    assert fetched is not None
    assert fetched.owner == "agent_a"
    assert fetched.recipe == "safe_output_on"


def test_get_missing_returns_none(store):
    assert store.get("nonexistent") is None


def test_transition_status(store):
    store.create_job("job_001", "", "R", "rec", {})
    rec = store.transition_status("job_001", JobStatus.RUNNING, current_step_index=0)
    assert rec.status == JobStatus.RUNNING
    assert rec.current_step_index == 0


def test_transition_status_illegal_raises(store):
    store.create_job("job_001", "", "R", "rec", {})
    # queued → completed は不可
    with pytest.raises(IllegalTransitionError):
        store.transition_status("job_001", JobStatus.COMPLETED)


def test_transition_to_terminal_with_result(store):
    store.create_job("job_001", "", "R", "rec", {})
    store.transition_status("job_001", JobStatus.RUNNING)
    rec = store.transition_status(
        "job_001", JobStatus.COMPLETED,
        result={"success": True, "steps_executed": [{"step": 0, "success": True}]},
    )
    assert rec.status == JobStatus.COMPLETED
    assert rec.result is not None
    assert rec.result["success"] is True


def test_update_step_does_not_change_status(store):
    store.create_job("job_001", "", "R", "rec", {})
    store.transition_status("job_001", JobStatus.RUNNING)
    store.update_step("job_001", 3, last_step_summary="command set_voltage")
    rec = store.get("job_001")
    assert rec.status == JobStatus.RUNNING
    assert rec.current_step_index == 3
    assert rec.last_step_summary == "command set_voltage"


def test_list_jobs_orders_by_created_at_desc(store):
    for i in range(5):
        store.create_job(f"job_{i:03d}", "", "R", "r", {})
    recs = store.list_jobs(limit=3)
    assert len(recs) == 3
    # 最後に作ったものが先頭
    assert recs[0].job_id == "job_004"


def test_list_jobs_status_filter(store):
    store.create_job("job_a", "", "R", "r", {})
    store.create_job("job_b", "", "R", "r", {})
    store.transition_status("job_a", JobStatus.RUNNING)
    store.transition_status("job_a", JobStatus.COMPLETED)
    # job_b は queued のまま
    completed = store.list_jobs(status_filter=["completed"])
    assert len(completed) == 1
    assert completed[0].job_id == "job_a"


def test_list_jobs_owner_filter(store):
    store.create_job("job_a", "alice", "R", "r", {})
    store.create_job("job_b", "bob", "R", "r", {})
    alice_jobs = store.list_jobs(owner="alice")
    assert len(alice_jobs) == 1
    assert alice_jobs[0].owner == "alice"


def test_mark_interrupted_on_startup(store):
    # running / waiting / cancelling の Job を作る
    store.create_job("job_r", "", "R", "r", {})
    store.transition_status("job_r", JobStatus.RUNNING)
    store.create_job("job_w", "", "R", "r", {})
    store.transition_status("job_w", JobStatus.RUNNING)
    store.transition_status("job_w", JobStatus.WAITING)
    store.create_job("job_c", "", "R", "r", {})
    store.transition_status("job_c", JobStatus.RUNNING)
    store.transition_status("job_c", JobStatus.CANCELLING)
    # 完了済み Job も作る (こちらは触れない)
    store.create_job("job_done", "", "R", "r", {})
    store.transition_status("job_done", JobStatus.RUNNING)
    store.transition_status("job_done", JobStatus.COMPLETED)

    n = store.mark_interrupted_on_startup()
    assert n == 3

    assert store.get("job_r").status == JobStatus.INTERRUPTED
    assert store.get("job_w").status == JobStatus.INTERRUPTED
    assert store.get("job_c").status == JobStatus.INTERRUPTED
    assert store.get("job_done").status == JobStatus.COMPLETED  # unchanged


def test_default_store_path_uses_env(monkeypatch, tmp_path):
    from lab_executor.job.store import default_store_path
    p = tmp_path / "custom.sqlite"
    monkeypatch.setenv("VISA_MCP_STATE_DB", str(p))
    assert default_store_path() == p
