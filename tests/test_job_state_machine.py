"""Job state machine のテスト (v0.5.0-rc2)"""
import pytest

from lab_executor.job.state_machine import (
    JobStatus,
    CancelMode,
    can_transition,
    is_terminal,
    is_active,
    validate_transition,
    IllegalTransitionError,
    TERMINAL_STATUSES,
    ACTIVE_STATUSES,
)


# === 終端/アクティブ判定 ===

def test_terminal_statuses_complete():
    assert is_terminal(JobStatus.COMPLETED)
    assert is_terminal(JobStatus.FAILED)
    assert is_terminal(JobStatus.CANCELLED)
    assert is_terminal(JobStatus.TIMEOUT)
    assert is_terminal(JobStatus.INTERRUPTED)


def test_active_statuses():
    assert is_active(JobStatus.QUEUED)
    assert is_active(JobStatus.RUNNING)
    assert is_active(JobStatus.WAITING)
    assert is_active(JobStatus.CANCELLING)
    assert not is_active(JobStatus.COMPLETED)


def test_terminal_and_active_disjoint():
    assert TERMINAL_STATUSES.isdisjoint(ACTIVE_STATUSES)


# === 許可される遷移 ===

@pytest.mark.parametrize("from_s,to_s", [
    (JobStatus.QUEUED, JobStatus.RUNNING),
    (JobStatus.QUEUED, JobStatus.CANCELLING),
    (JobStatus.QUEUED, JobStatus.CANCELLED),
    (JobStatus.QUEUED, JobStatus.INTERRUPTED),
    (JobStatus.RUNNING, JobStatus.WAITING),
    (JobStatus.RUNNING, JobStatus.COMPLETED),
    (JobStatus.RUNNING, JobStatus.FAILED),
    (JobStatus.RUNNING, JobStatus.CANCELLING),
    (JobStatus.RUNNING, JobStatus.TIMEOUT),
    (JobStatus.RUNNING, JobStatus.INTERRUPTED),
    (JobStatus.WAITING, JobStatus.RUNNING),
    (JobStatus.WAITING, JobStatus.COMPLETED),
    (JobStatus.WAITING, JobStatus.FAILED),
    (JobStatus.WAITING, JobStatus.CANCELLING),
    (JobStatus.WAITING, JobStatus.INTERRUPTED),
    (JobStatus.CANCELLING, JobStatus.CANCELLED),
    (JobStatus.CANCELLING, JobStatus.FAILED),
])
def test_allowed_transitions(from_s, to_s):
    assert can_transition(from_s, to_s)


# === 禁止される遷移 ===

@pytest.mark.parametrize("from_s,to_s", [
    # 終端からは遷移不可
    (JobStatus.COMPLETED, JobStatus.RUNNING),
    (JobStatus.FAILED, JobStatus.RUNNING),
    (JobStatus.CANCELLED, JobStatus.RUNNING),
    (JobStatus.INTERRUPTED, JobStatus.RUNNING),
    # queued から直接 completed は不可
    (JobStatus.QUEUED, JobStatus.COMPLETED),
    # cancelled から timeout は不可
    (JobStatus.CANCELLED, JobStatus.TIMEOUT),
])
def test_forbidden_transitions(from_s, to_s):
    assert not can_transition(from_s, to_s)


def test_validate_transition_raises():
    with pytest.raises(IllegalTransitionError) as exc:
        validate_transition(JobStatus.COMPLETED, JobStatus.RUNNING)
    assert exc.value.from_status == JobStatus.COMPLETED
    assert exc.value.to_status == JobStatus.RUNNING


def test_validate_transition_ok():
    # 例外が出ないこと
    validate_transition(JobStatus.QUEUED, JobStatus.RUNNING)


# === CancelMode ===

def test_cancel_mode_values():
    assert CancelMode.IMMEDIATE.value == "immediate"
    assert CancelMode.AFTER_CURRENT_STEP.value == "after_current_step"
    assert CancelMode.SAFE_SHUTDOWN.value == "safe_shutdown"


def test_cancel_mode_from_string():
    assert CancelMode("immediate") is CancelMode.IMMEDIATE
    with pytest.raises(ValueError):
        CancelMode("invalid")
