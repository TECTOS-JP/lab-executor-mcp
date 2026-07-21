"""
v0.6.0: Group / Map execution

- TargetExecution IR (1 target = 1 plan + required_resources + bindings)
- FailurePolicy (continue / stop_on_first_error / stop_if_failure_rate_exceeds)
- GroupExecutor (concurrency / partial_failure / retry / cancel)
- Resolver ($psu → resource_name 経由 system_config)

v0.8.0 のリポジトリ分割時に experiment_mcp/group/ へそのまま移動できるよう、
lab_visa_mcp 本体への直接依存は最小化 (visa_manager, session_manager, experiment_ir,
system_config のみ)。
"""
from lab_executor.group.target import TargetExecution, FailurePolicy
from lab_executor.group.resolver import (
    resolve_resource,
    resolve_unit_bindings,
    collect_target_resources,
    ResolveError,
)

__all__ = [
    "TargetExecution",
    "FailurePolicy",
    "resolve_resource",
    "resolve_unit_bindings",
    "collect_target_resources",
    "ResolveError",
]
