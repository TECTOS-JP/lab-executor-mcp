"""
Job 状態機械 (v0.5.0-rc2)

状態:
    queued      → 登録直後、まだ実行開始していない
    running     → step を順次実行中
    waiting     → wait step / polling などで待機中 (実行中の特殊形)
    completed   → 全ステップ成功で完了
    failed      → エラー (機器/プロトコル/安全制約等) で停止
    cancelling  → cancel 要求受付済み、安全停止シーケンス進行中
    cancelled   → キャンセル完了
    timeout     → job_timeout / step_timeout 経過で停止
    interrupted → サーバ再起動により中断 (再起動時に running/waiting から自動遷移)

cancel mode:
    immediate           ── 次の安全な区切りでブロッキング操作を放棄
    after_current_step  ── 現在の step を完了させてから停止
    safe_shutdown       ── YAML 定義の safe_shutdown シーケンスを実行してから停止
"""
from __future__ import annotations
from enum import Enum


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    INTERRUPTED = "interrupted"


class CancelMode(str, Enum):
    IMMEDIATE = "immediate"
    AFTER_CURRENT_STEP = "after_current_step"
    SAFE_SHUTDOWN = "safe_shutdown"


# 終端状態: これ以上遷移しない
TERMINAL_STATUSES: frozenset[JobStatus] = frozenset({
    JobStatus.COMPLETED,
    JobStatus.FAILED,
    JobStatus.CANCELLED,
    JobStatus.TIMEOUT,
    JobStatus.INTERRUPTED,
})

# アクティブ状態: 実行中または停止処理中
ACTIVE_STATUSES: frozenset[JobStatus] = frozenset({
    JobStatus.QUEUED,
    JobStatus.RUNNING,
    JobStatus.WAITING,
    JobStatus.CANCELLING,
})


# 許可される遷移ルール
_ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset({
        JobStatus.RUNNING,
        JobStatus.FAILED,            # start 直後の validation/lookup 失敗
        JobStatus.CANCELLING,        # queued 中にキャンセル要求
        JobStatus.CANCELLED,         # 直接キャンセル (実行前)
        JobStatus.INTERRUPTED,       # 再起動
    }),
    JobStatus.RUNNING: frozenset({
        JobStatus.WAITING,           # wait step 突入
        JobStatus.COMPLETED,
        JobStatus.FAILED,
        JobStatus.CANCELLING,
        JobStatus.TIMEOUT,
        JobStatus.INTERRUPTED,
    }),
    JobStatus.WAITING: frozenset({
        JobStatus.RUNNING,           # wait 完了後に次 step へ
        JobStatus.COMPLETED,         # wait が最終 step の場合
        JobStatus.FAILED,
        JobStatus.CANCELLING,
        JobStatus.TIMEOUT,
        JobStatus.INTERRUPTED,
    }),
    JobStatus.CANCELLING: frozenset({
        JobStatus.CANCELLED,
        JobStatus.FAILED,            # 安全停止中にエラーが出た場合
        JobStatus.INTERRUPTED,
    }),
    # 終端状態からの遷移はなし
    JobStatus.COMPLETED: frozenset(),
    JobStatus.FAILED: frozenset(),
    JobStatus.CANCELLED: frozenset(),
    JobStatus.TIMEOUT: frozenset(),
    JobStatus.INTERRUPTED: frozenset(),
}


def can_transition(from_status: JobStatus, to_status: JobStatus) -> bool:
    """指定の状態遷移が許可されているか判定"""
    return to_status in _ALLOWED_TRANSITIONS.get(from_status, frozenset())


def is_terminal(status: JobStatus) -> bool:
    return status in TERMINAL_STATUSES


def is_active(status: JobStatus) -> bool:
    return status in ACTIVE_STATUSES


class IllegalTransitionError(ValueError):
    """許可されていない状態遷移を試みた際に raise する"""

    def __init__(self, from_status: JobStatus, to_status: JobStatus) -> None:
        super().__init__(f"不正な状態遷移: {from_status.value} → {to_status.value}")
        self.from_status = from_status
        self.to_status = to_status


def validate_transition(from_status: JobStatus, to_status: JobStatus) -> None:
    """許可されていない遷移なら IllegalTransitionError を raise"""
    if not can_transition(from_status, to_status):
        raise IllegalTransitionError(from_status, to_status)
