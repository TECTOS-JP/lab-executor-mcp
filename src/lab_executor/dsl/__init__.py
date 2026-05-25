"""
v0.8.0: Experiment DSL

LLM が生成する JSON 実験計画を、既存 Plan/Step IR に compile し、
既存 JobManager / GroupExecutor で実行する。

設計:
- DSL schema (`schema.py`) は Pydantic で型安全に
- validator (`validator.py`) は 15 項目検証 + warnings
- compiler (`compiler.py`) は DSL → 既存 IR
- dry_run (`dry_run.py`) は実機 I/O ゼロで rendered + safety + verify summary を返す

v0.8.0 で実装する DSL 命令 (10 種):
  command / query / wait / wait_until / wait_for_condition / wait_for_stable /
  barrier / sweep / parallel / safe_shutdown
"""
from lab_executor.dsl.schema import (
    ExperimentPlan, Step, SweepValues,
    DSLCommandStep, DSLQueryStep,
    DSLWaitStep, DSLWaitUntilStep,
    DSLWaitForConditionStep, DSLWaitForStableStep,
    DSLBarrierStep, DSLSafeShutdownStep,
    DSLSweepStep, DSLParallelStep,
    CURRENT_DSL_VERSION,
    MAX_SWEEP_POINTS, MAX_PARALLEL_CONCURRENCY, MAX_PARALLEL_BRANCHES,
)

__all__ = [
    "ExperimentPlan", "Step", "SweepValues",
    "DSLCommandStep", "DSLQueryStep",
    "DSLWaitStep", "DSLWaitUntilStep",
    "DSLWaitForConditionStep", "DSLWaitForStableStep",
    "DSLBarrierStep", "DSLSafeShutdownStep",
    "DSLSweepStep", "DSLParallelStep",
    "CURRENT_DSL_VERSION",
    "MAX_SWEEP_POINTS", "MAX_PARALLEL_CONCURRENCY", "MAX_PARALLEL_BRANCHES",
]
