"""
Job Manager + Executor (v0.5.0)

- JobManager: recipe を非同期 Job として登録・追跡・キャンセル
- バックグラウンドは asyncio.create_task で実行
- 状態は SQLite (JobStore) に同期
- cancel_mode は immediate / after_current_step / safe_shutdown の 3 種類
- recipe Step 単位で cancel チェック (各 step 開始前に確認)
- WaitStep 実行中は短いインターバルで cancel チェックして即時停止可能
- job_timeout_s で TIMEOUT 自動遷移 (v0.5.0 追加)
"""
from __future__ import annotations
import asyncio
import logging
import time
import uuid
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    # v1.11: backend layer の top-level import を排除
    # (KNOWN_V111_TO_RESOLVE 解消)。JobManager は外部 (server / CLI)
    # から SessionManager / VisaManager を注入される composition root の
    # 一段下。v2.0 では `InstrumentBackend` Protocol を持つ。
    from visa_mcp.session_manager import SessionManager
    from visa_mcp.visa_manager import VisaManager

from lab_executor.experiment_ir import (
    CommandStep, Plan, WaitStep,
    WaitUntilStep, WaitForConditionStep, WaitForStableStep,
)
from lab_executor.job.state_machine import (
    CancelMode,
    JobStatus,
    is_terminal,
)
from lab_executor.job.store import JobStore, JobRecord
from lab_executor.job.scheduler import (
    ResourceScheduler,
    ResourceBusyError,
    QueuePolicy,
)
from lab_executor.recipe_executor import recipe_to_plan
from lab_executor.step_executor import execute_command_step, execute_wait_step
from lab_executor.polling_executor import (
    execute_wait_until,
    execute_wait_for_condition,
    execute_wait_for_stable,
    _do_one_poll,
    POLL_SLEEP_SLICE_S,
)
# v0.6.0: group / map
from lab_executor.group import (
    TargetExecution, FailurePolicy,
    resolve_resource, resolve_unit_bindings, collect_target_resources,
    ResolveError,
)
from lab_executor.group.executor import GroupExecutor
from lab_executor.system_config import SystemConfig

logger = logging.getLogger(__name__)


# wait step を細かいスライスに分割して cancel/timeout に即応するためのインターバル
_WAIT_SLICE_S = 0.2

# job_timeout_s デフォルト (24時間)
DEFAULT_JOB_TIMEOUT_S: float = 86400.0


class JobNotFoundError(Exception):
    pass


class JobAlreadyTerminalError(Exception):
    pass


class _JobRuntime:
    """asyncio.Task と cancel 要求フラグ / 期限の組"""

    def __init__(self, task: asyncio.Task, deadline: float | None) -> None:
        self.task = task
        self.cancel_mode: CancelMode | None = None  # 設定されたら cancel 要求中
        self.deadline = deadline                     # time.monotonic() 基準。None なら無期限
        # v0.5.0.3: queue 待ちから起動可能になった通知用。
        self._start_event: asyncio.Event = asyncio.Event()
        # v0.5.1: 現在の polling 進捗 (get_job_status で公開)。polling 系 step が更新する。
        self.current_progress: dict | None = None

    def is_timed_out(self) -> bool:
        return self.deadline is not None and time.monotonic() >= self.deadline

    def remaining_s(self) -> float | None:
        if self.deadline is None:
            return None
        return max(0.0, self.deadline - time.monotonic())


def _dsl_params(
    plan_id: str, plan_dict: dict[str, Any], compiled,
) -> dict[str, Any]:
    """v0.8.3: jobs.parameters に保存する DSL 固有メタ。
    template_source は compiled.summary["template_source"] 経由で伝播する
    (start_experiment_job が呼び出し前に summary に attach 済み)。
    """
    p: dict[str, Any] = {
        "plan_id": plan_id,
        "dsl_version": plan_dict.get("dsl_version"),
    }
    ts = (compiled.summary or {}).get("template_source")
    if ts:
        p["template_source"] = dict(ts)
    return p


def _merge_parallel_params(
    plan_id: str, plan_dict: dict[str, Any], compiled, branch_count: int,
) -> dict[str, Any]:
    p = _dsl_params(plan_id, plan_dict, compiled)
    p["branch_count"] = branch_count
    return p


class JobManager:
    """
    バックグラウンド Job の管理。

    使い方:
        manager = JobManager(visa, session_mgr, store)
        job_id = await manager.start_recipe_job("GPIB0::1::INSTR", "safe_output_on", {"target_v": 5})
        await manager.cancel(job_id, CancelMode.SAFE_SHUTDOWN)
    """

    def __init__(
        self,
        visa: "VisaManager | None" = None,
        session_mgr: "SessionManager | None" = None,
        store: JobStore | None = None,
        scheduler: ResourceScheduler | None = None,
        system_config: SystemConfig | None = None,
        *,
        backend: "VisaManager | None" = None,
    ) -> None:
        # v2.2.0: `backend=` keyword を推奨。`visa=` は互換維持
        # (lab-executor-mcp v3.x で削除候補)。
        if backend is not None and visa is not None:
            raise TypeError(
                "JobManager: cannot pass both `visa=` and `backend=`. "
                "Use `backend=` (v2.2+ recommended)."
            )
        if backend is None and visa is None:
            raise TypeError(
                "JobManager: either `backend=` (recommended) or "
                "`visa=` (deprecated) must be provided."
            )
        if backend is None:
            import warnings as _w
            _w.warn(
                "JobManager(visa=...) is deprecated since v2.2.0; "
                "use JobManager(backend=...) instead.",
                DeprecationWarning,
                stacklevel=2,
            )
            backend = visa
        if session_mgr is None:
            raise TypeError(
                "JobManager: session_mgr is required."
            )
        self._visa = backend
        self._sessions = session_mgr
        self._store = store or JobStore()
        self._runtimes: dict[str, _JobRuntime] = {}
        # v0.5.0.2: Job 単位排他のための ResourceScheduler
        self._scheduler = scheduler or ResourceScheduler()
        # v0.6.0: SystemConfig (alias/bus/groups/units 解決用)
        self._system_config = system_config or SystemConfig()
        # 起動時に running/waiting/cancelling/queued を interrupted に遷移
        self._store.mark_interrupted_on_startup()
        # v0.9.3: AuditStore + stale lock 解放
        self._audit_init_error = False
        try:
            from lab_executor.audit import AuditStore
            self._audit = AuditStore(self._store)
            released = self._audit.release_stale_locks()
            self._audit.record_event(
                "server_started",
                severity="info",
                metadata={"stale_locks_released": released,
                           "schema_version": 3},
            )
        except Exception:
            # v0.9.3.1: no-op 化を visible に (stderr warning + 後段 query で
            # status=audit_unavailable を返せるよう _audit_init_error を保持)
            logger.warning(
                "AuditStore 初期化失敗: audit / locks 機能が無効化されます。"
                "audit table の永続化は行われません。"
            )
            self._audit = None
            self._audit_init_error = True

    @property
    def system_config(self) -> SystemConfig:
        return self._system_config

    def set_system_config(self, cfg: SystemConfig) -> None:
        """ランタイムで system_config を差し替え (reload 対応)"""
        self._system_config = cfg

    def _session_for_alias_or_resource(self, ref: str) -> Any:
        """alias 経由 resource 解決して session を返すヘルパ。

        - ref が alias なら system_config から resource_name に変換
        - ref が resource なら素通し
        - どちらでもなければ get_session(ref) で再試行 (legacy)
        """
        try:
            resource = self._system_config.resolve_alias(ref) or ref
        except Exception:
            resource = ref
        return self._sessions.get_session(resource)

    @property
    def store(self) -> JobStore:
        return self._store

    @property
    def audit(self):
        """v0.9.3: AuditStore へのアクセス (experimental)"""
        return self._audit

    @property
    def session_manager(self) -> SessionManager:
        """Public accessor for the SessionManager (v0.8.2.1, used by Observation API)."""
        return self._sessions

    @property
    def scheduler(self) -> ResourceScheduler:
        return self._scheduler

    # ---------- public API ----------

    # v0.7.0.1: critical event (失敗時に Job result に警告を残す対象)
    _CRITICAL_EVENT_TYPES = frozenset({
        "verify_failed",
        "safe_shutdown_failed",
        "safe_shutdown_started",
        "safe_shutdown_completed",
        "job_failed",
        "job_cancelled",
        "job_interrupted",
        "job_timeout",
        "barrier_timeout",
        "target_failed",
        "step_failed",
    })

    def _safe_record_event(
        self,
        job_id: str,
        event_type: str,
        *,
        target_id: str | None = None,
        step_index: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """job_events 記録のラッパー (例外を握りつぶす)。

        v0.7.0.1: critical event の永続化失敗時は、runtime に
        persistence_warnings として記録し、最終 Job result に含める
        (実験安全・監査の観点で、書き込めなかった事実を必ず可視化する)。
        """
        try:
            self._store.record_event(
                job_id, event_type, target_id=target_id,
                step_index=step_index, payload=payload,
            )
        except Exception as e:
            logger.debug("record_event 失敗 (event=%s): %s", event_type, e)
            if event_type in self._CRITICAL_EVENT_TYPES:
                # critical event は visibility 確保のため warn ログ + runtime 保持
                logger.warning(
                    "[persistence_warning] critical event '%s' の DB 書き込み失敗: %s",
                    event_type, e,
                )
                rt = self._runtimes.get(job_id)
                if rt is not None:
                    if not hasattr(rt, "persistence_warnings"):
                        rt.persistence_warnings = []  # type: ignore[attr-defined]
                    rt.persistence_warnings.append({  # type: ignore[attr-defined]
                        "event_type": event_type,
                        "target_id": target_id,
                        "step_index": step_index,
                        "error": str(e),
                    })

    def _consume_persistence_warnings(self, job_id: str) -> list[dict] | None:
        """runtime の persistence_warnings を取り出す (なければ None)"""
        rt = self._runtimes.get(job_id)
        if rt is None:
            return None
        w = getattr(rt, "persistence_warnings", None)
        return list(w) if w else None

    async def start_recipe_job(
        self,
        resource_name: str,
        recipe_name: str,
        parameters: dict[str, Any] | None,
        *,
        owner: str = "",
        override_safety: bool = False,
        override_reason: str = "",
        job_timeout_s: float | None = None,
        queue_policy: QueuePolicy = "queue",
    ) -> JobRecord:
        """
        recipe を Job として登録し、scheduler 経由で bg 実行を開始する。

        v0.5.0.2 変更: 同一 resource への Job が既に running の場合、
        新しい Job は **queued** 状態で待機する (queue_policy="queue" デフォルト)。
        queue_policy="reject_if_busy" を指定すれば busy 時に即 failed を返す。

        起動失敗 (定義なし / 必須パラメータ欠落) は SQLite 上で failed として記録した上で返す。

        job_timeout_s: 全体の実行制限秒数 (None なら DEFAULT_JOB_TIMEOUT_S = 24h)。
                       経過すると Job は自動で TIMEOUT 状態に遷移する。
                       wait 中も含む全実行時間が対象。
        queue_policy: "queue" (デフォルト、busy 時は queued で順番待ち) /
                      "reject_if_busy" (busy 時は failed)
        """
        parameters = parameters or {}
        session = self._sessions.get_session(resource_name)
        if session is None or session.definition is None:
            return self._record_immediate_failure(
                resource_name, recipe_name, parameters,
                error_class="not_found",
                summary=f"{resource_name} は未識別、または YAML 定義がありません。",
            )
        recipe = session.definition.recipes.get(recipe_name)
        if recipe is None:
            return self._record_immediate_failure(
                resource_name, recipe_name, parameters,
                error_class="not_found",
                summary=f"recipe '{recipe_name}' は定義されていません",
            )
        # 必須パラメータチェック
        for p in recipe.parameters:
            if p.required and p.name not in parameters and p.default is None:
                return self._record_immediate_failure(
                    resource_name, recipe_name, parameters,
                    error_class="validation",
                    summary=f"必須パラメータ '{p.name}' が指定されていません",
                )

        # JOB 登録 (queued 状態)
        job_id = self._new_job_id()
        rec = self._store.create_job(
            job_id=job_id,
            owner=owner,
            resource_name=resource_name,
            recipe=recipe_name,
            parameters=parameters,
        )

        # v0.5.1: required_resources を Plan ビルド時に決定 (polling 対象 instrument も含む)
        # ここでは validation を兼ねて Plan を試し build する (式評価エラー等を早期検出)
        # 失敗しても scheduler には primary のみを登録し、_run_job_inner で再 build & 適切に failed 化
        try:
            variables = dict(parameters)
            for p in recipe.parameters:
                if p.name not in variables and p.default is not None:
                    variables[p.name] = p.default
            tentative_plan = recipe_to_plan(
                recipe, variables, primary_resource=resource_name,
            )
            required_resources = list(tentative_plan.required_resources) or [resource_name]
        except Exception:
            # build 失敗時は単一 resource で scheduler に投入し、_run_job_inner で failed にする
            required_resources = [resource_name]

        # scheduler に投入
        try:
            immediate, blocking = await self._scheduler.enqueue(
                job_id, required_resources, queue_policy=queue_policy,
            )
        except ResourceBusyError as e:
            return self._store.transition_status(
                job_id, JobStatus.FAILED,
                error_class="blocked",
                last_step_summary=f"resource busy (blocked by {e.blocking_job_id})",
                result={
                    "success": False,
                    "error": "ResourceBusy",
                    "message": str(e),
                    "blocking_job_id": e.blocking_job_id,
                    "queue_policy": queue_policy,
                },
            )

        # タイムアウト計算 (queued 期間も含む = ユーザー視点での "始めてから")
        effective_timeout = (
            DEFAULT_JOB_TIMEOUT_S if job_timeout_s is None else float(job_timeout_s)
        )
        deadline = time.monotonic() + effective_timeout if effective_timeout > 0 else None

        # バックグラウンドタスクとして起動 (queue 待ちから running まで _run_job 内で管理)
        task = asyncio.create_task(
            self._run_job(
                rec,
                required_resources=required_resources,
                override_safety=override_safety,
                override_reason=override_reason,
                start_immediately=immediate,
            ),
            name=f"job-{job_id}",
        )
        self._runtimes[job_id] = _JobRuntime(task, deadline)

        if not immediate:
            # queue 待ち情報を last_step_summary に
            self._store.update_step(
                job_id, -1,
                last_step_summary=f"queued, blocked_by={blocking}",
            )

        return rec

    async def start_wait_job(
        self,
        *,
        wait_type: str,
        params: dict[str, Any],
        owner: str = "",
        job_timeout_s: float | None = None,
        queue_policy: QueuePolicy = "queue",
    ) -> JobRecord:
        """
        v0.5.1: 単発の wait ジョブを起動する。

        wait_type:
          - "seconds":     params={"seconds": float}                       resource 不要
          - "until":       params={"timestamp": ISO8601} or {"seconds_from_now": float}
          - "condition":   params={"instrument", "command", "condition_expr",
                                   "args"?, "interval_s"?, "timeout_s"?,
                                   "value_path"?, ...}
          - "stable_value":params={"instrument", "command", "tolerance", "window_s",
                                   "args"?, "interval_s"?, "timeout_s"?, ...}

        seconds/until は required_resources=[] (scheduler 即起動)。
        condition/stable_value は params["instrument"] を required_resources に含める。
        """
        # IR Step を構築
        try:
            step = self._build_wait_step(wait_type, params)
        except Exception as e:
            return self._record_immediate_failure(
                resource_name="",
                recipe_name=f"wait_{wait_type}",
                parameters=params,
                error_class="validation",
                summary=f"start_wait_job validation: {e}",
            )

        # 単一ステップ Plan
        plan = Plan(
            name=f"wait_{wait_type}",
            steps=[step],
            required_resources=self._extract_resources_from_step(step),
        )

        # JOB 登録
        primary = plan.required_resources[0] if plan.required_resources else ""
        job_id = self._new_job_id()
        rec = self._store.create_job(
            job_id=job_id,
            owner=owner,
            resource_name=primary,
            recipe=f"<wait_{wait_type}>",
            parameters=params,
        )

        # scheduler enqueue
        try:
            immediate, blocking = await self._scheduler.enqueue(
                job_id, list(plan.required_resources), queue_policy=queue_policy,
            )
        except ResourceBusyError as e:
            return self._store.transition_status(
                job_id, JobStatus.FAILED,
                error_class="blocked",
                last_step_summary=f"resource busy (blocked by {e.blocking_job_id})",
                result={
                    "success": False, "error": "ResourceBusy",
                    "message": str(e),
                    "blocking_job_id": e.blocking_job_id,
                    "queue_policy": queue_policy,
                },
            )

        effective_timeout = (
            DEFAULT_JOB_TIMEOUT_S if job_timeout_s is None else float(job_timeout_s)
        )
        deadline = time.monotonic() + effective_timeout if effective_timeout > 0 else None

        task = asyncio.create_task(
            self._run_wait_job(
                rec, plan,
                required_resources=list(plan.required_resources),
                start_immediately=immediate,
            ),
            name=f"job-{job_id}",
        )
        self._runtimes[job_id] = _JobRuntime(task, deadline)

        if not immediate:
            self._store.update_step(
                job_id, -1,
                last_step_summary=f"queued, blocked_by={blocking}",
            )

        return rec

    def _build_wait_step(self, wait_type: str, params: dict[str, Any]):
        """wait_type + params から IR Step を構築 (validation 込み)

        params の必須キーが欠落していた場合は分かりやすい ValueError を raise する
        (KeyError だと LLM 向けエラーメッセージが空文字 'seconds' のように
        不親切になるため)。
        """
        def _require(key: str) -> Any:
            if key not in params:
                raise ValueError(
                    f"start_wait_job(wait_type={wait_type!r}): "
                    f"params に必須キー '{key}' がありません"
                )
            return params[key]

        if wait_type == "seconds":
            return WaitStep(seconds=float(_require("seconds")))
        if wait_type == "until":
            ts = params.get("timestamp")
            sec = params.get("seconds_from_now")
            if (ts in (None, "")) and (sec is None):
                raise ValueError(
                    "start_wait_job(wait_type='until'): "
                    "params に 'timestamp' または 'seconds_from_now' のいずれかが必須です"
                )
            return WaitUntilStep(
                timestamp=ts if ts not in (None, "") else None,
                seconds_from_now=(float(sec) if sec is not None else None),
            )
        if wait_type == "condition":
            return WaitForConditionStep(
                instrument=_require("instrument"),
                command=_require("command"),
                args=params.get("args") or {},
                condition_expr=_require("condition_expr"),
                interval_s=float(params.get("interval_s", 1.0)),
                timeout_s=float(params.get("timeout_s", 60.0)),
                command_timeout_s=(
                    float(params["command_timeout_s"])
                    if params.get("command_timeout_s") is not None else None
                ),
                value_path=params.get("value_path"),
                retry_on_error=int(params.get("retry_on_error", 1)),
                max_consecutive_errors=int(params.get("max_consecutive_errors", 3)),
            )
        if wait_type == "stable_value":
            return WaitForStableStep(
                instrument=_require("instrument"),
                command=_require("command"),
                args=params.get("args") or {},
                tolerance=float(_require("tolerance")),
                window_s=float(_require("window_s")),
                interval_s=float(params.get("interval_s", 1.0)),
                timeout_s=float(params.get("timeout_s", 60.0)),
                command_timeout_s=(
                    float(params["command_timeout_s"])
                    if params.get("command_timeout_s") is not None else None
                ),
                value_path=params.get("value_path"),
                min_samples=int(params.get("min_samples", 3)),
                retry_on_error=int(params.get("retry_on_error", 1)),
                max_consecutive_errors=int(params.get("max_consecutive_errors", 3)),
            )
        raise ValueError(
            f"未知の wait_type: {wait_type} "
            f"(valid: seconds / until / condition / stable_value)"
        )

    @staticmethod
    def _extract_resources_from_step(step) -> list[str]:
        if isinstance(step, (WaitForConditionStep, WaitForStableStep)):
            return [step.instrument]
        return []

    async def _run_wait_job(
        self,
        rec: JobRecord,
        plan: Plan,
        *,
        required_resources: list[str],
        start_immediately: bool,
    ) -> None:
        """単発 wait ジョブの bg 実行 (recipe を介さない)"""
        job_id = rec.job_id
        try:
            if not start_immediately:
                await self._wait_until_scheduled(job_id)
            await self._scheduler.on_running(job_id)

            runtime = self._runtimes.get(job_id)
            if runtime is None:
                return
            current = self._store.get(job_id)
            if current is not None and is_terminal(current.status):
                return

            self._store.transition_status(job_id, JobStatus.RUNNING, current_step_index=0)
            step = plan.steps[0]
            self._store.update_step(
                job_id, 0, last_step_summary=self._step_summary(step),
            )

            self._safe_transition(job_id, JobStatus.WAITING)
            try:
                if isinstance(step, WaitStep):
                    result = await self._run_wait_with_cancel_check(step, runtime)
                elif isinstance(step, WaitUntilStep):
                    result = await execute_wait_until(
                        step,
                        cancel_check=lambda: self._poll_cancel_reason(runtime),
                        on_progress=lambda p: self._update_progress(runtime, p),
                    )
                elif isinstance(step, WaitForConditionStep):
                    result = await execute_wait_for_condition(
                        self._visa, self._sessions.get_session, step,
                        cancel_check=lambda: self._poll_cancel_reason(runtime),
                        on_progress=lambda p: self._update_progress(runtime, p),
                    )
                elif isinstance(step, WaitForStableStep):
                    result = await execute_wait_for_stable(
                        self._visa, self._sessions.get_session, step,
                        cancel_check=lambda: self._poll_cancel_reason(runtime),
                        on_progress=lambda p: self._update_progress(runtime, p),
                    )
                else:
                    result = {"success": False, "error": "UnsupportedWaitType"}
            finally:
                runtime.current_progress = None

            if result.get("success"):
                self._store.transition_status(
                    job_id, JobStatus.COMPLETED,
                    current_step_index=0,
                    last_step_summary="wait completed",
                    result={
                        "success": True, "recipe": rec.recipe,
                        "steps_executed": [{"step": 0, **result}],
                        "step_count": 1,
                    },
                )
            elif result.get("interrupted_by_timeout"):
                self._record_timeout(rec, 0, [{"step": 0, **result}])
            elif result.get("interrupted_by_cancel"):
                # cancel 経路へ
                self._safe_transition(job_id, JobStatus.CANCELLING)
                self._store.transition_status(
                    job_id, JobStatus.CANCELLED,
                    error_class="cancelled",
                    last_step_summary="wait cancelled",
                    result={
                        "success": False, "recipe": rec.recipe,
                        "steps_executed": [{"step": 0, **result}],
                        "cancelled": True,
                        "cancel_mode": (
                            runtime.cancel_mode.value if runtime.cancel_mode else "?"
                        ),
                    },
                )
            else:
                self._store.transition_status(
                    job_id, JobStatus.FAILED,
                    current_step_index=0,
                    error_class=result.get("error", "internal"),
                    last_step_summary=str(result.get("message", result.get("error", "?")))[:80],
                    result={
                        "success": False, "recipe": rec.recipe,
                        "steps_executed": [{"step": 0, **result}],
                        "halted_at_step": 0,
                    },
                )

        except asyncio.CancelledError:
            self._safe_transition(job_id, JobStatus.CANCELLING)
            try:
                self._store.transition_status(
                    job_id, JobStatus.CANCELLED,
                    error_class="cancelled",
                    last_step_summary="cancelled (immediate)",
                    result={
                        "success": False, "recipe": rec.recipe,
                        "cancelled": True, "cancel_mode": "immediate",
                    },
                )
            except Exception:
                pass
            raise
        except Exception as e:
            logger.exception("wait job %s で予期しないエラー", job_id)
            self._store.transition_status(
                job_id, JobStatus.FAILED,
                error_class="internal",
                last_step_summary=f"unexpected: {e}",
                result={"success": False, "error": "InternalError", "message": str(e)},
            )
        finally:
            try:
                next_jobs = await self._scheduler.on_terminal(job_id, required_resources)
                for nj_id in next_jobs:
                    self._wake_queued_job(nj_id)
            except Exception:
                pass
            self._runtimes.pop(job_id, None)

    # =====================================================================
    # v0.8.0: Experiment DSL Job
    # =====================================================================

    async def start_experiment_job(
        self,
        plan_dict: dict[str, Any],
        *,
        owner: str = "",
        override_safety: bool = False,
        override_reason: str = "",
        job_timeout_s: float | None = None,
        queue_policy: QueuePolicy = "queue",
        template_source: dict[str, Any] | None = None,
    ) -> JobRecord:
        """v0.8.0: DSL plan を validate + compile + 実行する。

        plan_dict は ExperimentPlan に準拠した JSON (LLM 出力想定)。

        フロー:
          1. dsl.compiler.validate_and_compile() で validate + compile
          2. experiment_plans テーブルに persist
          3. compiled main_plan を既存 Recipe Job 実行経路で走らせる
             (parallel が含まれる場合は GroupExecutor に転送)
          4. plan 末尾の safe_shutdown step は終端時に
             best_effort_safe_shutdown を実行
        """
        import uuid as _uuid
        from lab_executor.dsl.compiler import validate_and_compile

        # validate + compile
        compiled = validate_and_compile(plan_dict, self._sessions, self._system_config)
        plan_id = f"plan_{_uuid.uuid4().hex[:12]}"
        plan_name = plan_dict.get("name") or "experiment"

        if not compiled.valid:
            # validation 失敗: job 作成し、queued → failed に 1 度だけ遷移する。
            # _record_immediate_failure を使うと validation_errors を result に
            # 入れられないため、直接遷移を行う。
            job_id = self._new_job_id()
            rec = self._store.create_job(
                job_id=job_id, owner=owner,
                resource_name="", recipe=f"<dsl:{plan_name}>",
                parameters={"plan_id": plan_id},
            )
            try:
                self._store.save_experiment_plan(
                    plan_id=plan_id, job_id=job_id,
                    name=plan_name,
                    dsl_version=plan_dict.get("dsl_version", "0.8"),
                    original_plan=plan_dict,
                    validation_result={
                        "valid": False,
                        "errors": compiled.errors,
                        "warnings": compiled.warnings,
                    },
                )
            except Exception:
                pass
            return self._store.transition_status(
                job_id, JobStatus.FAILED,
                error_class="validation",
                last_step_summary=(
                    f"DSL validation failed ({len(compiled.errors)} errors)"
                ),
                result={
                    "success": False, "error": "ValidationError",
                    "plan_id": plan_id,
                    "validation_errors": compiled.errors,
                    "validation_warnings": compiled.warnings,
                },
            )

        # v0.8.3: template_source を summary に attach (なければ no-op)
        if template_source:
            try:
                compiled.summary["template_source"] = dict(template_source)
            except Exception:
                pass

        # compile OK: parallel が含まれる場合は Map 経由、含まれなければ Recipe Plan 経由
        if compiled.parallel_groups:
            rec = await self._start_experiment_with_parallel(
                plan_id, plan_dict, compiled,
                owner=owner,
                override_safety=override_safety, override_reason=override_reason,
                job_timeout_s=job_timeout_s, queue_policy=queue_policy,
            )
        else:
            rec = await self._start_experiment_recipe_path(
                plan_id, plan_dict, compiled,
                owner=owner,
                override_safety=override_safety, override_reason=override_reason,
                job_timeout_s=job_timeout_s, queue_policy=queue_policy,
            )
        # template_source は _dsl_params 経由で create_job parameters に
        # 既に注入済み (compiled.summary["template_source"] から伝播)
        # v0.9.3: audit
        if self._audit is not None:
            try:
                self._audit.record_event(
                    "job_started" if rec.status.value != "failed"
                    else "job_failed",
                    severity="info" if rec.status.value != "failed" else "error",
                    status=rec.status.value,
                    job_id=rec.job_id,
                    owner=owner,
                    tool_name="start_experiment_job",
                    resource=rec.resource_name,
                    error_class=rec.error_class,
                    message=rec.last_step_summary,
                    request={"plan_name": (plan_dict or {}).get("name"),
                              "queue_policy": queue_policy,
                              "override_safety": override_safety},
                    metadata={"template_source": template_source},
                )
            except Exception:
                pass
        return rec

    async def _start_experiment_recipe_path(
        self,
        plan_id: str,
        plan_dict: dict[str, Any],
        compiled,
        *,
        owner: str,
        override_safety: bool,
        override_reason: str,
        job_timeout_s: float | None,
        queue_policy: QueuePolicy,
    ) -> JobRecord:
        """parallel を含まない DSL plan を「単一 Plan を持つ Job」として実行"""
        plan_name = plan_dict.get("name") or "experiment"
        primary_resource = (
            compiled.main_plan.required_resources[0]
            if compiled.main_plan.required_resources else ""
        )

        job_id = self._new_job_id()
        rec = self._store.create_job(
            job_id=job_id, owner=owner,
            resource_name=primary_resource,
            recipe=f"<dsl:{plan_name}>",
            parameters=_dsl_params(plan_id, plan_dict, compiled),
        )

        # plan 永続化
        try:
            self._store.save_experiment_plan(
                plan_id=plan_id, job_id=job_id,
                name=plan_name,
                dsl_version=plan_dict.get("dsl_version", "0.8"),
                original_plan=plan_dict,
                compiled_summary=compiled.summary,
                validation_result={
                    "valid": True, "errors": [], "warnings": compiled.warnings,
                },
            )
        except Exception as e:
            logger.warning("experiment_plans 永続化失敗: %s", e)

        # scheduler 投入
        required = list(compiled.main_plan.required_resources)
        try:
            immediate, blocking = await self._scheduler.enqueue(
                job_id, required, queue_policy=queue_policy,
            )
        except ResourceBusyError as e:
            return self._store.transition_status(
                job_id, JobStatus.FAILED,
                error_class="blocked",
                last_step_summary=f"resource busy (blocked by {e.blocking_job_id})",
                result={"success": False, "error": "ResourceBusy",
                        "message": str(e), "blocking_job_id": e.blocking_job_id,
                        "plan_id": plan_id},
            )

        effective_timeout = (
            DEFAULT_JOB_TIMEOUT_S if job_timeout_s is None else float(job_timeout_s)
        )
        deadline = time.monotonic() + effective_timeout if effective_timeout > 0 else None

        task = asyncio.create_task(
            self._run_experiment_plan_job(
                rec, compiled,
                required_resources=required,
                override_safety=override_safety, override_reason=override_reason,
                start_immediately=immediate,
                plan_id=plan_id,
            ),
            name=f"job-{job_id}",
        )
        self._runtimes[job_id] = _JobRuntime(task, deadline)
        if not immediate:
            self._store.update_step(
                job_id, -1, last_step_summary=f"queued, blocked_by={blocking}",
            )
        return rec

    async def _run_experiment_plan_job(
        self,
        rec: JobRecord,
        compiled,
        *,
        required_resources: list[str],
        override_safety: bool,
        override_reason: str,
        start_immediately: bool,
        plan_id: str,
    ) -> None:
        """DSL plan の単一 Plan 実行 (recipe job と同じ経路をたどる)"""
        job_id = rec.job_id
        try:
            if not start_immediately:
                await self._wait_until_scheduled(job_id)
            await self._scheduler.on_running(job_id)

            runtime = self._runtimes.get(job_id)
            if runtime is None:
                return
            current = self._store.get(job_id)
            if current is not None and is_terminal(current.status):
                return

            self._store.transition_status(job_id, JobStatus.RUNNING, current_step_index=0)
            self._safe_record_event(
                job_id, "experiment_job_started",
                payload={"plan_id": plan_id, "dsl": True},
            )

            # 各 step を _run_job_inner と同様に直列実行
            # ただし instrument は step.instrument (compile で resource_name に解決済み) を使う
            step_results: list[dict] = []
            plan = compiled.main_plan
            for idx, step in enumerate(plan.steps):
                if runtime.is_timed_out():
                    self._record_timeout(rec, idx, step_results)
                    return
                if runtime.cancel_mode is not None:
                    await self._handle_cancel(
                        rec, self._sessions.get_session(rec.resource_name),
                        runtime.cancel_mode, step_results,
                    )
                    return

                self._store.update_step(
                    job_id, idx,
                    last_step_summary=self._step_summary(step),
                )

                # step 種別ごとに dispatch
                if isinstance(step, WaitStep):
                    self._safe_transition(job_id, JobStatus.WAITING)
                    result = await self._run_wait_with_cancel_check(step, runtime)
                    self._safe_transition(job_id, JobStatus.RUNNING)
                elif isinstance(step, WaitUntilStep):
                    self._safe_transition(job_id, JobStatus.WAITING)
                    result = await execute_wait_until(
                        step,
                        cancel_check=lambda: self._poll_cancel_reason(runtime),
                        on_progress=lambda p: self._update_progress(runtime, p),
                    )
                    runtime.current_progress = None
                    self._safe_transition(job_id, JobStatus.RUNNING)
                elif isinstance(step, WaitForConditionStep):
                    self._safe_transition(job_id, JobStatus.WAITING)
                    result = await execute_wait_for_condition(
                        self._visa, self._sessions.get_session, step,
                        cancel_check=lambda: self._poll_cancel_reason(runtime),
                        on_progress=lambda p: self._update_progress(runtime, p),
                    )
                    runtime.current_progress = None
                    self._safe_transition(job_id, JobStatus.RUNNING)
                elif isinstance(step, WaitForStableStep):
                    self._safe_transition(job_id, JobStatus.WAITING)
                    result = await execute_wait_for_stable(
                        self._visa, self._sessions.get_session, step,
                        cancel_check=lambda: self._poll_cancel_reason(runtime),
                        on_progress=lambda p: self._update_progress(runtime, p),
                    )
                    runtime.current_progress = None
                    self._safe_transition(job_id, JobStatus.RUNNING)
                elif isinstance(step, CommandStep):
                    # DSL では step.instrument が必ず resource_name に解決済み
                    target_resource = step.instrument or rec.resource_name
                    target_session = self._sessions.get_session(target_resource)
                    if target_session is None:
                        result = {
                            "success": False, "error": "SessionNotFound",
                            "message": f"resource {target_resource} の session がありません",
                        }
                    else:
                        result = await execute_command_step(
                            self._visa, target_session, step,
                            override_safety=override_safety,
                            override_reason=override_reason,
                        )
                else:
                    result = {
                        "success": False, "error": "UnsupportedStepType",
                        "step_type": getattr(step, "type", "?"),
                    }
                step_results.append({"step": idx, **result})

                if not result.get("success", False):
                    if result.get("interrupted_by_timeout"):
                        self._record_timeout(rec, idx, step_results)
                        return
                    if result.get("interrupted_by_cancel"):
                        await self._handle_cancel(
                            rec, self._sessions.get_session(rec.resource_name),
                            runtime.cancel_mode or CancelMode.AFTER_CURRENT_STEP,
                            step_results,
                        )
                        return
                    err_class = result.get("error", "internal")
                    self._store.transition_status(
                        job_id, JobStatus.FAILED,
                        current_step_index=idx,
                        error_class=err_class,
                        last_step_summary=(
                            f"step {idx} failed: "
                            f"{str(result.get('message', result.get('error', '?')))[:80]}"
                        ),
                        result=self._with_persistence_warnings(
                            job_id,
                            {
                                "success": False, "recipe": rec.recipe,
                                "steps_executed": step_results,
                                "halted_at_step": idx,
                                "plan_id": plan_id,
                            },
                        ),
                    )
                    return

            # safe_shutdown step が含まれていた → best_effort 実行
            # v0.8.1.1 修正: targets 省略時も used_resources 全件に個別 shutdown を
            # 実行する (旧 v0.8.x では required_resources[0] の 1 台のみだった)。
            # これにより summary.safe_shutdown_targets と実行時の動作が完全一致する。
            shutdown_info = None
            if compiled.has_safe_shutdown:
                if compiled.safe_shutdown_targets:
                    # 明示 targets
                    targets_to_shutdown = list(compiled.safe_shutdown_targets)
                    source = "explicit_targets"
                else:
                    # 省略 → used_resources (= main_plan.required_resources) 全件
                    # v0.8.1.1: compiled.used_resources を優先、無ければ required_resources
                    targets_to_shutdown = list(
                        compiled.used_resources or required_resources
                    )
                    source = "all_used_resources"

                per_resource: list[dict] = []
                overall_success = True
                attempted_any = False
                for tgt in targets_to_shutdown:
                    s = self._sessions.get_session(tgt)
                    info = await self._best_effort_safe_shutdown(s)
                    per_resource.append({
                        "resource": tgt,
                        "shutdown": info,
                    })
                    if info.get("attempted"):
                        attempted_any = True
                        if not info.get("success"):
                            overall_success = False
                shutdown_info = {
                    "attempted": attempted_any,
                    "source": source,
                    "success": overall_success,
                    "targets": targets_to_shutdown,
                    "per_resource": per_resource,
                }
                step_results.append({
                    "step": -1, "step_type": "safe_shutdown",
                    "shutdown": shutdown_info,
                    "success": bool(
                        shutdown_info.get("success") or not shutdown_info.get("attempted")
                    ),
                })

            self._store.transition_status(
                job_id, JobStatus.COMPLETED,
                current_step_index=len(plan.steps) - 1,
                last_step_summary="experiment completed",
                result=self._with_persistence_warnings(
                    job_id,
                    {
                        "success": True, "recipe": rec.recipe,
                        "plan_id": plan_id,
                        "steps_executed": step_results,
                        "step_count": len(step_results),
                        "safe_shutdown": shutdown_info,
                    },
                ),
            )

        except asyncio.CancelledError:
            self._safe_transition(job_id, JobStatus.CANCELLING)
            try:
                self._store.transition_status(
                    job_id, JobStatus.CANCELLED,
                    error_class="cancelled",
                    last_step_summary="cancelled (immediate)",
                    result={"success": False, "recipe": rec.recipe,
                            "cancelled": True, "cancel_mode": "immediate",
                            "plan_id": plan_id},
                )
            except Exception:
                pass
            raise
        except Exception as e:
            logger.exception("experiment job %s で予期しないエラー", job_id)
            self._store.transition_status(
                job_id, JobStatus.FAILED,
                error_class="internal",
                last_step_summary=f"unexpected: {e}",
                result={"success": False, "error": "InternalError", "message": str(e),
                        "plan_id": plan_id},
            )
        finally:
            try:
                next_jobs = await self._scheduler.on_terminal(job_id, required_resources)
                for nj_id in next_jobs:
                    self._wake_queued_job(nj_id)
            except Exception:
                pass
            self._runtimes.pop(job_id, None)

    async def _start_experiment_with_parallel(
        self,
        plan_id: str,
        plan_dict: dict[str, Any],
        compiled,
        *,
        owner: str,
        override_safety: bool,
        override_reason: str,
        job_timeout_s: float | None,
        queue_policy: QueuePolicy,
    ) -> JobRecord:
        """parallel を含む DSL plan を GroupExecutor 経路で実行。

        MVP: parallel が plan の終端付近で 1 度発生する想定。
        parallel の各 branch を TargetExecution として GroupExecutor に転送。
        main_plan の (parallel 以外の) 前段 step は parallel 前に直列実行。

        v0.8.0 MVP では「main_plan の前段 + 単一 parallel グループ」を実行する
        単純形に絞る。複雑な交互順序や末尾の追加 step は v0.8.1+ で対応。
        """
        # MVP 実装: parallel の branches だけで Map ジョブを起動。
        # main_plan の非 parallel 部分は (現状無いので) スキップ。
        # parallel が 1 グループのみの想定。複数あれば最初のみ扱う。
        pg = compiled.parallel_groups[0]
        if len(compiled.parallel_groups) > 1:
            logger.warning(
                "v0.8.0 MVP: parallel グループが %d 個ありますが最初のみ実行します",
                len(compiled.parallel_groups),
            )

        # 各 branch Plan を TargetExecution に
        targets: list[TargetExecution] = []
        all_resources: set[str] = set()
        for i, branch_plan in enumerate(pg["branch_plans"]):
            # branch Plan から required_resources を再抽出
            br_res: set[str] = set()
            for s in branch_plan.steps:
                if hasattr(s, "instrument") and getattr(s, "instrument", None):
                    br_res.add(s.instrument)
            br_resources = sorted(br_res)
            all_resources.update(br_res)
            targets.append(TargetExecution(
                target_id=f"branch_{i}",
                plan=branch_plan,
                required_resources=br_resources,
                bindings={},
            ))

        # 主 resource は main_plan / parallel 全体の resource
        all_resources.update(compiled.main_plan.required_resources)

        plan_name = plan_dict.get("name") or "experiment"
        job_id = self._new_job_id()
        rec = self._store.create_job(
            job_id=job_id, owner=owner,
            resource_name=sorted(all_resources)[0] if all_resources else "",
            recipe=f"<dsl_parallel:{plan_name}>",
            parameters=_merge_parallel_params(plan_id, plan_dict, compiled, len(targets)),
        )
        try:
            self._store.save_experiment_plan(
                plan_id=plan_id, job_id=job_id,
                name=plan_name,
                dsl_version=plan_dict.get("dsl_version", "0.8"),
                original_plan=plan_dict,
                compiled_summary=compiled.summary,
                validation_result={"valid": True, "errors": [], "warnings": compiled.warnings},
            )
        except Exception:
            pass

        return await self._start_group_or_map_job(
            kind="dsl_parallel",
            recipe_label=f"<dsl_parallel:{plan_name}>",
            owner=owner,
            parameters=_merge_parallel_params(plan_id, plan_dict, compiled, len(targets)),
            targets=targets,
            required_resources=sorted(all_resources),
            concurrency=pg["concurrency"],
            failure_policy={"mode": "continue", "retry": 0},
            job_timeout_s=job_timeout_s,
            queue_policy=queue_policy,
        )

    # =====================================================================
    # v0.7.0: Monitor ジョブ
    # =====================================================================

    # monitor の制限 (実装方針 #14)
    _MONITOR_MIN_INTERVAL_S: float = 1.0
    _MONITOR_MAX_DURATION_S: float = 86400.0  # 24h
    _MONITOR_MAX_SAMPLES: int = 100_000

    async def start_monitor_job(
        self,
        instrument: str,
        command_name: str,
        *,
        interval_s: float = 5.0,
        duration_s: float = 600.0,
        stop_condition_expr: str | None = None,
        value_path: str | None = None,
        args: dict[str, Any] | None = None,
        owner: str = "",
        queue_policy: QueuePolicy = "queue",
    ) -> JobRecord:
        """v0.7.0: 定期測定する Monitor Job を起動。

        各 poll の値は SQLite `monitor_data` に保存され、`get_monitor_data` で
        取得可能。`stop_condition_expr` が指定された場合は条件成立で早期終了。

        interval_s >= 1.0 / duration_s <= 86400 / 最大 100k サンプル
        """
        args = args or {}
        # validation
        if interval_s < self._MONITOR_MIN_INTERVAL_S:
            return self._record_immediate_failure(
                resource_name=instrument, recipe_name=f"<monitor:{command_name}>",
                parameters={"interval_s": interval_s},
                error_class="validation",
                summary=(
                    f"interval_s={interval_s} は最小 {self._MONITOR_MIN_INTERVAL_S}s 以上必要"
                ),
            )
        if duration_s <= 0 or duration_s > self._MONITOR_MAX_DURATION_S:
            return self._record_immediate_failure(
                resource_name=instrument, recipe_name=f"<monitor:{command_name}>",
                parameters={"duration_s": duration_s},
                error_class="validation",
                summary=(
                    f"duration_s は 0 < x <= {self._MONITOR_MAX_DURATION_S} (24h) "
                    f"の範囲: {duration_s}"
                ),
            )

        # alias を resource に解決
        resource = self._system_config.resolve_alias(instrument) or instrument
        session = self._sessions.get_session(resource)
        if session is None or session.definition is None:
            return self._record_immediate_failure(
                resource_name=resource, recipe_name=f"<monitor:{command_name}>",
                parameters={"instrument": instrument},
                error_class="not_found",
                summary=f"{instrument} (→ {resource}) は未識別です",
            )
        cmd_def = session.definition.commands.get(command_name)
        if cmd_def is None or cmd_def.type != "query":
            return self._record_immediate_failure(
                resource_name=resource, recipe_name=f"<monitor:{command_name}>",
                parameters={"command": command_name},
                error_class="validation",
                summary=(
                    f"command '{command_name}' は query 型である必要があります"
                ),
            )

        # Job 登録
        job_id = self._new_job_id()
        rec = self._store.create_job(
            job_id=job_id, owner=owner,
            resource_name=resource,
            recipe=f"<monitor:{instrument}.{command_name}>",
            parameters={
                "instrument": instrument,
                "command": command_name,
                "interval_s": interval_s,
                "duration_s": duration_s,
                "stop_condition": stop_condition_expr,
                "value_path": value_path,
                "args": args,
            },
        )

        # scheduler 投入 (monitor 中は resource 占有)
        required_resources = [resource]
        try:
            immediate, blocking = await self._scheduler.enqueue(
                job_id, required_resources, queue_policy=queue_policy,
            )
        except ResourceBusyError as e:
            return self._store.transition_status(
                job_id, JobStatus.FAILED,
                error_class="blocked",
                last_step_summary=f"resource busy (blocked by {e.blocking_job_id})",
                result={
                    "success": False, "error": "ResourceBusy",
                    "message": str(e), "blocking_job_id": e.blocking_job_id,
                },
            )

        deadline = time.monotonic() + duration_s

        task = asyncio.create_task(
            self._run_monitor(
                rec, session, command_name, args,
                interval_s, duration_s, stop_condition_expr, value_path,
                required_resources=required_resources,
                start_immediately=immediate,
            ),
            name=f"job-{job_id}",
        )
        self._runtimes[job_id] = _JobRuntime(task, deadline)
        if not immediate:
            self._store.update_step(
                job_id, -1,
                last_step_summary=f"queued, blocked_by={blocking}",
            )
        return rec

    async def _run_monitor(
        self,
        rec: JobRecord,
        session,
        command_name: str,
        args: dict[str, Any],
        interval_s: float,
        duration_s: float,
        stop_condition_expr: str | None,
        value_path: str | None,
        *,
        required_resources: list[str],
        start_immediately: bool,
    ) -> None:
        """Monitor Job の bg 実行"""
        job_id = rec.job_id
        try:
            if not start_immediately:
                await self._wait_until_scheduled(job_id)
            await self._scheduler.on_running(job_id)

            runtime = self._runtimes.get(job_id)
            if runtime is None:
                return
            current = self._store.get(job_id)
            if current is not None and is_terminal(current.status):
                return

            self._store.transition_status(job_id, JobStatus.RUNNING, current_step_index=0)
            self._store.record_event(
                job_id, "job_started",
                payload={"type": "monitor", "instrument": session.resource_name,
                         "command": command_name, "interval_s": interval_s,
                         "duration_s": duration_s},
            )

            from lab_executor.utils.condition import safe_eval_condition, ConditionError

            t0 = time.monotonic()
            deadline = t0 + duration_s
            samples = 0
            consecutive_errors = 0
            max_consecutive_errors = 3
            stopped_by_condition = False
            last_value: Any = None

            while True:
                # cancel / timeout チェック
                if runtime.cancel_mode is not None:
                    break
                now = time.monotonic()
                if now >= deadline:
                    break
                # 1 poll
                value, raw, _parsed, error_kind = await _do_one_poll(
                    self._visa, session, command_name, args, None, value_path,
                )
                if error_kind is not None:
                    consecutive_errors += 1
                    if consecutive_errors >= max_consecutive_errors:
                        self._store.transition_status(
                            job_id, JobStatus.FAILED,
                            error_class="hardware",
                            last_step_summary=(
                                f"monitor: {consecutive_errors} 連続失敗 ({error_kind})"
                            ),
                            result={
                                "success": False, "error": "MonitorPollErrorExceeded",
                                "samples_recorded": samples,
                                "last_error_kind": error_kind,
                            },
                        )
                        return
                else:
                    consecutive_errors = 0
                    last_value = value
                    samples += 1
                    try:
                        self._store.append_monitor_data(
                            monitor_id=job_id,
                            instrument=session.resource_name,
                            value=value,
                            sample_count=samples,
                        )
                    except Exception as e:
                        logger.warning("monitor_data append 失敗: %s", e)
                    # 上限到達
                    if samples >= self._MONITOR_MAX_SAMPLES:
                        break
                    # stop_condition 評価
                    if stop_condition_expr:
                        try:
                            if safe_eval_condition(
                                stop_condition_expr, {"value": value},
                            ):
                                stopped_by_condition = True
                                self._store.record_event(
                                    job_id, "monitor_stop_condition_met",
                                    payload={
                                        "value": value,
                                        "condition_expr": stop_condition_expr,
                                    },
                                )
                                break
                        except ConditionError as e:
                            self._store.transition_status(
                                job_id, JobStatus.FAILED,
                                error_class="validation",
                                last_step_summary=f"stop_condition error: {e}",
                                result={"success": False, "error": "ConditionError"},
                            )
                            return

                # progress 更新 (公開用)
                self._update_progress(runtime, {
                    "type": "monitor",
                    "samples": samples,
                    "elapsed_s": time.monotonic() - t0,
                    "remaining_s": max(0.0, deadline - time.monotonic()),
                    "last_value": last_value,
                    "interval_s": interval_s,
                })

                # slice sleep で cancel 即応
                remaining_sleep = interval_s
                while remaining_sleep > 0:
                    if runtime.cancel_mode is not None or runtime.is_timed_out():
                        break
                    chunk = min(remaining_sleep, POLL_SLEEP_SLICE_S)
                    await asyncio.sleep(chunk)
                    remaining_sleep -= chunk
                if runtime.cancel_mode is not None:
                    break

            elapsed = time.monotonic() - t0
            # 終端判定
            if runtime.cancel_mode is not None:
                self._safe_transition(job_id, JobStatus.CANCELLING)
                self._store.transition_status(
                    job_id, JobStatus.CANCELLED,
                    error_class="cancelled",
                    last_step_summary=f"monitor cancelled at {samples} samples",
                    result={
                        "success": False, "cancelled": True,
                        "samples_recorded": samples, "elapsed_s": elapsed,
                    },
                )
            else:
                self._store.transition_status(
                    job_id, JobStatus.COMPLETED,
                    last_step_summary=(
                        f"monitor completed: {samples} samples"
                        + (" (stop condition met)" if stopped_by_condition else "")
                    ),
                    result={
                        "success": True,
                        "samples_recorded": samples,
                        "elapsed_s": elapsed,
                        "stopped_by_condition": stopped_by_condition,
                        "last_value": last_value,
                    },
                )

        except asyncio.CancelledError:
            self._safe_transition(job_id, JobStatus.CANCELLING)
            try:
                self._store.transition_status(
                    job_id, JobStatus.CANCELLED,
                    error_class="cancelled",
                    last_step_summary="monitor cancelled (immediate)",
                    result={"success": False, "cancelled": True},
                )
            except Exception:
                pass
            raise
        except Exception as e:
            logger.exception("monitor job %s で予期しないエラー", job_id)
            self._store.transition_status(
                job_id, JobStatus.FAILED,
                error_class="internal",
                last_step_summary=f"unexpected: {e}",
                result={"success": False, "error": "InternalError", "message": str(e)},
            )
        finally:
            try:
                next_jobs = await self._scheduler.on_terminal(job_id, required_resources)
                for nj_id in next_jobs:
                    self._wake_queued_job(nj_id)
            except Exception:
                pass
            self._runtimes.pop(job_id, None)

    # =====================================================================
    # v0.6.0: Group / Map ジョブ
    # =====================================================================

    async def start_group_query_job(
        self,
        group_name: str,
        command_name: str,
        args: dict[str, Any] | None = None,
        *,
        concurrency: int = 10,
        failure_policy: dict[str, Any] | None = None,
        owner: str = "",
        job_timeout_s: float | None = None,
        queue_policy: QueuePolicy = "queue",
    ) -> JobRecord:
        """instrument_groups[group_name] の全機器に対し同じ query を投げる Job

        各 instrument には個別 TargetExecution を作成。target_id = alias 名。
        """
        args = args or {}
        # validation
        group = self._system_config.get_group(group_name)
        if group is None:
            return self._record_immediate_failure(
                resource_name="", recipe_name=f"<group:{group_name}>",
                parameters={"group": group_name, "command": command_name, "args": args},
                error_class="not_found",
                summary=f"instrument_group '{group_name}' は定義されていません",
            )
        if not group.members:
            return self._record_immediate_failure(
                resource_name="", recipe_name=f"<group:{group_name}>",
                parameters={"group": group_name},
                error_class="validation",
                summary=f"instrument_group '{group_name}' に members がいません",
            )

        # build TargetExecution per member
        # v0.6.0.1: 全 member の command が "query" 型であることを事前検証。
        # write 系コマンドを start_group_query_job で実行するのは名前と挙動がずれるので拒否。
        # write を group で扱いたいユーザは start_map_recipe_job (同 parameters) を使う。
        targets: list[TargetExecution] = []
        required_resources_set: set[str] = set()
        try:
            for alias in group.members:
                resource = self._system_config.resolve_alias(alias) or alias
                session = self._sessions.get_session(resource)
                if session is None or session.definition is None:
                    return self._record_immediate_failure(
                        resource_name="", recipe_name=f"<group:{group_name}>",
                        parameters={"group": group_name},
                        error_class="not_found",
                        summary=(
                            f"group '{group_name}' member '{alias}' (→ {resource}) は "
                            f"未識別です。identify_instrument / bind_definition を先に実行してください"
                        ),
                    )
                cmd_def = session.definition.commands.get(command_name)
                if cmd_def is None:
                    return self._record_immediate_failure(
                        resource_name="", recipe_name=f"<group:{group_name}>",
                        parameters={"group": group_name},
                        error_class="not_found",
                        summary=f"member '{alias}' に command '{command_name}' が未定義",
                    )
                if cmd_def.type != "query":
                    return self._record_immediate_failure(
                        resource_name="", recipe_name=f"<group:{group_name}>",
                        parameters={"group": group_name, "command": command_name},
                        error_class="validation",
                        summary=(
                            f"start_group_query_job は query 系 command のみ許可します "
                            f"(member '{alias}'.{command_name} type='{cmd_def.type}'). "
                            f"write を group 実行したい場合は start_map_recipe_job を使用してください"
                        ),
                    )
                plan = Plan(
                    name=f"group_query:{command_name}",
                    steps=[CommandStep(command=command_name, args=args)],
                    required_resources=[resource],
                )
                targets.append(TargetExecution(
                    target_id=alias,
                    plan=plan,
                    required_resources=[resource],
                    bindings={},
                    parameters=args,
                ))
                required_resources_set.add(resource)
        except Exception as e:
            return self._record_immediate_failure(
                resource_name="", recipe_name=f"<group:{group_name}>",
                parameters={"group": group_name},
                error_class="validation",
                summary=f"target build 失敗: {e}",
            )

        return await self._start_group_or_map_job(
            kind="group_query",
            recipe_label=f"<group_query:{group_name}.{command_name}>",
            owner=owner,
            parameters={
                "group": group_name, "command": command_name,
                "args": args, "concurrency": concurrency,
            },
            targets=targets,
            required_resources=sorted(required_resources_set),
            concurrency=concurrency,
            failure_policy=failure_policy or {},
            job_timeout_s=job_timeout_s,
            queue_policy=queue_policy,
        )

    async def start_map_recipe_job(
        self,
        recipe_name: str,
        targets_spec: list[dict[str, Any]],
        *,
        concurrency: int = 10,
        failure_policy: dict[str, Any] | None = None,
        owner: str = "",
        job_timeout_s: float | None = None,
        queue_policy: QueuePolicy = "queue",
        primary_role: str | None = None,
    ) -> JobRecord:
        """recipe を異なる条件で各 target に並列適用

        targets_spec: [
          {
            "target_id": "sample001",
            "unit": "unit001",                     # 任意
            "bindings": {"psu": "psu001", ...},    # 任意 (unit と merge)
            "parameters": {"voltage": 1.0},
          },
          ...
        ]

        primary_role: recipe を解釈する際の主 instrument の役割名。
        例 primary_role="psu" の場合、各 target の bindings["psu"] の YAML 定義から
        recipe を取得する。指定なし時は全 target が同一 recipe 構造を持つことを期待し、
        最初の target から推定。
        """
        if not targets_spec:
            return self._record_immediate_failure(
                resource_name="", recipe_name=f"<map:{recipe_name}>",
                parameters={"recipe": recipe_name},
                error_class="validation",
                summary="targets が空です",
            )

        # build target executions
        targets: list[TargetExecution] = []
        all_resources: set[str] = set()
        seen_ids: set[str] = set()

        for spec in targets_spec:
            target_id = str(spec.get("target_id") or "")
            if not target_id:
                return self._record_immediate_failure(
                    resource_name="", recipe_name=f"<map:{recipe_name}>",
                    parameters={"recipe": recipe_name},
                    error_class="validation",
                    summary="各 target に target_id が必須です",
                )
            if target_id in seen_ids:
                return self._record_immediate_failure(
                    resource_name="", recipe_name=f"<map:{recipe_name}>",
                    parameters={"recipe": recipe_name},
                    error_class="validation",
                    summary=f"target_id 重複: '{target_id}'",
                )
            seen_ids.add(target_id)
            try:
                bindings = resolve_unit_bindings(
                    spec.get("unit"), spec.get("bindings") or {},
                    self._system_config,
                )
                if not bindings:
                    return self._record_immediate_failure(
                        resource_name="", recipe_name=f"<map:{recipe_name}>",
                        parameters={"recipe": recipe_name},
                        error_class="validation",
                        summary=f"target {target_id}: bindings 空 (unit / bindings 未指定)",
                    )
                # primary alias の決定
                # v0.6.0.1: bindings が複数ある場合は primary_role 必須化 (推定の罠を防ぐ)
                p_role = primary_role
                if p_role is None or p_role not in bindings:
                    if len(bindings) > 1:
                        return self._record_immediate_failure(
                            resource_name="", recipe_name=f"<map:{recipe_name}>",
                            parameters={"recipe": recipe_name},
                            error_class="validation",
                            summary=(
                                f"target {target_id}: bindings に複数の role がある場合は "
                                f"primary_role の指定が必須です (bindings={list(bindings)})"
                            ),
                        )
                    # 単一 binding なら唯一の role を primary に
                    p_role = next(iter(bindings.keys()))
                primary_alias = bindings[p_role]
                primary_resource = self._system_config.resolve_alias(primary_alias) or primary_alias
                primary_session = self._sessions.get_session(primary_resource)
                if primary_session is None or primary_session.definition is None:
                    return self._record_immediate_failure(
                        resource_name="", recipe_name=f"<map:{recipe_name}>",
                        parameters={"recipe": recipe_name},
                        error_class="not_found",
                        summary=(
                            f"target {target_id}: primary instrument "
                            f"{primary_alias} (→ {primary_resource}) は未識別です"
                        ),
                    )
                recipe = primary_session.definition.recipes.get(recipe_name)
                if recipe is None:
                    return self._record_immediate_failure(
                        resource_name="", recipe_name=f"<map:{recipe_name}>",
                        parameters={"recipe": recipe_name},
                        error_class="not_found",
                        summary=f"recipe '{recipe_name}' が {primary_alias} に未定義",
                    )
                # parameters merge + plan 構築
                variables = dict(spec.get("parameters") or {})
                for p in recipe.parameters:
                    if p.name not in variables and p.default is not None:
                        variables[p.name] = p.default
                plan = recipe_to_plan(
                    recipe, variables, primary_resource=primary_resource,
                )
                # required_resources は plan.required_resources + bindings 全部
                target_resources = collect_target_resources(bindings, self._system_config)
                # plan.required_resources とマージ
                all_for_target = sorted(set(plan.required_resources) | set(target_resources))
                targets.append(TargetExecution(
                    target_id=target_id,
                    plan=plan,
                    required_resources=all_for_target,
                    bindings=bindings,
                    parameters=variables,
                ))
                all_resources.update(all_for_target)
            except (ResolveError, Exception) as e:
                return self._record_immediate_failure(
                    resource_name="", recipe_name=f"<map:{recipe_name}>",
                    parameters={"recipe": recipe_name},
                    error_class="validation",
                    summary=f"target {target_id} build 失敗: {e}",
                )

        return await self._start_group_or_map_job(
            kind="map_recipe",
            recipe_label=f"<map_recipe:{recipe_name}>",
            owner=owner,
            parameters={"recipe": recipe_name, "target_count": len(targets), "concurrency": concurrency},
            targets=targets,
            required_resources=sorted(all_resources),
            concurrency=concurrency,
            failure_policy=failure_policy or {},
            job_timeout_s=job_timeout_s,
            queue_policy=queue_policy,
        )

    async def _start_group_or_map_job(
        self,
        *,
        kind: str,
        recipe_label: str,
        owner: str,
        parameters: dict[str, Any],
        targets: list[TargetExecution],
        required_resources: list[str],
        concurrency: int,
        failure_policy: dict[str, Any],
        job_timeout_s: float | None,
        queue_policy: QueuePolicy,
    ) -> JobRecord:
        """group_query / map_recipe ジョブ共通の登録 + scheduler 投入"""
        job_id = self._new_job_id()
        rec = self._store.create_job(
            job_id=job_id, owner=owner,
            resource_name=(required_resources[0] if required_resources else ""),
            recipe=recipe_label,
            parameters=parameters,
        )

        # scheduler enqueue
        try:
            immediate, blocking = await self._scheduler.enqueue(
                job_id, required_resources, queue_policy=queue_policy,
            )
        except ResourceBusyError as e:
            return self._store.transition_status(
                job_id, JobStatus.FAILED,
                error_class="blocked",
                last_step_summary=f"resource busy (blocked by {e.blocking_job_id})",
                result={
                    "success": False, "error": "ResourceBusy",
                    "message": str(e),
                    "blocking_job_id": e.blocking_job_id,
                    "queue_policy": queue_policy,
                },
            )

        effective_timeout = (
            DEFAULT_JOB_TIMEOUT_S if job_timeout_s is None else float(job_timeout_s)
        )
        deadline = time.monotonic() + effective_timeout if effective_timeout > 0 else None

        task = asyncio.create_task(
            self._run_group_or_map(
                rec, targets, required_resources,
                concurrency=concurrency,
                failure_policy_dict=failure_policy,
                start_immediately=immediate,
            ),
            name=f"job-{job_id}",
        )
        self._runtimes[job_id] = _JobRuntime(task, deadline)
        if not immediate:
            self._store.update_step(
                job_id, -1,
                last_step_summary=f"queued, blocked_by={blocking}",
            )
        return rec

    async def _run_group_or_map(
        self,
        rec: JobRecord,
        targets: list[TargetExecution],
        required_resources: list[str],
        *,
        concurrency: int,
        failure_policy_dict: dict[str, Any],
        start_immediately: bool,
    ) -> None:
        """Group/Map ジョブの bg 実行本体"""
        job_id = rec.job_id
        try:
            if not start_immediately:
                await self._wait_until_scheduled(job_id)
            await self._scheduler.on_running(job_id)

            runtime = self._runtimes.get(job_id)
            if runtime is None:
                return
            current = self._store.get(job_id)
            if current is not None and is_terminal(current.status):
                return

            self._store.transition_status(job_id, JobStatus.RUNNING, current_step_index=0)
            self._store.update_step(
                job_id, 0,
                last_step_summary=f"{rec.recipe} targets={len(targets)} concurrency={concurrency}",
            )

            executor = GroupExecutor(
                self._visa,
                session_resolver=self._session_for_alias_or_resource,
            )

            try:
                policy = FailurePolicy.from_dict(failure_policy_dict)
            except Exception as e:
                self._store.transition_status(
                    job_id, JobStatus.FAILED,
                    error_class="validation",
                    last_step_summary=f"failure_policy invalid: {e}",
                    result={"success": False, "error": "ValidationError", "message": str(e)},
                )
                return

            # v0.7.0: target_runs 初期化
            for t in targets:
                try:
                    self._store.upsert_target_run(
                        job_id, t.target_id, "queued",
                        required_resources=t.required_resources,
                        bindings=dict(t.bindings),
                        parameters=dict(t.parameters),
                        is_start=False,
                    )
                except Exception:
                    pass

            self._safe_record_event(
                job_id, "job_started",
                payload={
                    "type": "group_or_map",
                    "recipe": rec.recipe,
                    "target_count": len(targets),
                    "concurrency": concurrency,
                },
            )

            def _on_event(event_type: str, payload: dict) -> None:
                tid = payload.get("target_id")
                # target_runs 反映
                try:
                    if event_type == "target_started":
                        self._store.upsert_target_run(
                            job_id, tid, "running", is_start=True,
                        )
                    elif event_type == "target_completed":
                        self._store.upsert_target_run(
                            job_id, tid, "ok",
                            result={
                                "attempts": payload.get("attempts"),
                                "elapsed_s": payload.get("elapsed_s"),
                            },
                        )
                    elif event_type == "target_failed":
                        self._store.upsert_target_run(
                            job_id, tid,
                            payload.get("status", "failed"),
                            error={
                                "error_class": payload.get("error_class"),
                                "attempts": payload.get("attempts"),
                                "elapsed_s": payload.get("elapsed_s"),
                            },
                        )
                except Exception:
                    pass
                self._safe_record_event(
                    job_id, event_type, target_id=tid, payload=payload,
                )

            try:
                summary_dict = await executor.run(
                    targets,
                    concurrency=concurrency,
                    failure_policy=policy,
                    cancel_check=lambda: self._poll_cancel_reason(runtime),
                    on_progress=lambda p: self._update_progress(runtime, p),
                    on_event=_on_event,
                )
            finally:
                runtime.current_progress = None

            # status 判定 → SQLite 反映
            status = summary_dict["status"]
            if status == "ok":
                final_status = JobStatus.COMPLETED
            elif status == "partial_failure":
                final_status = JobStatus.COMPLETED  # partial_failure も成功扱い
            else:
                final_status = JobStatus.FAILED

            err_class = None
            if status == "partial_failure":
                err_class = "partial_failure"
            elif status == "error":
                err_class = "internal"

            self._store.transition_status(
                job_id, final_status,
                error_class=err_class,
                last_step_summary=(
                    f"{rec.recipe} total={summary_dict['summary']['total']} "
                    f"success={summary_dict['summary']['success']} "
                    f"failed={summary_dict['summary']['failed']}"
                ),
                result={
                    "success": status in ("ok", "partial_failure"),
                    "recipe": rec.recipe,
                    "group_or_map_status": status,
                    "summary": summary_dict["summary"],
                    "results": summary_dict["results"],
                    "errors": summary_dict["errors"],
                },
            )

        except asyncio.CancelledError:
            self._safe_transition(job_id, JobStatus.CANCELLING)
            try:
                self._store.transition_status(
                    job_id, JobStatus.CANCELLED,
                    error_class="cancelled",
                    last_step_summary="cancelled (immediate)",
                    result={
                        "success": False, "recipe": rec.recipe,
                        "cancelled": True, "cancel_mode": "immediate",
                    },
                )
            except Exception:
                pass
            raise
        except Exception as e:
            logger.exception("group/map job %s で予期しないエラー", job_id)
            self._store.transition_status(
                job_id, JobStatus.FAILED,
                error_class="internal",
                last_step_summary=f"unexpected: {e}",
                result={"success": False, "error": "InternalError", "message": str(e)},
            )
        finally:
            try:
                next_jobs = await self._scheduler.on_terminal(job_id, required_resources)
                for nj_id in next_jobs:
                    self._wake_queued_job(nj_id)
            except Exception:
                pass
            self._runtimes.pop(job_id, None)

    # =====================================================================
    # v0.5 API (続き)
    # =====================================================================

    def get(self, job_id: str) -> JobRecord:
        rec = self._store.get(job_id)
        if rec is None:
            raise JobNotFoundError(f"job not found: {job_id}")
        return rec

    def list_jobs(
        self,
        status_filter: list[str] | None = None,
        limit: int = 50,
        owner: str | None = None,
    ) -> list[JobRecord]:
        return self._store.list_jobs(status_filter, limit, owner)

    async def cancel(
        self,
        job_id: str,
        cancel_mode: CancelMode = CancelMode.AFTER_CURRENT_STEP,
        timeout_s: float | None = 30.0,
    ) -> JobRecord:
        """
        Job のキャンセルを要求し、終端状態に遷移するまで待機する。

        immediate           : asyncio.Task をキャンセル (asyncio.CancelledError)
        after_current_step  : 次の step 開始前にキャンセル
        safe_shutdown       : YAML safe_shutdown を実行してからキャンセル

        v0.5.0.2: queued 状態の Job は scheduler から取り除き、直接 cancelled へ遷移。
        """
        rec = self.get(job_id)
        if is_terminal(rec.status):
            return rec

        runtime = self._runtimes.get(job_id)
        if runtime is None:
            # ランタイムが消失 (再起動後等) → interrupted として返す
            return self._store.transition_status(
                job_id, JobStatus.INTERRUPTED,
                error_class="interrupted",
                last_step_summary="runtime missing",
            )

        runtime.cancel_mode = cancel_mode

        # v0.5.0.2: queued の場合は scheduler から取り除き、直接 cancelled へ遷移
        if rec.status == JobStatus.QUEUED:
            await self._scheduler.cancel_queued(job_id)
            # 待機中の _wait_until_scheduled を抜けさせる (v0.5.0.3: event は常に存在)
            runtime._start_event.set()
            try:
                self._store.transition_status(
                    job_id, JobStatus.CANCELLED,
                    error_class="cancelled",
                    last_step_summary=f"cancelled from queued ({cancel_mode.value})",
                    result={
                        "success": False, "recipe": rec.recipe,
                        "cancelled": True, "cancel_mode": cancel_mode.value,
                    },
                )
            except Exception:
                pass
            # Task の cleanup (scheduler.on_terminal + _runtimes.pop が finally で走る)
            runtime.task.cancel()
        else:
            # 通常 (running/waiting) のキャンセル経路
            # cancelling 状態に遷移
            try:
                self._store.transition_status(
                    job_id, JobStatus.CANCELLING,
                    last_step_summary=f"cancel_mode={cancel_mode.value}",
                )
            except Exception:
                # 既に終端なら無視
                pass

            if cancel_mode is CancelMode.IMMEDIATE:
                runtime.task.cancel()

        # 終端まで待機
        try:
            await asyncio.wait_for(runtime.task, timeout=timeout_s)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        except Exception:
            pass

        rec = self.get(job_id)
        # v0.9.3: audit
        if self._audit is not None:
            try:
                self._audit.record_event(
                    "job_cancelled",
                    severity="warning",
                    status=rec.status.value,
                    job_id=job_id,
                    owner=rec.owner,
                    tool_name="cancel_job",
                    resource=rec.resource_name,
                    metadata={"cancel_mode": cancel_mode.value
                              if hasattr(cancel_mode, "value")
                              else str(cancel_mode)},
                )
            except Exception:
                pass
        return rec

    # ---------- resume (v0.9.0 MVP, experimental) ----------

    async def resume_job(
        self,
        job_id: str,
        *,
        from_step: int | None = None,
        dry_run: bool = False,
        safe_shutdown_before_resume: bool = False,
        owner: str = "",
    ) -> dict[str, Any]:
        """v0.9.0 (experimental): interrupted / cancelled / failed Job を手動再開する。

        設計 (実装方針 #10 案 B): **新規 Job を作って resumed_from_job_id を紐づける**
        (元 Job の status は変えない)。

        Returns:
            dry_run=True or 検証失敗時:
                {
                  "resume_ready": bool, "original_job_id": ..., "suggested_from_step":
                  int, "requested_from_step": int|None, "steps_to_execute": [...],
                  "required_resources": [...], "warnings": [...], "errors": [...]
                }
            dry_run=False で成功:
                {
                  "resume_ready": True, "original_job_id": ...,
                  "resumed_job_id": <新 Job>, "resumed_job_status": ...,
                  "from_step": int
                }
        """
        warnings: list[dict] = []
        errors: list[dict] = []

        try:
            rec = self.get(job_id)
        except Exception:
            return {
                "resume_ready": False,
                "errors": [{
                    "error_class": "not_found",
                    "message": f"job not found: {job_id}",
                }],
            }

        # ---- 1. resume 可能 status ----
        ALLOWED = ("interrupted", "cancelled", "failed", "timeout")
        if rec.status.value not in ALLOWED:
            errors.append({
                "error_class": "resume_not_allowed",
                "message": (
                    f"job_status={rec.status.value} は resume 対象外。"
                    f"許可: {ALLOWED}"
                ),
                "recommended_next_actions": [
                    {"action": "start_new_job",
                     "reason": "completed/running Job は再実行ではなく新 Job として起動"},
                ],
            })
            return {
                "resume_ready": False, "original_job_id": job_id, "errors": errors,
            }

        # ---- 2. original_plan の取得 ----
        plan_row = self._store.get_experiment_plan_for_job(job_id)
        if plan_row is None or not plan_row.get("original_plan"):
            errors.append({
                "error_class": "resume_not_allowed",
                "message": "experiment_plan が保存されていません (resume には DSL Job が必要)",
            })
            return {
                "resume_ready": False, "original_job_id": job_id, "errors": errors,
            }
        original_plan = plan_row["original_plan"]
        if not isinstance(original_plan, dict):
            errors.append({
                "error_class": "resume_not_allowed",
                "message": "original_plan が JSON object ではありません",
            })
            return {
                "resume_ready": False, "original_job_id": job_id, "errors": errors,
            }

        dsl_version = original_plan.get("dsl_version", "0.8")
        if dsl_version not in ("0.8",):
            errors.append({
                "error_class": "resume_not_allowed",
                "message": (
                    f"dsl_version={dsl_version} は現在 resume 非互換 "
                    f"(現バージョン: 0.8)"
                ),
            })
            return {
                "resume_ready": False, "original_job_id": job_id, "errors": errors,
            }

        # safe_shutdown_failed は resume 不可 (実装方針 #9)
        if rec.error_class == "safe_shutdown_failed":
            errors.append({
                "error_class": "resume_not_allowed",
                "message": "safe_shutdown_failed 終端 Job は resume 不可",
            })
            return {
                "resume_ready": False, "original_job_id": job_id, "errors": errors,
            }

        # ---- 3. suggested_from_step (元 Job が止まった位置の次) ----
        total_steps = len(original_plan.get("steps") or [])
        last_idx = rec.current_step_index if rec.current_step_index is not None else -1
        suggested = max(0, (last_idx + 1) if last_idx >= 0 else 0)
        if suggested > total_steps:
            suggested = total_steps

        # ---- 4. from_step 範囲チェック ----
        if from_step is not None:
            if from_step < 0 or from_step > total_steps:
                errors.append({
                    "error_class": "validation",
                    "message": (
                        f"from_step={from_step} は範囲外 (0..{total_steps})"
                    ),
                    "details": {
                        "field": "from_step", "total_steps": total_steps,
                    },
                })
                return {
                    "resume_ready": False, "original_job_id": job_id,
                    "suggested_from_step": suggested, "errors": errors,
                }

        # ---- 5. dry_run mode / from_step 未指定: 分析だけ返す ----
        warnings.append({
            "warning_class": "resume_may_repeat_side_effects",
            "message": (
                "from_step より前の step は完了済みと仮定されます。"
                "実機状態によっては副作用が再発する可能性があるため、"
                "safe_shutdown_before_resume=True または手動で機器状態を確認してください"
            ),
        })

        # remaining_steps は from_step 以降を表示
        requested = from_step if from_step is not None else suggested
        remaining = (original_plan.get("steps") or [])[requested:]
        steps_to_execute_view = []
        for i, s in enumerate(remaining):
            if isinstance(s, dict):
                # v0.9.0.1: step_path を追加 (LLM が original DSL の参照位置を
                # 特定しやすいように)。MVP では top-level の単純 path のみ。
                idx = requested + i
                steps_to_execute_view.append({
                    "step_index": idx,
                    "step_path": f"steps[{idx}]",
                    "type": s.get("type"),
                    "instrument": s.get("instrument"),
                    "command": s.get("command"),
                })

        # compile して required_resources を得る (実機 I/O なし)
        try:
            sliced = dict(original_plan)
            sliced["steps"] = remaining
            from lab_executor.dsl.compiler import validate_and_compile
            compiled = validate_and_compile(
                sliced, self._sessions, self._system_config,
            )
            required = compiled.used_resources
        except Exception:
            required = []

        if dry_run or from_step is None:
            data = {
                "resume_ready": True if from_step is not None else False,
                "original_job_id": job_id,
                "suggested_from_step": suggested,
                "requested_from_step": from_step,
                "total_steps": total_steps,
                "steps_to_execute": steps_to_execute_view,
                "required_resources": required,
                "warnings": warnings,
            }
            if from_step is None:
                data["errors"] = [{
                    "error_class": "validation",
                    "message": (
                        "from_step が明示されていません。dry_run で steps_to_execute "
                        "を確認した上で from_step を指定してください"
                    ),
                    "details": {"requires_explicit_from_step": True,
                                "suggested_from_step": suggested},
                }]
            return data

        # ---- 6. safe_shutdown_before_resume ----
        # v0.9.0.1 (P1): 失敗時は resume を中止する (実装方針 #12 推奨仕様)。
        # best-effort という名前だが、resume 前 safe_shutdown は安全確保が目的
        # なので、ここで失敗を黙認すると危険な状態のまま resume が走る。
        if safe_shutdown_before_resume:
            shutdown_results = []
            any_fail = False
            for r in required:
                try:
                    session = self._sessions.get_session(r)
                    if session is not None:
                        sd = await self._best_effort_safe_shutdown(session)
                        shutdown_results.append({"resource": r, **sd})
                        if not sd.get("success", sd.get("ok", False)):
                            any_fail = True
                except Exception as e:
                    shutdown_results.append({"resource": r, "ok": False,
                                              "error": str(e)})
                    any_fail = True
            warnings.append({
                "warning_class": "safe_shutdown_applied_before_resume",
                "results": shutdown_results,
            })
            if any_fail:
                return {
                    "resume_ready": False,
                    "original_job_id": job_id,
                    "suggested_from_step": suggested,
                    "requested_from_step": from_step,
                    "warnings": warnings,
                    "errors": [{
                        "error_class": "safe_shutdown_failed",
                        "message": (
                            "safe_shutdown_before_resume が一部 resource で失敗。"
                            "resume を中止しました。機器状態を確認してから再試行してください"
                        ),
                        "details": {"shutdown_results": shutdown_results},
                        "recommended_next_actions": [
                            {"action": "check_instrument_state",
                             "tool": "get_state"},
                            {"action": "retry_without_safe_shutdown_before_resume",
                             "reason": "shutdown を明示スキップして手動で機器停止後に再試行"},
                        ],
                    }],
                }

        # ---- 7. 新規 Job を作って残り steps を実行 ----
        resume_marker = {
            "resumed_from_job_id": job_id,
            "resumed_from_step": from_step,
            "original_total_steps": total_steps,
        }
        # template_source 経由で metadata に乗せる (compiled.summary 経由)
        sliced_plan = dict(original_plan)
        sliced_plan["steps"] = remaining
        if not sliced_plan.get("name"):
            sliced_plan["name"] = f"resumed_from_{job_id}"

        new_rec = await self.start_experiment_job(
            plan_dict=sliced_plan, owner=owner or rec.owner,
            template_source={"resume": resume_marker},
        )

        # 元 Job の job_events に resume_started を記録
        try:
            self._store.record_event(
                job_id, "resume_started",
                payload={
                    "resumed_job_id": new_rec.job_id,
                    "from_step": from_step,
                },
            )
            self._store.record_event(
                new_rec.job_id, "job_resumed",
                payload={
                    "original_job_id": job_id,
                    "from_step": from_step,
                },
            )
        except Exception:
            pass

        # v0.9.3: audit
        if self._audit is not None:
            try:
                self._audit.record_event(
                    "resume_started",
                    severity="info",
                    status="ok",
                    tool_name="resume_job",
                    job_id=new_rec.job_id,
                    owner=owner or rec.owner,
                    metadata={
                        "original_job_id": job_id,
                        "from_step": from_step,
                        "safe_shutdown_before_resume":
                            safe_shutdown_before_resume,
                    },
                )
            except Exception:
                pass

        return {
            "resume_ready": True,
            "original_job_id": job_id,
            "resumed_job_id": new_rec.job_id,
            "resumed_job_status": new_rec.status.value,
            "from_step": from_step,
            "suggested_from_step": suggested,
            "required_resources": required,
            "warnings": warnings,
        }

    # ---------- internal ----------

    def _new_job_id(self) -> str:
        return f"job_{uuid.uuid4().hex[:12]}"

    def _record_immediate_failure(
        self,
        resource_name: str,
        recipe_name: str,
        parameters: dict[str, Any],
        *,
        error_class: str,
        summary: str,
    ) -> JobRecord:
        job_id = self._new_job_id()
        rec = self._store.create_job(
            job_id=job_id,
            owner="",
            resource_name=resource_name,
            recipe=recipe_name,
            parameters=parameters,
        )
        return self._store.transition_status(
            job_id, JobStatus.FAILED,
            error_class=error_class,
            last_step_summary=summary,
            result={"success": False, "error": error_class, "message": summary},
        )

    async def _run_job(
        self,
        rec: JobRecord,
        *,
        required_resources: list[str],
        override_safety: bool,
        override_reason: str,
        start_immediately: bool,
    ) -> None:
        """Job のバックグラウンド実行本体。

        - queue で待っている場合は、scheduler から起動指示が来るまで待機イベントを待つ
        - 終端遷移後、self._runtimes と scheduler から自分のエントリを必ず削除する
        """
        job_id = rec.job_id
        try:
            if not start_immediately:
                # queue 待ち。scheduler 側が次起動可能と判断したら _start_event をセットする
                await self._wait_until_scheduled(job_id)

            # 起動可能 → scheduler に running 通知
            await self._scheduler.on_running(job_id)

            await self._run_job_inner(
                rec,
                override_safety=override_safety,
                override_reason=override_reason,
            )
        finally:
            # 終端 Job の resource 解放と次 Job 起動
            try:
                next_jobs = await self._scheduler.on_terminal(job_id, required_resources)
                for nj_id in next_jobs:
                    self._wake_queued_job(nj_id)
            except Exception as e:
                logger.warning("scheduler on_terminal で例外: %s", e)
            # v0.5.0.1 fix: 終端 Job の Task 参照を解放してメモリリークを防ぐ
            self._runtimes.pop(job_id, None)

    async def _wait_until_scheduled(self, job_id: str) -> None:
        """queue 待ち中の Job を、起動可能になるまで sleep で待たせる。
        cancel が来たら CancelledError が伝播するので、それで抜ける。
        v0.5.0.3: _start_event は _JobRuntime.__init__ で eagerly 生成済み。
        """
        runtime = self._runtimes.get(job_id)
        if runtime is None:
            return
        await runtime._start_event.wait()

    def _wake_queued_job(self, job_id: str) -> None:
        """queue 先頭になった Job のイベントをセットして実行を開始させる。
        v0.5.0.3: event は常に存在するため None チェック不要。
        """
        runtime = self._runtimes.get(job_id)
        if runtime is None:
            return
        runtime._start_event.set()

    async def _run_job_inner(
        self,
        rec: JobRecord,
        *,
        override_safety: bool,
        override_reason: str,
    ) -> None:
        """Job のバックグラウンド実行本体 (内部)。終端遷移を含む全状態管理。

        v0.5.0.3: 入口で終端ガードを追加。
        immediate=True で task 起動前に cancel された場合等、
        ステータスが既に CANCELLED 等になっていた場合は何もせずに return する
        (state machine 違反による不要なログ出力を防止)。
        """
        job_id = rec.job_id
        runtime = self._runtimes[job_id]

        # v0.5.0.3: 既に終端状態 (cancelled / failed / interrupted / timeout) なら何もしない
        current = self._store.get(job_id)
        if current is not None and is_terminal(current.status):
            logger.debug(
                "_run_job_inner: job %s は既に終端 (%s) のため処理スキップ",
                job_id, current.status.value,
            )
            return

        session = self._sessions.get_session(rec.resource_name)

        # session 検証 (start_recipe_job で確認済みだが double check)
        if session is None or session.definition is None:
            self._store.transition_status(
                job_id, JobStatus.FAILED,
                error_class="not_found",
                last_step_summary="session lost",
                result={"success": False, "error": "SessionNotFound"},
            )
            return

        recipe = session.definition.recipes.get(rec.recipe)
        if recipe is None:
            self._store.transition_status(
                job_id, JobStatus.FAILED,
                error_class="not_found",
                last_step_summary="recipe not found",
                result={"success": False, "error": "RecipeNotFound"},
            )
            return

        # デフォルト適用
        variables = dict(rec.parameters)
        for p in recipe.parameters:
            if p.name not in variables and p.default is not None:
                variables[p.name] = p.default

        # Recipe → IR Plan 変換 (primary_resource を渡すことで required_resources が確定)
        try:
            plan: Plan = recipe_to_plan(
                recipe, variables, primary_resource=rec.resource_name,
            )
        except Exception as e:
            self._store.transition_status(
                job_id, JobStatus.FAILED,
                error_class="validation",
                last_step_summary=f"plan build failed: {e}",
                result={"success": False, "error": "ExpressionError", "message": str(e)},
            )
            return

        # running へ
        self._store.transition_status(job_id, JobStatus.RUNNING, current_step_index=0)
        # v0.7.0: job_events に開始記録
        self._safe_record_event(
            job_id, "job_started",
            payload={"type": "recipe", "recipe": rec.recipe,
                     "step_count": len(plan.steps)},
        )

        step_results: list[dict] = []

        try:
            for idx, step in enumerate(plan.steps):
                # 各 step 開始前の timeout / cancel チェック。
                # ループ末尾でも同様のチェックを行うが、ここでのチェックは
                # 「最初の step 前」と「直近 step 完了後の cancel が次イテレーションで
                # 検出される」用。重複に見えるが、最後の step 完了直後の cancel を
                # 救うためにループ末尾チェックも必要。
                if runtime.is_timed_out():
                    self._record_timeout(rec, idx, step_results)
                    return
                if runtime.cancel_mode is not None:
                    await self._handle_cancel(
                        rec, session, runtime.cancel_mode, step_results,
                    )
                    return

                self._store.update_step(
                    job_id, idx,
                    last_step_summary=self._step_summary(step),
                )
                # v0.7.0: step_started イベント + job_steps エントリ
                step_type = getattr(step, "type", "?")
                step_row_id = 0
                try:
                    step_row_id = self._store.record_step_started(
                        job_id, idx, step_type,
                    )
                except Exception:
                    pass
                self._safe_record_event(
                    job_id, "step_started",
                    step_index=idx,
                    payload={"step_type": step_type,
                             "summary": self._step_summary(step)},
                )

                # WaitStep は専用パス (cancel/timeout に即応)
                if isinstance(step, WaitStep):
                    self._safe_transition(job_id, JobStatus.WAITING)
                    result = await self._run_wait_with_cancel_check(step, runtime)
                    self._safe_transition(job_id, JobStatus.RUNNING)
                elif isinstance(step, WaitUntilStep):
                    self._safe_transition(job_id, JobStatus.WAITING)
                    result = await execute_wait_until(
                        step,
                        cancel_check=lambda: self._poll_cancel_reason(runtime),
                        on_progress=lambda p: self._update_progress(runtime, p),
                    )
                    runtime.current_progress = None
                    self._safe_transition(job_id, JobStatus.RUNNING)
                elif isinstance(step, WaitForConditionStep):
                    self._safe_transition(job_id, JobStatus.WAITING)
                    result = await execute_wait_for_condition(
                        self._visa, self._sessions.get_session, step,
                        cancel_check=lambda: self._poll_cancel_reason(runtime),
                        on_progress=lambda p: self._update_progress(runtime, p),
                    )
                    runtime.current_progress = None
                    self._safe_transition(job_id, JobStatus.RUNNING)
                elif isinstance(step, WaitForStableStep):
                    self._safe_transition(job_id, JobStatus.WAITING)
                    result = await execute_wait_for_stable(
                        self._visa, self._sessions.get_session, step,
                        cancel_check=lambda: self._poll_cancel_reason(runtime),
                        on_progress=lambda p: self._update_progress(runtime, p),
                    )
                    runtime.current_progress = None
                    self._safe_transition(job_id, JobStatus.RUNNING)
                elif isinstance(step, CommandStep):
                    result = await execute_command_step(
                        self._visa, session, step,
                        override_safety=override_safety,
                        override_reason=override_reason,
                    )
                else:
                    result = {
                        "success": False,
                        "error": "UnsupportedStepType",
                        "step_type": getattr(step, "type", "?"),
                    }

                step_results.append({"step": idx, **result})
                # v0.7.0: step_completed / step_failed 記録
                try:
                    self._store.record_step_completed(
                        step_row_id,
                        status="ok" if result.get("success") else "failed",
                        result=result if result.get("success") else None,
                        error=result if not result.get("success") else None,
                    )
                except Exception:
                    pass
                self._safe_record_event(
                    job_id,
                    "step_completed" if result.get("success") else "step_failed",
                    step_index=idx,
                    payload={"step_type": step_type,
                             "verified": result.get("verified"),
                             "error": result.get("error")},
                )

                if not result.get("success", False):
                    # wait の timeout 中断 → TIMEOUT 終端へ
                    if result.get("interrupted_by_timeout"):
                        self._record_timeout(rec, idx, step_results)
                        return
                    # cancel 要求による wait 中断は failed ではなく cancel 経路へ
                    if result.get("interrupted_by_cancel"):
                        await self._handle_cancel(
                            rec, session,
                            runtime.cancel_mode or CancelMode.AFTER_CURRENT_STEP,
                            step_results,
                        )
                        return
                    err_class = result.get("error", "internal")
                    if result.get("blocked_by_safety"):
                        err_class = "safety"
                    self._store.transition_status(
                        job_id, JobStatus.FAILED,
                        current_step_index=idx,
                        error_class=err_class,
                        last_step_summary=f"step {idx} failed: {result.get('message', result.get('error', '?'))[:80]}",
                        result=self._with_persistence_warnings(
                            job_id,
                            {
                                "success": False, "recipe": rec.recipe,
                                "steps_executed": step_results,
                                "halted_at_step": idx,
                            },
                        ),
                    )
                    return

                # ループ末尾の cancel チェック。
                # 「最後の step 完了直後に cancel された」ケースを救うため必要。
                # 中間 step の場合は次イテレーション先頭のチェックと等価。
                if runtime.cancel_mode is not None:
                    if runtime.cancel_mode in (
                        CancelMode.AFTER_CURRENT_STEP, CancelMode.SAFE_SHUTDOWN,
                    ):
                        await self._handle_cancel(
                            rec, session, runtime.cancel_mode, step_results,
                        )
                        return

            # 全 step 成功
            _final_result = {
                "success": True, "recipe": rec.recipe,
                "steps_executed": step_results,
                "step_count": len(step_results),
            }
            self._attach_persistence_warnings(job_id, _final_result)
            self._store.transition_status(
                job_id, JobStatus.COMPLETED,
                current_step_index=len(plan.steps) - 1,
                last_step_summary="completed",
                result=_final_result,
            )

        except asyncio.CancelledError:
            # immediate cancel または asyncio runtime teardown による cancel
            # state machine: WAITING/RUNNING/CANCELLING のいずれからも CANCELLED へ向かう。
            # CANCELLED への直接遷移は CANCELLING からのみ許可されているので、
            # まず CANCELLING を経由する。既に CANCELLING / 終端なら _safe_transition でスキップ。
            self._safe_transition(job_id, JobStatus.CANCELLING)
            try:
                self._store.transition_status(
                    job_id, JobStatus.CANCELLED,
                    error_class="cancelled",
                    last_step_summary="cancelled (immediate)",
                    result={
                        "success": False, "recipe": rec.recipe,
                        "steps_executed": step_results,
                        "cancelled": True, "cancel_mode": "immediate",
                    },
                )
            except Exception:
                # 既に終端なら無視 (queued path で既に CANCELLED 等)
                pass
            # CancelledError を再 raise しないと teardown 時に warning が出る
            raise
        except Exception as e:
            logger.exception("Job %s で予期しないエラー", job_id)
            self._store.transition_status(
                job_id, JobStatus.FAILED,
                error_class="internal",
                last_step_summary=f"unexpected: {e}",
                result={"success": False, "error": "InternalError", "message": str(e)},
            )

    async def _run_wait_with_cancel_check(
        self,
        step: WaitStep,
        runtime: _JobRuntime,
    ) -> dict:
        """wait を _WAIT_SLICE_S 刻みで sleep し、間に cancel/timeout チェックを挟む。"""
        remaining = float(step.seconds)
        while remaining > 0:
            # timeout チェック
            if runtime.is_timed_out():
                return {
                    "step_type": "wait",
                    "seconds": float(step.seconds) - remaining,
                    "interrupted_by_timeout": True,
                    "success": False,
                    "error": "timeout",
                    "message": "wait interrupted by job_timeout_s",
                }
            if runtime.cancel_mode is CancelMode.IMMEDIATE:
                # asyncio.Task.cancel() で別途処理されるが、念のため
                raise asyncio.CancelledError("immediate cancel during wait")
            if runtime.cancel_mode in (
                CancelMode.AFTER_CURRENT_STEP, CancelMode.SAFE_SHUTDOWN,
            ):
                # wait は「現在 step」として扱い、即時中断する (ユーザー観点では妥当)
                return {
                    "step_type": "wait",
                    "seconds": float(step.seconds) - remaining,
                    "interrupted_by_cancel": True,
                    "success": False,
                    "error": "cancelled",
                    "message": "wait interrupted by cancel request",
                }
            chunk = min(remaining, _WAIT_SLICE_S)
            await asyncio.sleep(chunk)
            remaining -= chunk
        return {
            "step_type": "wait",
            "seconds": step.seconds,
            "success": True,
        }

    async def _handle_cancel(
        self,
        rec: JobRecord,
        session,
        mode: CancelMode,
        step_results: list[dict],
    ) -> JobStatus:
        """cancel 要求を実際に実行 (safe_shutdown なら shutdown シーケンス)"""
        job_id = rec.job_id

        # v0.5.0.4: safe_shutdown は構造化結果 (dict) を返す
        shutdown_info: dict | None = None
        if mode is CancelMode.SAFE_SHUTDOWN:
            shutdown_info = await self._best_effort_safe_shutdown(session)
            step_results.append({
                "step": -1, "step_type": "safe_shutdown",
                "shutdown": shutdown_info,
                "success": bool(shutdown_info.get("success") or not shutdown_info.get("attempted")),
            })

        # CANCELLED への遷移は CANCELLING 経由が必要。途中で cancel 検出された場合は
        # まず CANCELLING に遷移してから CANCELLED へ。
        current = self._store.get(job_id)
        if current and current.status not in (JobStatus.CANCELLING, JobStatus.CANCELLED):
            self._safe_transition(job_id, JobStatus.CANCELLING)

        _cancel_result = {
            "success": False, "recipe": rec.recipe,
            "steps_executed": step_results,
            "cancelled": True, "cancel_mode": mode.value,
            # v0.5.0.4: safe_shutdown サマリを構造化付与
            "safe_shutdown": shutdown_info if shutdown_info is not None else None,
        }
        self._attach_persistence_warnings(job_id, _cancel_result)
        self._store.transition_status(
            job_id, JobStatus.CANCELLED,
            error_class="cancelled",
            last_step_summary=(
                f"cancelled ({mode.value})"
                + (f" shutdown_success={shutdown_info.get('success')}" if shutdown_info else "")
            ),
            result=_cancel_result,
        )
        return JobStatus.CANCELLED

    def _record_timeout(
        self,
        rec: JobRecord,
        step_idx: int,
        step_results: list[dict],
    ) -> None:
        """job_timeout_s 経過時の TIMEOUT 終端遷移を記録"""
        _result = {
            "success": False, "recipe": rec.recipe,
            "steps_executed": step_results,
            "timed_out_at_step": step_idx,
        }
        self._attach_persistence_warnings(rec.job_id, _result)
        self._store.transition_status(
            rec.job_id, JobStatus.TIMEOUT,
            current_step_index=step_idx,
            error_class="timeout",
            last_step_summary=f"job_timeout_s exceeded at step {step_idx}",
            result=_result,
        )

    # safe_shutdown YAML が無いとき fallback を適用するカテゴリ (v0.5.0.4)
    # その他 (温調器・モータ・ポンプ・電子負荷・リレー等) は YAML 定義必須。
    _SAFE_SHUTDOWN_FALLBACK_CATEGORIES = frozenset({
        "power_supply",
        "source_measure_unit",
    })

    # YAML safe_shutdown 内の各 wait step に許容する最大秒数 (v0.5.0.4)
    _SAFE_SHUTDOWN_WAIT_MAX_S = 10.0

    async def _best_effort_safe_shutdown(self, session) -> dict:
        """安全停止を実行する。

        v0.5.0.4 で返り値を構造化 dict に変更:
          {
            "attempted": bool,
            "source": "yaml" | "fallback_power_supply" | "none",
            "success": bool,                    # 全 step が success なら True
            "steps": [{"step": i, "kind": "command"|"wait", "command": ..., "success": bool, ...}],
            "skipped_reason": str | None,       # source="none" の場合の理由
          }

        優先順位:
          1. YAML の `safe_shutdown` セクションに定義されたシーケンス
          2. 上記が無くかつ metadata.category in {power_supply, source_measure_unit} の場合のみ、
             fallback (set_output OFF + set_voltage 0)
          3. その他のカテゴリ (温調器・モータ等) は fallback 無効 → no-op
        """
        if session is None or session.definition is None:
            return {
                "attempted": False, "source": "none", "success": False,
                "steps": [], "skipped_reason": "no session",
            }

        # 1. YAML 定義された safe_shutdown を優先
        if session.definition.safe_shutdown:
            return await self._run_yaml_shutdown(
                session, session.definition.safe_shutdown,
            )

        # 2. Fallback: 電源系のみ
        category = session.definition.metadata.category
        if category not in self._SAFE_SHUTDOWN_FALLBACK_CATEGORIES:
            return {
                "attempted": False, "source": "none", "success": False,
                "steps": [],
                "skipped_reason": (
                    f"no YAML safe_shutdown; fallback disabled for "
                    f"category='{category}' (allowed: power_supply, source_measure_unit)"
                ),
            }

        # fallback 実行
        steps_result: list[dict] = []
        all_ok = True
        for idx, (cmd_name, args) in enumerate([
            ("set_output", {"state": "OFF"}),
            ("set_voltage", {"voltage": 0}),
        ]):
            cmd_def = session.definition.commands.get(cmd_name)
            if cmd_def is None:
                steps_result.append({
                    "step": idx, "kind": "command", "command": cmd_name,
                    "success": False, "error": "CommandNotFound",
                })
                all_ok = False
                continue
            try:
                step = CommandStep(command=cmd_name, args=args)
                r = await execute_command_step(
                    self._visa, session, step,
                    override_safety=True,
                    override_reason="safe_shutdown by cancel (fallback)",
                )
                ok = bool(r.get("success"))
                all_ok = all_ok and ok
                steps_result.append({
                    "step": idx, "kind": "command", "command": cmd_name,
                    "success": ok,
                    "scpi_sent": r.get("scpi_sent"),
                    "error": r.get("error") if not ok else None,
                })
            except Exception as e:
                all_ok = False
                steps_result.append({
                    "step": idx, "kind": "command", "command": cmd_name,
                    "success": False, "error": type(e).__name__,
                    "message": str(e),
                })

        return {
            "attempted": True,
            "source": "fallback_power_supply",
            "success": all_ok,
            "steps": steps_result,
        }

    async def _run_yaml_shutdown(self, session, steps: list) -> dict:
        """YAML 定義の safe_shutdown ステップを順次実行 (override_safety=True)

        v0.5.0.4 で:
        - 構造化結果 dict を返す
        - wait step は slice 方式 (cancel/timeout を阻害しない、上限 _SAFE_SHUTDOWN_WAIT_MAX_S)
        - 文字列式 ("$var") は受け付けない (数値リテラルのみ、安全停止の予測可能性のため)
        """
        steps_result: list[dict] = []
        all_ok = True

        for idx, rs in enumerate(steps):
            try:
                if rs.step_type == "wait":
                    seconds_raw = rs.wait.get("seconds", 0)
                    # 数値リテラルのみ許可
                    if isinstance(seconds_raw, str):
                        steps_result.append({
                            "step": idx, "kind": "wait",
                            "success": False, "error": "ExpressionNotAllowed",
                            "message": "safe_shutdown wait は数値リテラルのみ許可",
                        })
                        all_ok = False
                        continue
                    seconds = min(float(seconds_raw), self._SAFE_SHUTDOWN_WAIT_MAX_S)
                    # slice 方式 (上限到達・kernel cancel に応答可能)
                    remaining = seconds
                    while remaining > 0:
                        chunk = min(remaining, _WAIT_SLICE_S)
                        await asyncio.sleep(chunk)
                        remaining -= chunk
                    steps_result.append({
                        "step": idx, "kind": "wait",
                        "seconds": seconds,
                        "success": True,
                    })
                else:
                    step = CommandStep(command=rs.command or "", args=rs.args)
                    r = await execute_command_step(
                        self._visa, session, step,
                        override_safety=True,
                        override_reason="safe_shutdown by cancel (YAML)",
                    )
                    ok = bool(r.get("success"))
                    all_ok = all_ok and ok
                    steps_result.append({
                        "step": idx, "kind": "command", "command": rs.command,
                        "success": ok,
                        "scpi_sent": r.get("scpi_sent"),
                        "error": r.get("error") if not ok else None,
                    })
            except Exception as e:
                all_ok = False
                steps_result.append({
                    "step": idx, "kind": getattr(rs, "step_type", "?"),
                    "success": False, "error": type(e).__name__,
                    "message": str(e),
                })

        return {
            "attempted": True,
            "source": "yaml",
            "success": all_ok,
            "steps": steps_result,
        }

    def _attach_persistence_warnings(self, job_id: str, result: dict) -> None:
        """v0.7.0.1: critical event 永続化失敗を Job result に注入する。

        result dict をその場で mutate して `persistence_warnings` キーを追加
        (空ならキー自体を追加しない)。
        """
        if not isinstance(result, dict):
            return
        warnings = self._consume_persistence_warnings(job_id)
        if warnings:
            result["persistence_warnings"] = warnings

    def _with_persistence_warnings(self, job_id: str, result: dict) -> dict:
        """`_attach_persistence_warnings` の immutable 風ヘルパ。
        result を mutate して返す (新規 dict は作らない、軽量化のため)。
        """
        self._attach_persistence_warnings(job_id, result)
        return result

    @staticmethod
    def _poll_cancel_reason(runtime: "_JobRuntime") -> str | None:
        """polling_executor の cancel_check 用。timeout / cancel を文字列で通知。"""
        if runtime.is_timed_out():
            return "timeout"
        if runtime.cancel_mode is not None:
            return "cancel"
        return None

    @staticmethod
    def _update_progress(runtime: "_JobRuntime", progress: dict) -> None:
        """polling step が runtime.current_progress に書き戻す (get_job_status で公開)"""
        runtime.current_progress = progress

    def get_progress(self, job_id: str) -> dict | None:
        """v0.5.1: 現在の polling 進捗 (なければ None)。

        runtime.current_progress は polling executor の on_progress callback
        により上書きされ続けるため、ここでスナップショット (shallow copy)
        を返して、呼び出し側 (MCP JSON serialize) が走っている最中に
        中身が書き換わるのを防ぐ。
        """
        rt = self._runtimes.get(job_id)
        if rt is None or rt.current_progress is None:
            return None
        return dict(rt.current_progress)

    def _safe_transition(self, job_id: str, to: JobStatus) -> None:
        """遷移ルール違反は黙って無視 (cancelling 中の状態変更等)"""
        try:
            self._store.transition_status(job_id, to)
        except Exception:
            pass

    @staticmethod
    def _step_summary(step) -> str:
        if isinstance(step, WaitStep):
            return f"wait {step.seconds}s"
        if isinstance(step, WaitUntilStep):
            target = step.timestamp or f"+{step.seconds_from_now}s"
            return f"wait_until {target}"
        if isinstance(step, WaitForConditionStep):
            return (
                f"wait_for_condition {step.instrument}.{step.command} "
                f"[{step.condition_expr}]"
            )
        if isinstance(step, WaitForStableStep):
            return (
                f"wait_for_stable {step.instrument}.{step.command} "
                f"tol={step.tolerance} window={step.window_s}s"
            )
        if isinstance(step, CommandStep):
            return f"command {step.command}"
        return f"step type={getattr(step, 'type', '?')}"
