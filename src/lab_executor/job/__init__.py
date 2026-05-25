"""
lab_executor.job ── Job 実行基盤 (v0.5.0-rc2)

長時間 Recipe / 実験を非同期 Job として管理する。

- state_machine: Job 状態の定義と遷移ルール
- store: SQLite による Job メタデータの永続化
- manager: Job の生成・追跡・キャンセル
- executor: バックグラウンドで recipe を実行

v0.8.0 のリポジトリ分割時に experiment_mcp/job/ へそのまま移動できるよう、
visa_mcp 本体への直接依存を最小化している。
"""
from lab_executor.job.state_machine import (
    JobStatus,
    CancelMode,
    can_transition,
    TERMINAL_STATUSES,
    ACTIVE_STATUSES,
)
from lab_executor.job.store import JobStore, JobRecord
from lab_executor.job.manager import JobManager
from lab_executor.job.scheduler import ResourceScheduler, ResourceBusyError, QueuePolicy

__all__ = [
    "JobStatus",
    "CancelMode",
    "can_transition",
    "TERMINAL_STATUSES",
    "ACTIVE_STATUSES",
    "JobStore",
    "JobRecord",
    "JobManager",
    "ResourceScheduler",
    "ResourceBusyError",
    "QueuePolicy",
]
