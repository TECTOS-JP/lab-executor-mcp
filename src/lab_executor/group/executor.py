"""
v0.6.0 GroupExecutor

複数 TargetExecution を concurrency 制限付きで並列実行し、
partial_failure を正常系として集約する共通 executor。

設計原則 (実装方針 #11, #12):
- Map Job 全体は親 Job 1 つ (子 Job を作らない)
- 内部 concurrency で active targets を制限 (scheduler に 100 件一気に投げない)
- 結果順序は安定 (入力 target_id 順)
- partial_failure は status: "partial_failure" として正常系
- target 全体 retry のみ実装 (step 部分 retry は v0.6.x スコープ外)

呼び出し側は target を生成して run() を呼ぶだけ。
"""
from __future__ import annotations
import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, TYPE_CHECKING

from lab_executor.experiment_ir import (
    CommandStep, Plan, WaitStep,
    WaitUntilStep, WaitForConditionStep, WaitForStableStep,
    BarrierStep,
)
from lab_executor.group.barrier import BarrierCoordinator
from lab_executor.group.target import TargetExecution, FailurePolicy
from lab_executor.step_executor import execute_command_step
from lab_executor.polling_executor import (
    execute_wait_until,
    execute_wait_for_condition,
    execute_wait_for_stable,
)

if TYPE_CHECKING:
    # v1.11: backend / session 系は型ヒントだけ。実体は JobManager から
    # 注入 (`visa: VisaManager`, `session_resolver: Callable[..., InstrumentSession | None]`)。
    # v2.0 で `InstrumentBackend` Protocol 経由に置換予定。
    from visa_mcp.session_manager import InstrumentSession
    from visa_mcp.visa_manager import VisaManager

logger = logging.getLogger(__name__)


# target 1 つの結果
class TargetResult:
    __slots__ = (
        "target_id", "status", "data", "error_class", "error_message",
        "attempts", "elapsed_s", "steps_executed",
    )

    def __init__(
        self,
        target_id: str,
        status: str,
        data: dict | None = None,
        error_class: str | None = None,
        error_message: str | None = None,
        attempts: int = 1,
        elapsed_s: float = 0.0,
        steps_executed: list | None = None,
    ) -> None:
        # status: "ok" | "failed" | "skipped" | "cancelled"
        self.target_id = target_id
        self.status = status
        self.data = data or {}
        self.error_class = error_class
        self.error_message = error_message
        self.attempts = attempts
        self.elapsed_s = elapsed_s
        self.steps_executed = steps_executed or []

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "target_id": self.target_id,
            "status": self.status,
            "attempts": self.attempts,
            "elapsed_s": round(self.elapsed_s, 3),
        }
        if self.status == "ok":
            d["data"] = self.data
        if self.error_class:
            d["error_class"] = self.error_class
        if self.error_message:
            d["error_message"] = self.error_message
        if self.steps_executed:
            d["steps_executed"] = self.steps_executed
        return d


class GroupExecutor:
    """concurrency / failure_policy / partial_failure / retry を司る共通 executor

    cancel_check: 親 Job ランタイムからの cancel/timeout 要求を確認するコールバック。
                  非 None を返したら "理由文字列" (cancel|timeout)。
    on_progress:  進捗を runtime.current_progress に書き戻す callback。
                  渡される dict は target counts 等。

    v0.6.0.1: target-level resource lock を追加。
    同一 Group/Map Job 内部で同じ resource を共有する複数 target が
    concurrency により同時実行されてしまう問題を解消する。
    Job 間競合は ResourceScheduler が、Job 内 target 間競合はこの
    target-level lock が担う、という二段構成。
    """

    def __init__(
        self,
        visa: VisaManager,
        # alias_or_resource を InstrumentSession に解決する
        session_resolver: Callable[[str], InstrumentSession | None],
    ) -> None:
        self._visa = visa
        self._resolve_session = session_resolver
        # v0.6.0.1: target 単位 resource lock (Job 内 target 間排他)
        # canonical sorted で取得することで deadlock 回避
        self._target_locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, resource: str) -> asyncio.Lock:
        """resource ごとの target-level Lock を lazy 生成して返す"""
        lock = self._target_locks.get(resource)
        if lock is None:
            lock = asyncio.Lock()
            self._target_locks[resource] = lock
        return lock

    async def _acquire_target_resources(self, resources: list[str]) -> list[asyncio.Lock]:
        """target の required_resources を canonical sorted 順に取得する。

        全 lock を取得できるまで待機。順序固定で deadlock 回避。
        呼び出し側は finally で release_all() を呼ぶこと。
        """
        ordered = sorted(set(resources))
        acquired: list[asyncio.Lock] = []
        try:
            for r in ordered:
                lock = self._lock_for(r)
                await lock.acquire()
                acquired.append(lock)
            return acquired
        except Exception:
            # 取得済みを解放
            for lk in acquired:
                lk.release()
            raise

    @staticmethod
    def _release_target_resources(acquired: list[asyncio.Lock]) -> None:
        for lk in acquired:
            try:
                lk.release()
            except RuntimeError:
                pass

    async def run(
        self,
        targets: list[TargetExecution],
        *,
        concurrency: int = 10,
        failure_policy: FailurePolicy | None = None,
        override_safety: bool = False,
        override_reason: str = "",
        cancel_check: Callable[[], str | None] | None = None,
        on_progress: Callable[[dict], None] | None = None,
        on_event: Callable[[str, dict], None] | None = None,
    ) -> dict:
        """v0.7.0: on_event(event_type, payload) callback で
        target_started/completed/failed や barrier_arrived 等を JobStore に
        記録できる。指定なしなら no-op。"""
        """
        targets を並列実行する。返り値:
          {
            "status": "ok" | "partial_failure" | "error",
            "summary": {"total","success","failed","skipped","retried"},
            "results": [target_dict, ...]  # 入力順
            "errors": [{target_id, error_class, ...}, ...],
          }
        """
        failure_policy = failure_policy or FailurePolicy()
        failure_policy.validate()
        concurrency = max(1, int(concurrency))

        total = len(targets)
        # target_id 順を保持 (結果出力時の安定性)
        order_index: dict[str, int] = {t.target_id: i for i, t in enumerate(targets)}
        # v0.6.1: stagger 順序保証のため target_index を割り当て (入力順)
        for i, t in enumerate(targets):
            t.target_index = i

        results: dict[str, TargetResult] = {}
        retried_count = 0

        # v0.6.1: BarrierCoordinator (Job 内同期)
        barrier_coord = BarrierCoordinator()
        barrier_coord.register_targets([t.target_id for t in targets])

        # v0.6.1.1: stagger tracker (簡易、stagger 中 target → 開始予定時刻 / target_index / step)
        # _run_target_once が更新、_emit_progress が集約。
        stagger_tracker: dict[str, dict] = {}

        # control flags
        stop_requested = False  # failure_policy で未開始 target をスキップする要求
        cancel_reason: str | None = None  # cancel/timeout 検出時の理由

        # concurrency 制限
        sem = asyncio.Semaphore(concurrency)

        def _check_cancel() -> str | None:
            nonlocal cancel_reason
            if cancel_check is None:
                return None
            r = cancel_check()
            if r:
                cancel_reason = r
            return r

        def _emit_progress() -> None:
            if on_progress is None:
                return
            counts = _count_status(results, total)
            p = {
                "type": "group_or_map",
                "total": total,
                "queued": counts["queued"],
                "running": counts["running"],
                "completed": counts["completed"],
                "failed": counts["failed"],
                "skipped": counts["skipped"],
                "retrying": counts["retrying"],
            }
            # v0.6.1: 現在 active な barrier があれば付与
            br = barrier_coord.current_barrier_progress()
            if br is not None:
                p["barrier"] = br
            # v0.6.1.1: stagger 中の集約情報
            if stagger_tracker:
                now = time.monotonic()
                items = sorted(
                    stagger_tracker.values(),
                    key=lambda x: x.get("expected_start_at", 0.0),
                )
                in_progress = [it for it in items if it["expected_start_at"] > now]
                if in_progress:
                    next_item = in_progress[0]
                    p["stagger"] = {
                        "step_index": next_item.get("step_index"),
                        "command": next_item.get("command"),
                        "stagger_ms": next_item.get("stagger_ms"),
                        "in_stagger_count": len(in_progress),
                        "next_target_id": next_item.get("target_id"),
                        "next_start_in_s": max(
                            0.0, next_item["expected_start_at"] - now,
                        ),
                    }
            on_progress(p)

        async def _run_one(target: TargetExecution) -> None:
            nonlocal retried_count
            async with sem:
                # 取得直後に cancel / stop 要求を再確認
                if _check_cancel():
                    results[target.target_id] = TargetResult(
                        target.target_id, "skipped",
                        error_class="cancelled",
                        error_message=f"cancelled before start ({cancel_reason})",
                    )
                    _emit_progress()
                    return
                if stop_requested:
                    results[target.target_id] = TargetResult(
                        target.target_id, "skipped",
                        error_class="policy_stop",
                        error_message="未開始 target が failure_policy により skipped",
                    )
                    _emit_progress()
                    return

                # v0.6.0.1: target が要求する resource を Job 内 target 間で逐次化
                # (Job 間競合は親 ResourceScheduler が、Job 内 target 間競合はこの lock が担う)
                # 取得は canonical sorted 順で deadlock 回避。
                acquired_locks: list[asyncio.Lock] = []
                try:
                    acquired_locks = await self._acquire_target_resources(
                        target.required_resources,
                    )
                except asyncio.CancelledError:
                    results[target.target_id] = TargetResult(
                        target.target_id, "cancelled",
                        error_class="cancelled",
                        error_message="cancelled while acquiring target resources",
                    )
                    _emit_progress()
                    return

                # lock 取得後に再度 cancel / stop チェック (待っている間に状況変化)
                if _check_cancel():
                    self._release_target_resources(acquired_locks)
                    results[target.target_id] = TargetResult(
                        target.target_id, "skipped",
                        error_class="cancelled",
                        error_message=f"cancelled after lock acquire ({cancel_reason})",
                    )
                    _emit_progress()
                    return
                if stop_requested:
                    self._release_target_resources(acquired_locks)
                    results[target.target_id] = TargetResult(
                        target.target_id, "skipped",
                        error_class="policy_stop",
                        error_message="未開始 target が failure_policy により skipped",
                    )
                    _emit_progress()
                    return

                # mark running (progress 用)
                results[target.target_id] = TargetResult(target.target_id, "running")
                if on_event is not None:
                    try:
                        on_event("target_started", {
                            "target_id": target.target_id,
                            "required_resources": target.required_resources,
                            "bindings": dict(target.bindings),
                        })
                    except Exception:
                        pass
                _emit_progress()

                attempts = 0
                max_attempts = 1 + failure_policy.retry
                t0 = time.monotonic()
                last_result: TargetResult | None = None

                while attempts < max_attempts:
                    attempts += 1
                    if attempts > 1:
                        retried_count += 1
                    res = await self._run_target_once(
                        target,
                        override_safety=override_safety,
                        override_reason=override_reason,
                        cancel_check=_check_cancel,
                        # v0.6.1: barrier coordinator + target-level lock 制御を渡す
                        barrier_coord=barrier_coord,
                        acquired_locks=acquired_locks,
                        # v0.6.1.1: stagger progress 集約用
                        stagger_tracker=stagger_tracker,
                    )
                    res.attempts = attempts
                    res.elapsed_s = time.monotonic() - t0
                    last_result = res
                    # 終了状態が ok / cancelled / 致命的なら break
                    if res.status in ("ok", "cancelled"):
                        break
                    # retry policy 残量チェック
                    if attempts >= max_attempts:
                        break
                    # 次の retry まで間隔を空ける (短い)
                    await asyncio.sleep(0.05)

                # v0.6.1: target が失敗 / cancelled なら barrier から除外
                # (他 target が待ち続けて deadlock するのを防ぐ)
                if last_result and last_result.status != "ok":
                    barrier_coord.exclude_target(target.target_id)

                # v0.7.0: target_completed / target_failed event
                if on_event is not None and last_result is not None:
                    try:
                        ev_type = (
                            "target_completed" if last_result.status == "ok"
                            else "target_failed"
                        )
                        on_event(ev_type, {
                            "target_id": target.target_id,
                            "status": last_result.status,
                            "attempts": last_result.attempts,
                            "elapsed_s": last_result.elapsed_s,
                            "error_class": last_result.error_class,
                        })
                    except Exception:
                        pass

                results[target.target_id] = last_result or TargetResult(
                    target.target_id, "failed",
                    error_class="internal",
                    error_message="no result",
                )

                # v0.6.0.1: target-level resource lock 解放
                self._release_target_resources(acquired_locks)

                # failure_policy 評価
                _evaluate_policy()
                _emit_progress()

        def _evaluate_policy() -> None:
            nonlocal stop_requested
            if stop_requested:
                return
            if failure_policy.mode == "continue":
                return
            failed = sum(1 for r in results.values() if r.status == "failed")
            done = sum(1 for r in results.values() if r.status in ("ok", "failed"))
            if failure_policy.mode == "stop_on_first_error":
                if failed >= 1:
                    stop_requested = True
                    logger.info(
                        "group/map: stop_on_first_error - 未開始 target を skip",
                    )
            elif failure_policy.mode == "stop_if_failure_rate_exceeds":
                if done > 0:
                    rate = failed / done
                    if rate > failure_policy.stop_if_failure_rate_exceeds_threshold:
                        stop_requested = True
                        logger.info(
                            "group/map: failure_rate=%.2f > threshold, stop",
                            rate,
                        )

        # 全 target を並列起動 (semaphore で concurrency 制限)
        tasks = [asyncio.create_task(_run_one(t)) for t in targets]
        try:
            await asyncio.gather(*tasks, return_exceptions=False)
        except asyncio.CancelledError:
            # 親 Job が cancel された
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            # 未完 target を cancelled に
            for t in targets:
                if t.target_id not in results:
                    results[t.target_id] = TargetResult(
                        t.target_id, "cancelled",
                        error_class="cancelled",
                        error_message="parent job cancelled",
                    )
            raise

        # 入力順で結果整列
        ordered = sorted(
            results.values(), key=lambda r: order_index.get(r.target_id, 0),
        )

        counts = _count_status(results, total, final=True)
        # status 判定
        if counts["failed"] > 0 or counts["skipped"] > 0 or counts["cancelled"] > 0:
            if counts["completed"] > 0:
                status = "partial_failure"
            else:
                status = "error"
        else:
            status = "ok"

        errors = [
            {
                "target_id": r.target_id,
                "error_class": r.error_class or "unknown",
                "error_message": r.error_message,
                "recoverable": True,
            }
            for r in ordered
            if r.status in ("failed", "skipped", "cancelled")
        ]

        return {
            "status": status,
            "summary": {
                "total": counts["total"],
                "success": counts["completed"],
                "failed": counts["failed"],
                "skipped": counts["skipped"],
                "cancelled": counts["cancelled"],
                "retried": retried_count,
            },
            "results": [r.to_dict() for r in ordered],
            "errors": errors,
            "stopped_by_policy": stop_requested,
            "cancel_reason": cancel_reason,
        }

    # ---------- 1 target 実行 ----------

    async def _run_target_once(
        self,
        target: TargetExecution,
        *,
        override_safety: bool,
        override_reason: str,
        cancel_check: Callable[[], str | None],
        barrier_coord: "BarrierCoordinator | None" = None,
        acquired_locks: list[asyncio.Lock] | None = None,
        stagger_tracker: dict[str, dict] | None = None,
    ) -> TargetResult:
        """1 target を Plan に従って実行 (cancel/timeout 即応、step 失敗で halt)

        v0.6.1:
        - barrier_coord が指定されていれば BarrierStep を coordinator 経由で同期
        - barrier 待ち中は target-level lock を解放し、終了後に再取得 (deadlock 回避)
        - CommandStep.stagger_ms と target.target_index に基づいて step 開始遅延
        """
        step_results: list[dict] = []

        # CommandStep.instrument が "$role" の場合に bindings から resolve するヘルパ
        def _resolve_step_instrument(step) -> InstrumentSession | None:
            ref = getattr(step, "instrument", None)
            # explicit ref ない場合: target.bindings に primary がいたらそれ、
            # なければ required_resources[0] を session として返す
            if ref is None:
                # target が単一 resource なら自動でそれを使う
                if len(target.required_resources) == 1:
                    return self._resolve_session(target.required_resources[0])
                # 複数の場合は明示が必要 → None で呼び出し元エラー
                return None
            if ref.startswith("$"):
                role = ref[1:]
                alias = target.bindings.get(role)
                if not alias:
                    return None
                return self._resolve_session(alias)
            return self._resolve_session(ref)

        try:
            for idx, step in enumerate(target.plan.steps):
                r = cancel_check()
                if r:
                    return TargetResult(
                        target.target_id, "cancelled",
                        error_class="cancelled",
                        error_message=f"cancelled mid-target ({r})",
                        steps_executed=step_results,
                    )

                if isinstance(step, BarrierStep):
                    # v0.7.0: on_event hook が ある場合は GroupExecutor.run() の
                    # クロージャに保存された関数を呼ぶ。_run_target_once は
                    # クロージャ外なので、step result に barrier 情報を含める
                    # 既存挙動で十分。barrier_arrived/completed は run() 側で発火する
                    if barrier_coord is None:
                        # 単一 target 経路で barrier に到達した → no-op として通すか
                        # 不整合エラーかは仕様次第。execute_recipe では事前に reject 済み。
                        # safety net として success=True で素通し。
                        res = {
                            "step_type": "barrier", "success": True,
                            "barrier_name": step.name,
                            "note": "no coordinator (single-target path)",
                        }
                    else:
                        # v0.6.1: barrier 待ち中は target-level resource lock を解放。
                        # これにより 「target1 が lock を持ったまま target2 を待つ」
                        # deadlock を回避する。親 Job lock があるので外部からは触られない。
                        released_locks = acquired_locks or []
                        for lk in released_locks:
                            try:
                                lk.release()
                            except RuntimeError:
                                pass
                        try:
                            br_res = await barrier_coord.arrive(
                                name=step.name,
                                step_index=idx,
                                target_id=target.target_id,
                                timeout_s=step.timeout_s,
                                cancel_check=cancel_check,
                            )
                        finally:
                            # 再取得 (lock 順序固定で他 target との deadlock 回避)
                            # canonical sorted は acquired_locks の元順序が保たれている前提
                            # (acquired は _acquire_target_resources 内で sorted 取得済み)
                            for lk in released_locks:
                                await lk.acquire()
                        res = {"step_type": "barrier", **br_res}
                elif isinstance(step, CommandStep):
                    # v0.6.1: stagger 適用 (CommandStep.stagger_ms が指定されていれば)
                    if step.stagger_ms is not None and step.stagger_ms > 0:
                        stagger_s = step.stagger_ms / 1000.0 * target.target_index
                        if stagger_s > 0:
                            # v0.6.1.1: tracker に登録 (progress 公開用)
                            t_now = time.monotonic()
                            if stagger_tracker is not None:
                                stagger_tracker[target.target_id] = {
                                    "target_id": target.target_id,
                                    "step_index": idx,
                                    "command": step.command,
                                    "stagger_ms": step.stagger_ms,
                                    "expected_start_at": t_now + stagger_s,
                                }
                            # slice 方式で cancel 即応
                            from lab_executor.polling_executor import POLL_SLEEP_SLICE_S
                            remaining = stagger_s
                            try:
                                while remaining > 0:
                                    r = cancel_check()
                                    if r:
                                        return TargetResult(
                                            target.target_id, "cancelled",
                                            error_class="cancelled",
                                            error_message=f"cancelled during stagger ({r})",
                                            steps_executed=step_results,
                                        )
                                    chunk = min(remaining, POLL_SLEEP_SLICE_S)
                                    await asyncio.sleep(chunk)
                                    remaining -= chunk
                            finally:
                                if stagger_tracker is not None:
                                    stagger_tracker.pop(target.target_id, None)

                    session = _resolve_step_instrument(step)
                    if session is None or session.definition is None:
                        return TargetResult(
                            target.target_id, "failed",
                            error_class="not_found",
                            error_message=(
                                f"step {idx}: instrument 解決失敗 "
                                f"(ref={getattr(step, 'instrument', None)!r}, "
                                f"bindings={target.bindings})"
                            ),
                            steps_executed=step_results,
                        )
                    res = await execute_command_step(
                        self._visa, session, step,
                        override_safety=override_safety,
                        override_reason=override_reason,
                    )
                elif isinstance(step, WaitStep):
                    # group/map 内 wait は短時間想定 → asyncio.sleep を slice
                    res = await self._sleep_with_cancel(step.seconds, cancel_check)
                    if res.get("interrupted_by_cancel"):
                        return TargetResult(
                            target.target_id, "cancelled",
                            error_class="cancelled",
                            steps_executed=step_results + [{"step": idx, **res}],
                        )
                elif isinstance(step, WaitUntilStep):
                    res = await execute_wait_until(step, cancel_check=cancel_check)
                elif isinstance(step, WaitForConditionStep):
                    # 解決: ref が "$role" の場合は bindings 経由
                    res = await execute_wait_for_condition(
                        self._visa,
                        lambda name: self._resolve_polling_session(name, target),
                        step,
                        cancel_check=cancel_check,
                    )
                elif isinstance(step, WaitForStableStep):
                    res = await execute_wait_for_stable(
                        self._visa,
                        lambda name: self._resolve_polling_session(name, target),
                        step,
                        cancel_check=cancel_check,
                    )
                else:
                    res = {
                        "success": False,
                        "error": "UnsupportedStepType",
                        "step_type": getattr(step, "type", "?"),
                    }

                step_results.append({"step": idx, **res})

                if not res.get("success", False):
                    if res.get("interrupted_by_cancel"):
                        return TargetResult(
                            target.target_id, "cancelled",
                            error_class="cancelled",
                            steps_executed=step_results,
                        )
                    err_class = res.get("error", "internal")
                    return TargetResult(
                        target.target_id, "failed",
                        error_class=err_class,
                        error_message=str(
                            res.get("message", res.get("error", "?"))
                        )[:200],
                        steps_executed=step_results,
                    )

            # 全 step ok
            return TargetResult(
                target.target_id, "ok",
                data={"step_count": len(step_results)},
                steps_executed=step_results,
            )

        except asyncio.CancelledError:
            return TargetResult(
                target.target_id, "cancelled",
                error_class="cancelled",
                error_message="task cancelled",
                steps_executed=step_results,
            )
        except Exception as e:
            logger.exception("target %s で予期しないエラー", target.target_id)
            return TargetResult(
                target.target_id, "failed",
                error_class="internal",
                error_message=str(e),
                steps_executed=step_results,
            )

    def _resolve_polling_session(
        self, ref: str, target: TargetExecution,
    ) -> InstrumentSession | None:
        """polling step の instrument ref を解決 (target.bindings を考慮)"""
        if ref.startswith("$"):
            role = ref[1:]
            alias = target.bindings.get(role)
            if not alias:
                return None
            return self._resolve_session(alias)
        return self._resolve_session(ref)

    async def _sleep_with_cancel(
        self, seconds: float,
        cancel_check: Callable[[], str | None],
    ) -> dict:
        """slice 方式 sleep (group target 内専用)"""
        from lab_executor.polling_executor import POLL_SLEEP_SLICE_S
        remaining = float(seconds)
        elapsed = 0.0
        while remaining > 0:
            r = cancel_check()
            if r:
                return {
                    "step_type": "wait",
                    "success": False,
                    "interrupted_by_cancel": True,
                    "error": r,
                    "elapsed_s": elapsed,
                }
            chunk = min(remaining, POLL_SLEEP_SLICE_S)
            await asyncio.sleep(chunk)
            remaining -= chunk
            elapsed += chunk
        return {"step_type": "wait", "success": True, "seconds": seconds}


def _count_status(
    results: dict[str, TargetResult], total: int, final: bool = False,
) -> dict[str, int]:
    """進捗 progress と最終 summary 双方で使う counts。

    final=True なら "queued" / "retrying" は 0 として返す (実行終了後の集計)。
    """
    completed = sum(1 for r in results.values() if r.status == "ok")
    failed = sum(1 for r in results.values() if r.status == "failed")
    skipped = sum(1 for r in results.values() if r.status == "skipped")
    cancelled = sum(1 for r in results.values() if r.status == "cancelled")
    running = sum(1 for r in results.values() if r.status == "running")
    queued = max(0, total - len(results))
    return {
        "total": total,
        "queued": queued if not final else 0,
        "running": running if not final else 0,
        "completed": completed,
        "failed": failed,
        "skipped": skipped,
        "cancelled": cancelled,
        "retrying": 0,  # v0.6.0 では別途集計しない
    }
