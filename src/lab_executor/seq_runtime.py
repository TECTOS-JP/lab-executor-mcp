"""
シーケンス処理の実行時オーケストレーション (SP-1 / SP-2)

同期経路 (recipe_executor.execute_plan) と非同期 Job 経路 (job/manager) の
両方から使う共通ヘルパ。式評価・capture・compute・${...} 実行時解決 + 範囲執行を
一箇所に集約し、両経路で同一の挙動を保証する。

安全要件 (sequence_processing_spec §5.1 / §6):
- capture: 値が抽出できない (None) 場合はステップ failed (silent NaN 防止)
- deferred: 解決値が範囲外なら実行せず range_violation で failed (+ safe_shutdown)
- compute: 評価エラーは on_error に従う (abort / safe_shutdown)
"""
from __future__ import annotations
from typing import Any, Awaitable, Callable

from .experiment_ir import CommandStep, ComputeStep, VariableStore, VariableStoreError
from .step_executor import execute_command_step
from .utils.seq_expression import SeqExpressionError, evaluate

# emit_event(event_type, payload) -- timeline へ流す (None なら記録しない)
EmitEvent = Callable[[str, dict], None]
# safe_shutdown を実行する async callable。dict (実行サマリ) を返す
SafeShutdown = Callable[[], Awaitable[dict]]


def _as_scalar(value: Any) -> Any | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, str)):
        return value
    return None


def extract_capture_value(result: dict, value_path: str) -> Any | None:
    """command result から capture 値を抽出する。

    - ``value_path`` 指定時: ドットパスで result dict をたどる (例 "parsed.value")
    - 未指定時: observation の ``_value_numeric_from_result`` と同じ寛容抽出を再利用
      (private だが同一プロジェクト内の意図的再利用 — M1 以来の前例に従う)
    """
    if value_path:
        cur: Any = result
        for part in value_path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return None
        return _as_scalar(cur)
    from .tools.observation import _value_numeric_from_result
    return _value_numeric_from_result(result)


def resolve_deferred(step: CommandStep, store: VariableStore) -> tuple[dict, list[dict]]:
    """CommandStep.deferred_args を評価する。

    返り値 ``(resolved, infos)``:
      - ``resolved``: {arg: value}
      - ``infos``: 各 arg の {"arg", "expr", "value", "min", "max", "in_range"}
    式評価エラーは ``SeqExpressionError`` を送出する。
    """
    ctx = store.as_ctx()
    resolved: dict[str, Any] = {}
    infos: list[dict] = []
    for arg, spec in (step.deferred_args or {}).items():
        expr = spec["expr"]
        mn = spec.get("min")
        mx = spec.get("max")
        val = evaluate(expr, ctx)
        in_range = True
        if mn is not None and val < mn:
            in_range = False
        if mx is not None and val > mx:
            in_range = False
        resolved[arg] = val
        infos.append({
            "arg": arg, "expr": expr, "value": val,
            "min": mn, "max": mx, "in_range": in_range,
        })
    return resolved, infos


async def process_command_step(
    visa: Any,
    session: Any,
    step: CommandStep,
    store: VariableStore,
    *,
    override_safety: bool,
    override_reason: str,
    source_step_path: str = "",
    emit_event: EmitEvent | None = None,
    safe_shutdown: SafeShutdown | None = None,
) -> dict:
    """CommandStep を deferred 解決 + 範囲執行 + capture 付きで実行する。"""
    resolved_args = dict(step.args)

    # --- 1. ${...} 実行時引数の解決 + 範囲執行 ---
    if step.deferred_args:
        try:
            resolved, infos = resolve_deferred(step, store)
        except SeqExpressionError as e:
            return {
                "command": step.command, "success": False,
                "error": "deferred_resolve_failed", "message": str(e),
            }
        resolved_args.update(resolved)
        if emit_event:
            for info in infos:
                emit_event("deferred_arg_resolved",
                           {**info, "step_path": source_step_path})
        violated = [i for i in infos if not i["in_range"]]
        if violated:
            shutdown = await safe_shutdown() if safe_shutdown else None
            return {
                "command": step.command, "success": False,
                "error": "range_violation",
                "message": (
                    "実行時解決値が範囲外です: "
                    + ", ".join(
                        f"{i['arg']}={i['value']} (範囲 [{i['min']}, {i['max']}])"
                        for i in violated
                    )
                ),
                "violations": violated,
                "safe_shutdown": shutdown,
            }
        call_step = step.model_copy(update={"args": resolved_args})
    else:
        call_step = step

    # --- 2. コマンド実行 ---
    result = await execute_command_step(
        visa, session, call_step,
        override_safety=override_safety, override_reason=override_reason,
    )

    # --- 3. capture (result_as) ---
    if result.get("success") and step.result_as:
        val = extract_capture_value(result, step.value_path or "")
        if val is None:
            return {
                **result, "success": False, "error": "capture_failed",
                "message": (
                    f"result_as '{step.result_as}' の値を抽出できません "
                    f"(value_path={step.value_path!r})"
                ),
            }
        try:
            store.set_step(
                step.result_as, val,
                source_step_path=source_step_path, unit=step.unit or "",
            )
        except (VariableStoreError, TypeError) as e:
            return {
                **result, "success": False, "error": "capture_failed",
                "message": str(e),
            }
        if emit_event:
            emit_event("var_assigned", store.events[-1])
        result = {**result, "captured": {
            "name": step.result_as, "value": val, "unit": step.unit or "",
        }}
    return result


async def process_compute_step(
    step: ComputeStep,
    store: VariableStore,
    *,
    source_step_path: str = "",
    emit_event: EmitEvent | None = None,
    safe_shutdown: SafeShutdown | None = None,
) -> dict:
    """ComputeStep を評価し vars.* へ代入する。"""
    try:
        val = evaluate(step.expr, store.as_ctx())
        store.set_var(
            step.set, val,
            source_step_path=source_step_path, expr=step.expr, unit=step.unit,
        )
    except (SeqExpressionError, VariableStoreError, TypeError) as e:
        shutdown = None
        if step.on_error == "safe_shutdown" and safe_shutdown:
            shutdown = await safe_shutdown()
        return {
            "step_type": "compute", "set": step.set, "expr": step.expr,
            "success": False, "error": "compute_error", "message": str(e),
            "on_error": step.on_error, "safe_shutdown": shutdown,
        }
    if emit_event:
        emit_event("var_assigned", store.events[-1])
    return {
        "step_type": "compute", "set": step.set, "expr": step.expr,
        "value": val, "unit": step.unit, "success": True,
    }
