"""recommended_next_actions のテスト (v0.5.0)"""
from lab_executor.job.state_machine import JobStatus
from lab_executor.job.store import JobRecord
from lab_executor.tools.jobs import _recommended_actions_for


def _rec(status: JobStatus, error_class: str = "") -> JobRecord:
    return JobRecord(
        job_id="job_test",
        owner="agent_a",
        resource_name="TEST::INSTR",
        recipe="rec_name",
        parameters={"target_v": 5},
        status=status,
        current_step_index=3,
        error_class=error_class,
        last_step_summary="...",
    )


def test_timeout_has_retry_and_safe_shutdown():
    actions = _recommended_actions_for(_rec(JobStatus.TIMEOUT))
    action_names = [a["action"] for a in actions]
    assert "retry" in action_names
    assert "inspect_state" in action_names
    assert "safe_shutdown" in action_names
    # retry には job_timeout_s 推奨が含まれる
    retry = next(a for a in actions if a["action"] == "retry")
    assert "job_timeout_s" in retry.get("args", {})


def test_interrupted_has_inspect_and_safe_shutdown():
    actions = _recommended_actions_for(_rec(JobStatus.INTERRUPTED))
    names = [a["action"] for a in actions]
    assert "inspect_state" in names
    assert "safe_shutdown" in names
    assert "resume_from_step" in names


def test_failed_with_safety_error():
    actions = _recommended_actions_for(_rec(JobStatus.FAILED, "safety"))
    names = [a["action"] for a in actions]
    assert "review_safety_constraints" in names
    assert "retry_with_override" in names


def test_failed_with_validation():
    actions = _recommended_actions_for(_rec(JobStatus.FAILED, "validation"))
    names = [a["action"] for a in actions]
    assert "fix_parameters" in names


def test_failed_with_not_found():
    actions = _recommended_actions_for(_rec(JobStatus.FAILED, "not_found"))
    names = [a["action"] for a in actions]
    assert "list_recipes" in names
    assert "list_resources" in names


def test_failed_with_other_error_class():
    actions = _recommended_actions_for(_rec(JobStatus.FAILED, "hardware"))
    names = [a["action"] for a in actions]
    assert "retry" in names
    assert "inspect_state" in names


def test_cancelled_has_inspect():
    actions = _recommended_actions_for(_rec(JobStatus.CANCELLED))
    names = [a["action"] for a in actions]
    assert "inspect_state" in names


def test_completed_no_recommendations():
    actions = _recommended_actions_for(_rec(JobStatus.COMPLETED))
    assert actions == []


def test_all_actions_have_reason():
    """全 action に reason が必須 (LLM が選びやすいよう)"""
    for status in [JobStatus.TIMEOUT, JobStatus.INTERRUPTED, JobStatus.CANCELLED,
                   JobStatus.FAILED]:
        for err in ["", "safety", "validation", "not_found", "hardware"]:
            actions = _recommended_actions_for(_rec(status, err))
            for a in actions:
                assert "action" in a
                assert "reason" in a
                assert a["reason"], f"empty reason in {status} / {err} / {a}"
