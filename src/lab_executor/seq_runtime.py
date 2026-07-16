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

import numpy as np

from .experiment_ir import (
    BranchStep, CommandStep, ComputeStep, GuardStep, RepeatStep,
    VariableStore, VariableStoreError, WaitStep,
    PyStep, DllStep, CallStep,
)
from .step_executor import execute_command_step
from .utils.seq_expression import SeqExpressionError, evaluate, evaluate_condition

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

    - ``run_command(step, step_path, store) -> dict``: CommandStep 実行
      (通常 ``process_command_step`` をラップする)。v2.33.0 (SP-7): capture /
      deferred 解決の対象ストアを引数で受ける (サブシーケンスは親と別スコープ)。
    - ``run_wait(step, step_path) -> dict``: WaitStep 実行
      (Job 経路は slice + cancel チェック付き。capture が無いので store 不要)
    - ``cancel_check() -> "cancel" | "timeout" | None``: 各 step 前に呼ばれる
      (同期経路は None のままで良い)
    """
    run_command: Callable[[CommandStep, str, VariableStore], Awaitable[dict]]
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
            r = await execs.run_command(st, path, store)
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
        elif isinstance(st, PyStep):
            r = await process_py_step(
                st, store, source_step_path=path,
                emit_event=emit_event, safe_shutdown=safe_shutdown,
            )
        elif isinstance(st, DllStep):
            r = await process_dll_step(
                st, store, source_step_path=path,
                emit_event=emit_event, safe_shutdown=safe_shutdown,
            )
        elif isinstance(st, CallStep):
            r = await process_call_step(
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
        # v2.31.0 (SP-5): evaluate_condition は ndarray の曖昧真偽値を
        # SeqExpressionError に変換する (np.all/np.any での集約を促す)
        passed = evaluate_condition(step.expr, store.as_ctx())
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
                try:
                    taken = bool(val)
                except ValueError:
                    # v2.31.0 (SP-5): ndarray の曖昧真偽値を明確なエラーに
                    raise SeqExpressionError(
                        "条件式の結果が配列で真偽値が曖昧です。"
                        "np.all(...) / np.any(...) 等で集約してください"
                    )
            except SeqExpressionError as e:
                return {
                    **base, "success": False, "error": "branch_error",
                    "message": f"branch 条件の評価エラー (case[{ci}]): {e}",
                }
        if isinstance(val, np.ndarray):
            val = None  # イベント payload に ndarray を載せない (要約は不要)
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


async def process_call_step(
    step: CallStep,
    store: VariableStore,
    execs: NestedExecutors,
    *,
    source_step_path: str = "",
    emit_event: EmitEvent | None = None,
    safe_shutdown: SafeShutdown | None = None,
) -> dict:
    """CallStep をサブシーケンスとして独立スコープで実行する (spec §5.9)。

    - 子 VariableStore を作り (params = sub_params、env は親から継承)、
      展開済み sub_steps を実行する。呼び出し元の steps/vars は見えない。
    - 実行後、returns_map に列挙された子 vars/steps だけを呼び出し元 vars へ戻す。
    """
    base = {"step_type": "call", "sequence": step.sequence}
    parent_ctx = store.as_ctx()
    # v2.34.0 (SP-7.1): 実行時 with 式を呼び出し元スコープで評価して子 params に合流。
    child_params = dict(step.sub_params)
    for pname, expr in (step.with_exprs or {}).items():
        try:
            child_params[pname] = evaluate(expr, parent_ctx)
        except SeqExpressionError as e:
            return {
                **base, "success": False, "error": "call_with_error",
                "message": f"call.with['{pname}'] の評価エラー: {e}",
            }
    sub_store = VariableStore(params=child_params, env=dict(parent_ctx["env"]))
    call_path = f"{source_step_path}/call:{step.sequence}"
    if emit_event:
        emit_event("call_entered", {
            "sequence": step.sequence, "step_path": source_step_path,
            "lib_sha256": step.lib_sha256,
        })
    ok, results = await execute_step_list(
        step.sub_steps, sub_store, execs,
        step_path=call_path,
        emit_event=emit_event, safe_shutdown=safe_shutdown,
    )
    out = {**base, "steps_executed": results, "success": ok}
    if not ok:
        return _bubble_failure(out, results)
    # --- returns_map: 子 vars/steps → 呼び出し元 vars ---
    sub_ctx = sub_store.as_ctx()
    for sub_name, parent_name in step.returns_map.items():
        if sub_name in sub_ctx["vars"]:
            val = sub_ctx["vars"][sub_name]
        elif sub_name in sub_ctx["steps"]:
            val = sub_ctx["steps"][sub_name]
        else:
            return {
                **out, "success": False, "error": "call_return_missing",
                "message": (
                    f"サブシーケンス '{step.sequence}' の returns "
                    f"'{sub_name}' が実行後に未定義です"
                ),
            }
        try:
            store.set_var(
                parent_name, val,
                source_step_path=call_path,
                expr=f"<return {step.sequence}.{sub_name}>",
            )
        except (VariableStoreError, TypeError) as e:
            return {
                **out, "success": False, "error": "call_return_error",
                "message": str(e),
            }
        if emit_event:
            emit_event("var_assigned", store.events[-1])
    if emit_event:
        emit_event("call_returned", {
            "sequence": step.sequence, "step_path": source_step_path,
            "returns": list(step.returns_map.values()),
        })
    return out


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

    v2.31.0 (SP-5): ``collect`` — 各反復の capture / compute 値を蓄積し、
    repeat 終了時に vars.* へ ndarray として代入する (要素は数値のみ。
    0 回実行なら空配列)。
    """
    base = {"step_type": "repeat"}
    all_results: list[dict] = []
    prev_loop = store.get_env("loop_index", _ENV_MISSING)
    # SP-5: collect の蓄積バケツ (target -> [値, ...])
    buckets: dict[str, list[float]] = {t: [] for t in step.collect.values()}

    def _restore_env() -> None:
        if prev_loop is _ENV_MISSING:
            store.del_env("loop_index")
        else:
            store.set_env("loop_index", prev_loop)

    def _gather_collect(i: int) -> dict | None:
        """反復 i の collect 値を蓄積する。エラーなら failed 用 dict を返す。"""
        if not step.collect:
            return None
        ctx = store.as_ctx()
        for src, target in step.collect.items():
            if src in ctx["steps"]:
                val = ctx["steps"][src]
            elif src in ctx["vars"]:
                val = ctx["vars"][src]
            else:
                return {
                    "error": "collect_failed",
                    "message": (
                        f"repeat.collect の '{src}' が反復 {i} で代入されません"
                        "でした (branch で代入経路が飛ばされた可能性)"
                    ),
                }
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                return {
                    "error": "collect_failed",
                    "message": (
                        f"repeat.collect の '{src}' は数値である必要があります: "
                        f"{type(val).__name__} (反復 {i})"
                    ),
                }
            buckets[target].append(float(val))
        return None

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
                err = _gather_collect(i)
                if err is not None:
                    return {
                        **base, "iterations": iterations,
                        "steps_executed": all_results, "success": False,
                        **err,
                    }
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
                    # v2.31.0 (SP-5): evaluate_condition (ndarray 曖昧真偽値の変換)
                    cond = evaluate_condition(
                        step.while_expr or "", store.as_ctx(),
                    )
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
                err = _gather_collect(i)
                if err is not None:
                    return {
                        **base, "iterations": iterations,
                        "steps_executed": all_results, "success": False,
                        **err,
                    }
                iterations += 1
                i += 1
    finally:
        _restore_env()

    # SP-5: collect の array を vars.* へ代入 (0 回実行なら空配列)
    collected: dict[str, Any] = {}
    for src, target in step.collect.items():
        arr = np.asarray(buckets[target], dtype=float)
        try:
            store.set_var(
                target, arr,
                source_step_path=source_step_path,
                expr=f"collect({src})",
            )
        except (VariableStoreError, TypeError) as e:
            return {
                **base, "iterations": iterations,
                "steps_executed": all_results, "success": False,
                "error": "collect_failed", "message": str(e),
            }
        if emit_event:
            emit_event("var_assigned", store.events[-1])
        collected[target] = store.events[-1]["value"]  # 要約形 (JSON 安全)

    if emit_event:
        emit_event("repeat_ended", {
            "reason": reason, "iterations": iterations,
            "step_path": source_step_path,
        })
    out = {
        **base, "iterations": iterations, "ended": reason,
        "steps_executed": all_results, "success": True,
    }
    if collected:
        out["collected"] = collected
    return out


# ============================================================
# v2.32.0 (SP-6): py / dll (コード実行 + ポリシーゲート + 来歴記録)
# ============================================================


def _display(v: Any) -> Any:
    """イベント payload 用の表示値 (ndarray は要約形)。"""
    from .experiment_ir.context import summarize_array
    if isinstance(v, np.ndarray):
        return summarize_array(v)
    if isinstance(v, np.generic):
        return v.item()
    return v


async def _code_step_error(
    base: dict,
    on_error: str,
    error: str,
    message: str,
    safe_shutdown: SafeShutdown | None,
    traceback_text: str = "",
) -> dict:
    """py / dll のエラーを on_error に従って failed result にする。

    on_error=pause は Job manager 側でインターセプトされる
    (result に "on_error": "pause" が残ることで検出される)。
    """
    shutdown = None
    if on_error == "safe_shutdown" and safe_shutdown:
        shutdown = await safe_shutdown()
    out = {
        **base, "success": False, "error": error, "message": message,
        "safe_shutdown": shutdown,
    }
    if traceback_text:
        out["traceback"] = traceback_text[-2000:]
    return out


async def process_py_step(
    step: PyStep,
    store: VariableStore,
    *,
    source_step_path: str = "",
    emit_event: EmitEvent | None = None,
    safe_shutdown: SafeShutdown | None = None,
    policy: Any = None,
) -> dict:
    """PyStep を subprocess ワーカーで実行する (spec §5.7)。

    - 実行直前にポリシー再検証 + file の sha256 再照合 (TOCTOU 対策)
    - outputs に列挙されたキーのみ vars.* へ取り込む
    - 来歴: file は sha256 / code は全文を timeline ``py_executed`` に記録
    """
    from pathlib import Path as _Path

    from . import code_exec
    from .code_policy import CodePolicyError, check_python, load_policy

    base = {
        "step_type": "py",
        "file": step.file,
        "sha256": step.sha256,
        "on_error": step.on_error,
    }

    # --- 実行直前のポリシー再検証 + sha256 再照合 (TOCTOU 対策) ---
    pol = policy if policy is not None else load_policy()
    verified_file_code: str | None = None
    try:
        if step.resolved_path:
            p = _Path(step.resolved_path)
            if not p.exists():
                raise CodePolicyError(f"py.file が存在しません: {p}")
            # Hash and decode the same immutable byte snapshot that is handed to
            # the worker.  Reopening ``p`` in the worker would leave a race in
            # which an attacker can replace the file after this check.
            try:
                raw = p.read_bytes()
            except OSError as e:
                raise CodePolicyError(f"py.file を読み込めません: {p} ({e})") from e
            import hashlib as _hashlib

            current = _hashlib.sha256(raw).hexdigest()
            if current != step.sha256:
                raise CodePolicyError(
                    f"py.file の内容がコンパイル時から変更されています "
                    f"(sha256 不一致): {p}"
                )
            check_python(pol, file_path=p, sha256=current)
            try:
                verified_file_code = raw.decode("utf-8")
            except UnicodeDecodeError as e:
                raise CodePolicyError(
                    f"py.file は UTF-8 である必要があります: {p}"
                ) from e
        else:
            check_python(pol, file_path=None, sha256=step.sha256)
    except CodePolicyError as e:
        return await _code_step_error(
            base, step.on_error, "policy_violation", str(e), safe_shutdown,
        )

    # --- inputs 評価 ---
    ctx = store.as_ctx()
    inputs: dict[str, Any] = {}
    try:
        for local, expr in step.inputs.items():
            inputs[local] = evaluate(expr, ctx)
    except SeqExpressionError as e:
        return await _code_step_error(
            base, step.on_error, "py_input_error", str(e), safe_shutdown,
        )

    # --- subprocess 実行 ---
    try:
        outputs = await code_exec.run_py(
            code=verified_file_code if step.resolved_path else step.code,
            file_path=None,
            source_name=step.resolved_path or None,
            inputs=inputs,
            outputs=step.outputs,
            params=ctx["params"],
            env=ctx["env"],
            timeout_s=step.timeout_s,
        )
    except code_exec.CodeExecError as e:
        return await _code_step_error(
            base, step.on_error, f"py_{e.error}", e.message, safe_shutdown,
            traceback_text=e.traceback_text,
        )

    # --- outputs → vars.* (宣言分のみ) ---
    assigned: dict[str, Any] = {}
    for name in step.outputs:
        try:
            store.set_var(
                name, outputs[name],
                source_step_path=source_step_path,
                expr=f"py:{step.sha256[:12]}",
            )
        except (VariableStoreError, TypeError) as e:
            return await _code_step_error(
                base, step.on_error, "py_output_error",
                f"outputs '{name}': {e}", safe_shutdown,
            )
        if emit_event:
            emit_event("var_assigned", store.events[-1])
        assigned[name] = store.events[-1]["value"]

    # --- 来歴記録 (spec §5.7: file は sha256、code は全文) ---
    if emit_event:
        emit_event("py_executed", {
            "sha256": step.sha256,
            "file": step.file,
            "code": step.code if step.code is not None else None,
            "inputs": {k: _display(v) for k, v in inputs.items()},
            "outputs": assigned,
            "timeout_s": step.timeout_s,
            "step_path": source_step_path,
        })
    return {**base, "outputs": assigned, "success": True}


async def process_dll_step(
    step: DllStep,
    store: VariableStore,
    *,
    source_step_path: str = "",
    emit_event: EmitEvent | None = None,
    safe_shutdown: SafeShutdown | None = None,
    policy: Any = None,
) -> dict:
    """DllStep を専用ワーカー subprocess で実行する (spec §5.8)。

    **計算専用の位置付け** — 機器制御はバックエンドの役割。
    アクセス違反はワーカー死として回収しステップ failed (ランタイムは無事)。
    来歴: DLL の sha256 / path / function / 引数値を timeline ``dll_executed``
    に記録する。
    """
    from pathlib import Path as _Path

    from . import code_exec
    from .code_policy import CodePolicyError, check_dll, load_policy, sha256_file

    base = {
        "step_type": "dll",
        "path": step.path,
        "function": step.function,
        "sha256": step.sha256,
        "on_error": step.on_error,
    }

    # --- 実行直前のポリシー再検証 + sha256 再照合 (TOCTOU 対策) ---
    pol = policy if policy is not None else load_policy()
    try:
        p = _Path(step.path)
        if not p.exists():
            raise CodePolicyError(f"dll.path が存在しません: {p}")
        current = sha256_file(p)
        if current != step.sha256:
            raise CodePolicyError(
                f"DLL の内容がコンパイル時から変更されています "
                f"(sha256 不一致): {p}"
            )
        check_dll(pol, path=p, sha256=current)
    except CodePolicyError as e:
        return await _code_step_error(
            base, step.on_error, "policy_violation", str(e), safe_shutdown,
        )

    # --- args 評価 (文字列は参照式、数値はリテラル) ---
    ctx = store.as_ctx()
    resolved_args: list[Any] = []
    try:
        for a in step.args:
            resolved_args.append(evaluate(a, ctx) if isinstance(a, str) else a)
    except SeqExpressionError as e:
        return await _code_step_error(
            base, step.on_error, "dll_arg_error", str(e), safe_shutdown,
        )

    # --- 専用ワーカーで呼び出し ---
    try:
        resp = await code_exec.run_dll(
            path=step.path,
            function=step.function,
            argtypes=step.argtypes,
            restype=step.restype,
            args=resolved_args,
            out_args=step.out_args,
            timeout_s=step.timeout_s,
            expected_sha256=step.sha256,
        )
    except code_exec.CodeExecError as e:
        return await _code_step_error(
            base, step.on_error, f"dll_{e.error}", e.message, safe_shutdown,
            traceback_text=e.traceback_text,
        )

    # --- result_as (戻り値の capture) + out_args (バッファ回収) ---
    result_val = resp.get("result")
    try:
        if step.result_as and result_val is not None:
            store.set_step(
                step.result_as, result_val,
                source_step_path=source_step_path,
            )
            if emit_event:
                emit_event("var_assigned", store.events[-1])
        for name, arr in (resp.get("outputs") or {}).items():
            store.set_var(
                name, arr,
                source_step_path=source_step_path,
                expr=f"dll:{step.function}",
            )
            if emit_event:
                emit_event("var_assigned", store.events[-1])
    except (VariableStoreError, TypeError) as e:
        return await _code_step_error(
            base, step.on_error, "dll_output_error", str(e), safe_shutdown,
        )

    # --- 来歴記録 (spec §5.8: sha256 / path / function / 引数値) ---
    if emit_event:
        emit_event("dll_executed", {
            "sha256": step.sha256,
            "path": step.path,
            "function": step.function,
            "argtypes": list(step.argtypes),
            "restype": step.restype,
            "args": [_display(v) for v in resolved_args],
            "result": _display(result_val),
            "out_args": {k: v for k, v in step.out_args.items()},
            "timeout_s": step.timeout_s,
            "step_path": source_step_path,
        })
    return {
        **base,
        "result": _display(result_val),
        "outputs": {
            k: _display(v) for k, v in (resp.get("outputs") or {}).items()
        },
        "success": True,
    }
