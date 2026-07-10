"""
シーケンス処理の実行時オーケストレーション (SP-1 / SP-2 / SP-3)

同期経路 (recipe_executor.execute_plan) と非同期 Job 経路 (job/manager) の
両方から使う共通ヘルパ。式評価・capture・compute・${...} 実行時解決 + 範囲執行・
branch / repeat / guard の実行を一箇所に集約し、両経路で同一の挙動を保証する。

安全要件 (sequence_processing_spec §5.1 / §5.3-5.5 / §6):
- capture: 値が抽出できない (None) 場合はステップ failed (silent NaN 防止)
- deferred: 解決値が範囲外なら実行せず range_violation で failed (+ safe_shutdown)
- compute: 評価エラーは on_error に従う (abort / safe_shutdown)
- guard: 偽なら on_fail (abort / safe_shutdown / warn)。評価エラーは常に failed
- repeat while: max_iterations 到達は failed にせず repeat_ended に記録
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .experiment_ir import (
    BranchStep, CommandStep, ComputeStep, GuardStep, RepeatStep,
    VariableStore, VariableStoreError, WaitStep,
)
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

    v2.29.0: 解決値が数値でない場合 (str / bool) は SeqExpressionError。
    実行時解決引数は範囲で縛る前提のため数値のみ許可する (安全要件 §6。
    str と min/max の比較で TypeError が裸で伝播していた問題の修正)。
    """
    ctx = store.as_ctx()
    resolved: dict[str, Any] = {}
    infos: list[dict] = []
    for arg, spec in (step.deferred_args or {}).items():
        expr = spec["expr"]
        mn = spec.get("min")
        mx = spec.get("max")
        val = evaluate(expr, ctx)
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            raise SeqExpressionError(
                f"実行時引数は数値である必要があります: "
                f"{step.command}.{arg} = {val!r} (式: {expr!r})"
            )
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


# ============================================================
# v2.29.0 (SP-3): guard / branch / repeat
# ============================================================


@dataclass
class NestedExecutors:
    """branch case / repeat body 内のリーフ step を実行するコールバック束。

    同期経路と Job 経路で command / wait の実行方法 (cancel 応答・イベント記録)
    が異なるため、経路側が束ねて渡す。

    - ``run_command(step, step_path) -> dict``: CommandStep 実行
      (通常 ``process_command_step`` をラップする)
    - ``run_wait(step, step_path) -> dict``: WaitStep 実行
      (Job 経路は slice + cancel チェック付き)
    - ``cancel_check() -> "cancel" | "timeout" | None``: 各 step 前に呼ばれる
      (同期経路は None のままで良い)
    """
    run_command: Callable[[CommandStep, str], Awaitable[dict]]
    run_wait: Callable[[WaitStep, str], Awaitable[dict]]
    cancel_check: Callable[[], str | None] | None = None


_INTERRUPT_FLAGS = ("interrupted_by_cancel", "interrupted_by_timeout")


def _bubble_failure(outer: dict, results: list[dict]) -> dict:
    """ネスト実行の失敗を外側 result へ伝播する (error / message / 中断フラグ)。"""
    if results:
        last = results[-1]
        outer.setdefault("error", last.get("error", "nested_step_failed"))
        if last.get("message"):
            outer.setdefault("message", last["message"])
        for f in _INTERRUPT_FLAGS:
            if last.get(f):
                outer[f] = True
    return outer


async def execute_step_list(
    steps: list,
    store: VariableStore,
    execs: NestedExecutors,
    *,
    step_path: str,
    emit_event: EmitEvent | None = None,
    safe_shutdown: SafeShutdown | None = None,
) -> tuple[bool, list[dict]]:
    """ネストした Step 列を順次実行する (branch case / repeat body 用)。

    返り値 ``(ok, results)``。失敗 step で打ち切る。
    各 result には階層 ``step_path`` ("steps[3]/case[1]/steps[0]" 形式) が付く。
    """
    results: list[dict] = []
    for i, st in enumerate(steps):
        path = f"{step_path}/steps[{i}]"
        if execs.cancel_check is not None:
            reason = execs.cancel_check()
            if reason == "timeout":
                results.append({
                    "step_path": path, "success": False,
                    "error": "timeout", "interrupted_by_timeout": True,
                    "message": "nested step interrupted by job_timeout_s",
                })
                return False, results
            if reason == "cancel":
                results.append({
                    "step_path": path, "success": False,
                    "error": "cancelled", "interrupted_by_cancel": True,
                    "message": "nested step interrupted by cancel request",
                })
                return False, results

        if isinstance(st, WaitStep):
            r = await execs.run_wait(st, path)
        elif isinstance(st, CommandStep):
            r = await execs.run_command(st, path)
        elif isinstance(st, ComputeStep):
            r = await process_compute_step(
                st, store, source_step_path=path,
                emit_event=emit_event, safe_shutdown=safe_shutdown,
            )
        elif isinstance(st, GuardStep):
            r = await process_guard_step(
                st, store, source_step_path=path,
                emit_event=emit_event, safe_shutdown=safe_shutdown,
            )
        elif isinstance(st, BranchStep):
            r = await process_branch_step(
                st, store, execs, source_step_path=path,
                emit_event=emit_event, safe_shutdown=safe_shutdown,
            )
        elif isinstance(st, RepeatStep):
            r = await process_repeat_step(
                st, store, execs, source_step_path=path,
                emit_event=emit_event, safe_shutdown=safe_shutdown,
            )
        else:
            r = {
                "success": False, "error": "UnsupportedStepType",
                "step_type": getattr(st, "type", "?"),
                "message": "branch/repeat 内で未対応のステップ型です",
            }
        results.append({"step_path": path, **r})
        if not r.get("success", False):
            return False, results
    return True, results


async def process_guard_step(
    step: GuardStep,
    store: VariableStore,
    *,
    source_step_path: str = "",
    emit_event: EmitEvent | None = None,
    safe_shutdown: SafeShutdown | None = None,
) -> dict:
    """GuardStep を評価する (spec §5.5)。

    - 真: success
    - 偽: on_fail = warn (続行 + guard_failed イベント) / abort (failed) /
      safe_shutdown (安全停止後 failed)
    - 式評価エラー: on_fail に関わらず failed (判定不能を通さない)
    """
    base = {
        "step_type": "guard", "expr": step.expr,
        "on_fail": step.on_fail,
    }
    try:
        passed = bool(evaluate(step.expr, store.as_ctx()))
    except SeqExpressionError as e:
        return {
            **base, "success": False, "error": "guard_error",
            "message": f"guard 式の評価エラー: {e}",
        }

    if passed:
        return {**base, "passed": True, "success": True}

    if emit_event:
        emit_event("guard_failed", {
            "expr": step.expr, "on_fail": step.on_fail,
            "message": step.message, "step_path": source_step_path,
        })

    if step.on_fail == "warn":
        return {
            **base, "passed": False, "warned": True, "success": True,
            "message": step.message or "guard 条件が偽です (warn: 続行)",
        }

    shutdown = None
    if step.on_fail == "safe_shutdown" and safe_shutdown:
        shutdown = await safe_shutdown()
    return {
        **base, "passed": False, "success": False, "error": "guard_failed",
        "message": step.message or f"guard 条件が偽です: {step.expr}",
        "safe_shutdown": shutdown,
    }


async def process_branch_step(
    step: BranchStep,
    store: VariableStore,
    execs: NestedExecutors,
    *,
    source_step_path: str = "",
    emit_event: EmitEvent | None = None,
    safe_shutdown: SafeShutdown | None = None,
) -> dict:
    """BranchStep を実行する (spec §5.3)。

    上から評価し、最初に真になった case の steps のみ実行する。
    採択を timeline イベント ``branch_taken`` (条件式・評価値付き) に記録。
    どの case も採択されない (else 無し・全 when 偽) 場合は no-op success。
    """
    base = {"step_type": "branch"}
    for ci, case in enumerate(step.cases):
        if case.when is None:
            taken, val = True, None
        else:
            try:
                val = evaluate(case.when, store.as_ctx())
                taken = bool(val)
            except SeqExpressionError as e:
                return {
                    **base, "success": False, "error": "branch_error",
                    "message": f"branch 条件の評価エラー (case[{ci}]): {e}",
                }
        if not taken:
            continue

        if emit_event:
            emit_event("branch_taken", {
                "case_index": ci, "when": case.when, "value": val,
                "step_path": source_step_path,
            })
        ok, results = await execute_step_list(
            case.steps, store, execs,
            step_path=f"{source_step_path}/branch/case[{ci}]",
            emit_event=emit_event, safe_shutdown=safe_shutdown,
        )
        out = {
            **base, "case_index": ci, "when": case.when,
            "steps_executed": results, "success": ok,
        }
        if not ok:
            _bubble_failure(out, results)
        return out

    # どの case も採択されず (else 無し)
    if emit_event:
        emit_event("branch_taken", {
            "case_index": None, "when": None, "value": None,
            "step_path": source_step_path,
        })
    return {**base, "case_index": None, "steps_executed": [], "success": True}


_ENV_MISSING = object()


async def process_repeat_step(
    step: RepeatStep,
    store: VariableStore,
    execs: NestedExecutors,
    *,
    source_step_path: str = "",
    emit_event: EmitEvent | None = None,
    safe_shutdown: SafeShutdown | None = None,
) -> dict:
    """RepeatStep を実行する (spec §5.4)。

    body 内では ``env.loop_index`` (0 始まり) を参照できる (ネスト repeat では
    内側が外側を一時的に上書きし、終了時に復元する)。

    終了理由 (``repeat_ended`` イベントの reason):
    - ``count_completed``: count 回完了
    - ``condition_false``: while 条件が偽になった
    - ``max_iterations``: 上限到達 (**failed にはしない** — 後続 guard で扱う)
    """
    base = {"step_type": "repeat"}
    all_results: list[dict] = []
    prev_loop = store.get_env("loop_index", _ENV_MISSING)

    def _restore_env() -> None:
        if prev_loop is _ENV_MISSING:
            store.del_env("loop_index")
        else:
            store.set_env("loop_index", prev_loop)

    async def _run_iteration(i: int) -> tuple[bool, list[dict]]:
        store.set_env("loop_index", i)
        return await execute_step_list(
            step.body, store, execs,
            step_path=f"{source_step_path}/repeat/iter[{i}]",
            emit_event=emit_event, safe_shutdown=safe_shutdown,
        )

    iterations = 0
    reason = ""
    try:
        if step.count is not None:
            for i in range(step.count):
                ok, results = await _run_iteration(i)
                all_results.extend(results)
                if not ok:
                    return _bubble_failure({
                        **base, "iterations": iterations,
                        "steps_executed": all_results, "success": False,
                    }, results)
                iterations += 1
            reason = "count_completed"
        else:
            max_it = step.max_iterations or 0
            i = 0
            while True:
                if i >= max_it:
                    reason = "max_iterations"
                    break
                try:
                    cond = bool(evaluate(step.while_expr or "", store.as_ctx()))
                except SeqExpressionError as e:
                    return {
                        **base, "iterations": iterations,
                        "steps_executed": all_results, "success": False,
                        "error": "repeat_error",
                        "message": f"repeat.while の評価エラー: {e}",
                    }
                if not cond:
                    reason = "condition_false"
                    break
                ok, results = await _run_iteration(i)
                all_results.extend(results)
                if not ok:
                    return _bubble_failure({
                        **base, "iterations": iterations,
                        "steps_executed": all_results, "success": False,
                    }, results)
                iterations += 1
                i += 1
    finally:
        _restore_env()

    if emit_event:
        emit_event("repeat_ended", {
            "reason": reason, "iterations": iterations,
            "step_path": source_step_path,
        })
    return {
        **base, "iterations": iterations, "ended": reason,
        "steps_executed": all_results, "success": True,
    }
