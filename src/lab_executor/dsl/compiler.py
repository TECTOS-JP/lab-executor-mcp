"""
v0.8.0: DSL validator + compiler

ExperimentPlan を walk して:
- 15 項目を検証 (instrument / command / safety / verify / state_query 等)
- 既存 IR (CommandStep / WaitStep / ...) に compile
- sweep を展開、parallel を TargetExecution リストに変換
- safe_shutdown step は marker として残し、JobManager 側で処理

返り値は `CompiledPlan` データクラス。validation だけしたい場合は
`validate_only=True` で compile を省略可能 (ただし基本検証は同じ)。
"""
from __future__ import annotations
import logging
import math
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    # v1.11: backend layer import を runtime 候補 module の top-level から
    # 排除 (KNOWN_V111_TO_RESOLVE 解消)。compiler は SessionManager を
    # 型ヒントとしてのみ参照し、実体は呼び出し側 (JobManager) が注入。
    from lab_visa_mcp.session_manager import SessionManager

from lab_executor.dsl.schema import (
    ExperimentPlan, Step,
    DSLCommandStep, DSLQueryStep,
    DSLWaitStep, DSLWaitUntilStep,
    DSLWaitForConditionStep, DSLWaitForStableStep,
    DSLBarrierStep, DSLSafeShutdownStep,
    DSLSweepStep, DSLParallelStep,
    MAX_SWEEP_POINTS, MAX_PARALLEL_CONCURRENCY, MAX_PARALLEL_BRANCHES,
)
from lab_executor.experiment_ir import (
    CommandStep, Plan, Step as IRStep, WaitStep,
    WaitUntilStep, WaitForConditionStep, WaitForStableStep,
    BarrierStep,
)
from lab_executor.group.resolver import resolve_resource, ResolveError
from lab_executor.system_config import SystemConfig
from lab_executor import safety as sf

logger = logging.getLogger(__name__)


# ============================================================
# Compile 結果
# ============================================================


@dataclass
class CompiledPlan:
    """validation + compile の結果"""
    valid: bool
    errors: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)
    # 展開後 step 数 / 推定 duration / required_resources / verify 使用有無 等
    summary: dict[str, Any] = field(default_factory=dict)
    # compile 結果: 「single thread plan」 + 「parallel branch plans」のリスト
    # v0.8.0.1: parallel は top-level 末尾 1 回のみ許可 (placement 制約強化)
    main_plan: Plan | None = None
    parallel_groups: list[dict] = field(default_factory=list)
    # safe_shutdown ステップが含まれていたか
    has_safe_shutdown: bool = False
    # v0.8.0.1: safe_shutdown.targets が明示指定された場合の対象 resource リスト
    # None なら "Plan で使用した全 resource" (= used_resources)
    safe_shutdown_targets: list[str] | None = None
    # 解決済み bindings (alias → resource_name)
    resolved_instruments: dict[str, str] = field(default_factory=dict)
    # v0.8.0.1: dry_run 用 rendered SCPI / safety / verify 情報
    rendered_steps: list[dict] = field(default_factory=list)
    # v0.8.1: 「Plan 内で参照された全 resource」と「scheduler が lock する resource」を分離
    # required_resources (= main_plan.required_resources) は scheduler 投入用 (parallel branches は別 Job 経路を辿るため、main_plan のみの収集)
    # used_resources は dry-run / safe_shutdown のスコープ表示用 (parallel branches も含む完全な使用 resource 集合)
    used_resources: list[str] = field(default_factory=list)
    # v0.8.3: unit 解決結果 (unit 未指定なら unit=None / unit_bindings={})
    unit_resolution: dict[str, Any] = field(default_factory=dict)


# ============================================================
# Validator + Compiler 本体
# ============================================================


class _Context:
    """compile 中の状態を持つ context"""
    def __init__(
        self,
        plan: ExperimentPlan,
        session_mgr: SessionManager,
        system_config: SystemConfig,
        path: str = "",
    ) -> None:
        self.plan = plan
        self.session_mgr = session_mgr
        self.system_config = system_config
        self.path = path  # 例: "steps[3].body[0]"
        self.errors: list[dict] = []
        self.warnings: list[dict] = []
        # 使用された resource_name の収集 (canonical sorted で出す)
        self.required_resources: set[str] = set()
        # 解決済み instrument_ref → resource_name
        self.resolved: dict[str, str] = {}
        # v0.8.3: unit 解決メタ情報
        # unit_bindings: 指定 unit から取り出した role → alias
        # explicit_bindings: plan.bindings 原文
        # effective_bindings: unit_bindings + explicit_bindings (override 優先)
        # overridden_roles: explicit が unit を上書きした role 一覧
        self.unit_bindings: dict[str, str] = {}
        self.explicit_bindings: dict[str, str] = dict(plan.bindings)
        self.effective_bindings: dict[str, str] = dict(plan.bindings)
        self.overridden_roles: list[str] = []
        self.unit_name: str | None = plan.unit
        # raw resource を「unit 指定があるのに使った」かどうか
        self._raw_resource_with_unit_warned: set[str] = set()
        # 推定 duration (s)
        self.estimated_duration_s: float = 0.0
        # verify 使用 step 数 / polling 使用有無
        self.uses_verify_count: int = 0
        self.uses_polling: bool = False
        # safe_shutdown 含む
        self.has_safe_shutdown: bool = False
        # v0.8.0.1: safe_shutdown.targets が明示指定された場合の解決済み resource list。
        # None ならコンパイル後の required_resources を使う。
        self.safe_shutdown_targets: list[str] | None = None
        # parallel が含まれていたか
        self.parallel_groups: list[dict] = []
        # validation-only mode?
        self.dry: bool = False
        # v0.8.1: parallel branch ごとの使用 resource 収集用。
        # parallel walk 中だけ set を入れ、終わったら None に戻す。
        # _resolve_instrument は None でなければここにも追加する。
        self._branch_resources: set[str] | None = None
        # v2.13.0: plan 内の先行 CommandStep を resource 単位で記録した
        # 「予測 history」。validator の precondition チェックで
        # session.command_history (= 実機の実行履歴) と OR して使う。
        # plan walk が順次進むにつれて、各 CommandStep の command_name
        # を該当 resource_name の list に append する。
        # 注: parallel branch 内の step は branch_resources と同様に
        # 並列内では互いに見えない (順序保証なしのため安全側) — v2.13
        # では sequential / sweep 経路のみで積む。
        self._predicted_history: dict[str, list[str]] = {}
        # parallel walk 中なら、branch 内の予測 history を branch
        # 終了時に破棄するため、開始時に snapshot を取る
        self._parallel_depth: int = 0
        # rendered SCPI (dry_run 用)
        self.rendered_steps: list[dict] = []
        # 上限カウント
        self.total_expanded_steps: int = 0

    def add_error(
        self, error_class: str, message: str,
        step_index: int | None = None,
        field_path: str | None = None,
        recommended_next_actions: list[dict] | None = None,
        **extra,
    ) -> None:
        """v0.8.1: step_path + field_path + recommended_next_actions を強化。
        AI エージェントが「どこを / なぜ / どう直すか」を判断しやすくする。
        """
        e: dict[str, Any] = {
            "error_class": error_class,
            "message": message,
            "step_path": self.path,    # v0.8.1: 階層構造内 step 位置
        }
        if step_index is not None:
            e["step_index"] = step_index
        if field_path:
            e["field_path"] = field_path
        if recommended_next_actions:
            e["recommended_next_actions"] = recommended_next_actions
        # backward compat: "path" キーも残す (v0.8.0 までのテスト互換)
        e["path"] = self.path
        e.update(extra)
        self.errors.append(e)

    def add_warning(
        self, warning_class: str, message: str,
        step_index: int | None = None,
        field_path: str | None = None,
        recommended_next_actions: list[dict] | None = None,
        **extra,
    ) -> None:
        """v0.8.1: warnings にも step_path / field_path / recommended_next_actions を強化"""
        w: dict[str, Any] = {
            "warning_class": warning_class,
            "message": message,
            "step_path": self.path,
        }
        if step_index is not None:
            w["step_index"] = step_index
        if field_path:
            w["field_path"] = field_path
        if recommended_next_actions:
            w["recommended_next_actions"] = recommended_next_actions
        w["path"] = self.path  # backward compat
        w.update(extra)
        self.warnings.append(w)


def _resolve_instrument(
    ctx: _Context, ref: str, step_index: int,
) -> tuple[str | None, "InstrumentSession | None"]:
    """DSL の instrument ref を (resource_name, session) に解決。

    解決順序 (実装方針 #13):
      1. plan.bindings (e.g. "$psu" → "psu001")
      2. system_config の experiment_units (省略、bindings に集約)
      3. _system.yaml instruments alias
      4. raw VISA resource (許可、ただし alias 推奨を warning)
    """
    if not ref:
        ctx.add_error(
            "validation",
            "instrument が空です", step_index=step_index,
        )
        return None, None

    target = ref
    # v0.8.3: $role 参照は effective_bindings (unit + explicit) で解決
    if ref.startswith("$"):
        role = ref[1:]
        if role not in ctx.effective_bindings:
            # unit 指定があるのに role 未定義の場合は unit_role_missing
            if ctx.unit_name:
                ctx.add_error(
                    "unit_role_missing",
                    f"role '{role}' が unit '{ctx.unit_name}' / explicit bindings の"
                    f"いずれにも定義されていません (ref={ref!r})",
                    step_index=step_index,
                    field_path="bindings",
                    recommended_next_actions=[
                        {"action": "add_binding_override",
                         "reason": f"plan.bindings に '{role}': '<alias>' を追加"},
                        {"action": "choose_different_unit",
                         "reason": f"role '{role}' を含む experiment_unit を選択"},
                    ],
                )
            else:
                ctx.add_error(
                    "unknown_binding",
                    f"plan.bindings に '{role}' がありません (ref={ref!r})",
                    step_index=step_index,
                    recommended_next_actions=[
                        {"action": "fix_bindings",
                         "reason": f"plan.bindings に '{role}' を追加してください"},
                    ],
                )
            return None, None
        target = ctx.effective_bindings[role]

    # alias / resource_name 解決
    try:
        resource = resolve_resource(target, {}, ctx.system_config)
    except ResolveError as e:
        ctx.add_error(
            "unknown_instrument",
            f"instrument '{target}' を resource として解決できません: {e}",
            step_index=step_index,
            recommended_next_actions=[
                {"action": "list_resources", "tool": "list_resources"},
                {"action": "check_bindings",
                 "reason": "plan.bindings / _system.yaml.instruments を確認"},
            ],
        )
        return None, None

    # raw resource 指定 (alias を介さず) → warning
    if target == resource and "::" in target:
        ctx.add_warning(
            "raw_resource_used",
            f"raw VISA resource '{target}' が直接指定されています。"
            f"alias / bindings の使用を推奨します",
            instrument=target,
        )
        # v0.8.3: unit 指定ありの Plan で raw resource を直接使った場合は
        # 別 warning を出す (typo で unit role を bypass している可能性が高い)
        if ctx.unit_name and target not in ctx._raw_resource_with_unit_warned:
            ctx._raw_resource_with_unit_warned.add(target)
            ctx.add_warning(
                "raw_resource_used_with_unit",
                f"unit '{ctx.unit_name}' が指定された Plan で raw resource "
                f"'{target}' が直接使われています。"
                f"unit role 経由の指定 ($role) を推奨します",
                instrument=target,
                field_path="instrument",
                recommended_next_actions=[
                    {"action": "use_role_reference",
                     "reason": f"unit '{ctx.unit_name}' に該当 role を追加し $role で参照"},
                ],
            )

    ctx.resolved[ref] = resource
    ctx.required_resources.add(resource)
    # v0.8.1: parallel branch walk 中なら branch local にも記録
    if ctx._branch_resources is not None:
        ctx._branch_resources.add(resource)

    session = ctx.session_mgr.get_session(resource)
    if session is None or session.definition is None:
        ctx.add_error(
            "not_identified",
            f"instrument '{ref}' (→ {resource}) は未識別です",
            step_index=step_index,
            recommended_next_actions=[
                {"action": "identify_instrument",
                 "tool": "identify_instrument",
                 "args": {"resource_name": resource}},
            ],
        )
        return resource, None
    return resource, session


def _validate_command(
    ctx: _Context,
    session,
    command_name: str,
    args: dict,
    step_index: int,
    expect_type: str | None = None,
    *,
    instrument_ref: str | None = None,
    args_raw: dict | None = None,
) -> None:
    """v0.8.1: instrument_ref / args_raw を渡すことで rendered_steps の
    可読性を向上 (LLM が「どこの DSL を直すか」を判断しやすくする)。
    """
    """command の存在 + 型 + パラメータ + safety を検証"""
    from lab_executor.utils.param_validator import validate_and_build_scpi, ParameterValidationError

    if session is None or session.definition is None:
        return

    cmd_def = session.definition.commands.get(command_name)
    if cmd_def is None:
        ctx.add_error(
            "unknown_command",
            f"command '{command_name}' が機器定義に存在しません",
            step_index=step_index,
            command=command_name,
            recommended_next_actions=[
                {"action": "list_commands", "tool": "list_commands",
                 "args": {"resource_name": session.resource_name}},
            ],
        )
        return

    if expect_type and cmd_def.type != expect_type:
        ctx.add_error(
            "wrong_command_type",
            f"command '{command_name}' の type は '{cmd_def.type}'、"
            f"'{expect_type}' が必要です",
            step_index=step_index,
            command=command_name,
        )
        return

    # parameter validation + SCPI rendering
    try:
        rendered = validate_and_build_scpi(cmd_def, args)
    except ParameterValidationError as e:
        ctx.add_error(
            "parameter_invalid",
            f"command '{command_name}' のパラメータ検証失敗: {e}",
            step_index=step_index,
            command=command_name,
        )
        return

    # v2.13.0: safety validation には session.command_history (実機の
    # 実行履歴) + plan 内の予測 history を結合して渡す。これで plan の
    # 先行 step (例: set_voltage_protection / set_current_protection)
    # を precondition 満たす扱いにできる。parallel branch 内では他
    # branch を見ない (現実装では parallel_depth>0 中は積まない)。
    predicted = ctx._predicted_history.get(session.resource_name, [])
    combined_history = list(session.command_history) + predicted
    violations = sf.validate(
        session.definition, command_name, args,
        session_history=combined_history,
    )
    mode = sf.get_safety_mode()
    if violations:
        if mode in ("strict",):
            ctx.add_error(
                "safety_violation",
                f"command '{command_name}' は strict mode で safety に違反します",
                step_index=step_index,
                command=command_name,
                violations=list(violations),
                recommended_next_actions=[
                    {"action": "review_safety_constraints",
                     "tool": "list_safety_constraints",
                     "args": {"resource_name": session.resource_name}},
                ],
            )
        else:
            ctx.add_warning(
                "safety_violation",
                f"command '{command_name}' は safety violation を含みますが "
                f"{mode} mode のため警告のみ",
                command=command_name,
                violations=list(violations),
            )

    # polling_safe ヒント (query 用)
    if expect_type == "query" and cmd_def is not None and not cmd_def.polling_safe:
        ctx.add_warning(
            "polling_safe_false",
            f"command '{command_name}' は polling_safe=False。"
            f"polling/monitor で使うと副作用の可能性",
            command=command_name,
        )

    # verify 設定の確認
    if cmd_def.verify is not None:
        rb = session.definition.commands.get(cmd_def.verify.readback_command)
        if rb is None:
            ctx.add_warning(
                "verify_readback_missing",
                f"command '{command_name}' の verify.readback_command "
                f"'{cmd_def.verify.readback_command}' が定義されていません",
                command=command_name,
            )
        elif rb.type != "query":
            ctx.add_warning(
                "verify_readback_not_query",
                f"command '{command_name}' の verify.readback_command は "
                f"query 型である必要がありますが '{rb.type}' です",
                command=command_name,
            )
        else:
            ctx.uses_verify_count += 1

    # 推定 duration (1 命令 ~ 50ms と仮定、verify あれば +50ms)
    ctx.estimated_duration_s += 0.05 + (0.05 if cmd_def.verify is not None else 0.0)

    # v2.13.0: plan walk 順序で予測 history を更新。次以降の step の
    # precondition チェックがこの command を「呼ばれた」とみなせる。
    # parallel branch 内 (parallel_depth>0) では順序保証がないため
    # 積まない (安全側)。
    if ctx._parallel_depth == 0:
        ctx._predicted_history.setdefault(
            session.resource_name, []).append(command_name)

    # v0.8.1: rendered_steps を agent 可読構造に強化
    # step_path (階層内位置) / instrument_ref (元 DSL の参照) / resolved_resource /
    # args_raw (テンプレ展開前) / args_resolved (展開後) を含める
    ctx.rendered_steps.append({
        "step_index": step_index,
        "step_path": ctx.path,
        "path": ctx.path,                                  # backward compat
        "step_type": "command" if cmd_def.type == "write" else "query",
        "command_type": cmd_def.type,                      # "write" or "query"
        # 元 DSL の instrument_ref ($psu 等)
        "instrument_ref": instrument_ref if instrument_ref is not None else session.resource_name,
        "resolved_resource": session.resource_name,
        # 後方互換: 既存 "instrument" は resolved_resource を入れる
        "instrument": session.resource_name,
        "command": command_name,
        # raw (テンプレ "{voltage}" を含む元 args) と resolved (展開後)
        "args_raw": dict(args_raw) if args_raw is not None else dict(args),
        "args_resolved": dict(args),
        "args": dict(args),                                # backward compat
        "rendered_scpi": rendered,
        "rendered": rendered,                              # alias
        "safety": {
            "status": "ok" if not violations else f"violated_{mode}",
            "violations": list(violations) if violations else [],
            "mode": mode,
        },
        "verify": (
            {
                "enabled": True,
                "readback_command": cmd_def.verify.readback_command,
                "tolerance": cmd_def.verify.tolerance,
                "retry": cmd_def.verify.retry,
            }
            if cmd_def.verify is not None else {"enabled": False}
        ),
    })


# ============================================================
# walk step (DSL → IR conversion)
# ============================================================


def _convert_step(
    ctx: _Context,
    s: Step,
    step_index: int,
    variables: dict[str, Any] | None = None,
) -> list[IRStep]:
    """DSL step を IRStep のリストに変換 (sweep は複数 step に展開)。

    parallel は IR Plan には直接落とせないため、ctx.parallel_groups に蓄積。
    """
    variables = variables or {}

    # arg のテンプレート展開 (簡易: "{voltage}" → variables["voltage"])
    def _expand(value: Any) -> Any:
        if isinstance(value, str):
            # {var} 形式
            if value.startswith("{") and value.endswith("}"):
                name = value[1:-1]
                if name in variables:
                    return variables[name]
                # variables に無いがテンプレート → そのまま (validation error)
                ctx.add_error(
                    "unknown_variable",
                    f"テンプレート変数 '{name}' が未定義です",
                    step_index=step_index,
                )
                return value
        return value

    def _expand_args(args: dict) -> dict:
        return {k: _expand(v) for k, v in args.items()}

    if isinstance(s, (DSLCommandStep, DSLQueryStep)):
        is_query = isinstance(s, DSLQueryStep)
        resource, session = _resolve_instrument(ctx, s.instrument, step_index)
        args = _expand_args(s.args)
        _validate_command(
            ctx, session, s.command, args, step_index,
            expect_type="query" if is_query else None,
            instrument_ref=s.instrument,
            args_raw=dict(s.args),
        )
        if resource is None:
            return []
        return [CommandStep(
            command=s.command,
            args=args,
            description=s.description,
            instrument=resource,
        )]

    if isinstance(s, DSLWaitStep):
        ctx.estimated_duration_s += float(s.seconds)
        return [WaitStep(seconds=float(s.seconds), description=s.description)]

    if isinstance(s, DSLWaitUntilStep):
        if s.seconds_from_now is not None:
            ctx.estimated_duration_s += float(s.seconds_from_now)
        # timestamp の場合は実行時計算 (推定不可)
        return [WaitUntilStep(
            timestamp=s.timestamp,
            seconds_from_now=s.seconds_from_now,
            description=s.description,
        )]

    if isinstance(s, DSLWaitForConditionStep):
        ctx.uses_polling = True
        resource, session = _resolve_instrument(ctx, s.instrument, step_index)
        _validate_command(
            ctx, session, s.command, _expand_args(s.args),
            step_index, expect_type="query",
            instrument_ref=s.instrument,    # v0.8.1.1: polling 系も $ref / args_raw を保持
            args_raw=dict(s.args),
        )
        ctx.estimated_duration_s += min(float(s.timeout_s), 30.0)  # 推定上限 30s
        if resource is None:
            return []
        return [WaitForConditionStep(
            instrument=resource,
            command=s.command,
            args=_expand_args(s.args),
            condition_expr=s.condition_expr,
            interval_s=s.interval_s,
            timeout_s=s.timeout_s,
            command_timeout_s=s.command_timeout_s,
            value_path=s.value_path,
            retry_on_error=s.retry_on_error,
            max_consecutive_errors=s.max_consecutive_errors,
            description=s.description,
        )]

    if isinstance(s, DSLWaitForStableStep):
        ctx.uses_polling = True
        resource, session = _resolve_instrument(ctx, s.instrument, step_index)
        _validate_command(
            ctx, session, s.command, _expand_args(s.args),
            step_index, expect_type="query",
            instrument_ref=s.instrument,
            args_raw=dict(s.args),
        )
        ctx.estimated_duration_s += min(float(s.timeout_s), 30.0)
        if resource is None:
            return []
        return [WaitForStableStep(
            instrument=resource,
            command=s.command,
            args=_expand_args(s.args),
            tolerance=s.tolerance,
            window_s=s.window_s,
            interval_s=s.interval_s,
            timeout_s=s.timeout_s,
            command_timeout_s=s.command_timeout_s,
            value_path=s.value_path,
            min_samples=s.min_samples,
            retry_on_error=s.retry_on_error,
            max_consecutive_errors=s.max_consecutive_errors,
            description=s.description,
        )]

    if isinstance(s, DSLBarrierStep):
        return [BarrierStep(
            name=s.name, timeout_s=s.timeout_s,
            description=s.description,
        )]

    if isinstance(s, DSLSafeShutdownStep):
        ctx.has_safe_shutdown = True
        # v0.8.1: targets=[] (空配列) は曖昧なので validation error
        # ("全対象" なら targets を省略、"何もしない" なら step 自体不要)
        if s.targets is not None and len(s.targets) == 0:
            ctx.add_error(
                "safe_shutdown_targets_empty",
                "safe_shutdown.targets=[] (空配列) は曖昧なため拒否します。"
                "全 used_resources を対象にするには targets を省略してください。"
                "何もしない場合は safe_shutdown step 自体を削除してください",
                step_index=step_index,
                field_path=f"{ctx.path}.targets",
                recommended_next_actions=[
                    {"action": "omit_targets",
                     "reason": "全 used_resources を対象にする場合は targets を書かない"},
                    {"action": "remove_step",
                     "reason": "何もしない場合は step ごと削除する"},
                ],
            )
            return []
        # v0.8.0.1: targets 指定があれば実 resource に解決して保持
        resolved_targets: list[str] | None = None
        if s.targets:
            resolved_targets = []
            for t in s.targets:
                r, _ = _resolve_instrument(ctx, t, step_index)
                if r is not None:
                    resolved_targets.append(r)
            # 既存解決と統合 (canonical sorted)
            resolved_targets = sorted(set(resolved_targets))
        if resolved_targets:
            # 複数 safe_shutdown step は union として扱う
            if ctx.safe_shutdown_targets is None:
                ctx.safe_shutdown_targets = resolved_targets
            else:
                ctx.safe_shutdown_targets = sorted(
                    set(ctx.safe_shutdown_targets) | set(resolved_targets)
                )
        # IR 上には残さない (Job 終端で JobManager が _best_effort_safe_shutdown を実行)
        # 但し dry_run の rendered 表示用に rendered_steps へ追加
        # v0.8.1.1: step_path を追加 (他の rendered_steps と一貫性)
        ctx.rendered_steps.append({
            "step_index": step_index,
            "step_path": ctx.path,
            "path": ctx.path,                # backward compat
            "step_type": "safe_shutdown",
            "targets": (
                resolved_targets if resolved_targets is not None
                else "all_used_resources"
            ),
            "targets_raw": list(s.targets) if s.targets else None,
        })
        return []

    if isinstance(s, DSLSweepStep):
        # v0.8.1: sweep body 内に parallel が含まれていたら reject
        # ("各値ごとに parallel" vs "全展開後 parallel" が曖昧、v1.x で再検討)
        for j, body_step in enumerate(s.body):
            if isinstance(body_step, DSLParallelStep):
                ctx.add_error(
                    "parallel_inside_sweep",
                    "sweep.body 内の parallel step は v0.8.x では禁止です "
                    "(各値ごと parallel か全展開後 parallel かが曖昧)。"
                    "sweep を抜けた top-level 末尾に parallel を配置してください。"
                    "v1.x で再検討予定",
                    step_index=step_index,
                    field_path=f"{ctx.path}.body[{j}]",
                    recommended_next_actions=[
                        {"action": "move_parallel_outside_sweep",
                         "reason": "parallel を sweep の外側 (top-level 末尾) に移動"},
                    ],
                )
                return []
        # sweep を展開: 各 value で body を複製
        values = s.values.expand()
        if len(values) * max(1, len(s.body)) + ctx.total_expanded_steps > 10000:
            ctx.add_error(
                "expanded_too_large",
                f"sweep 展開後の総 step 数が大きすぎます "
                f"({len(values)} × {len(s.body)})",
                step_index=step_index,
                field_path=f"{ctx.path}.values",
                expanded_steps=len(values) * max(1, len(s.body)),
                limit=10000,
                recommended_next_actions=[
                    {"action": "split_plan",
                     "reason": "sweep の値を分割して複数 plan として実行"},
                ],
            )
            return []
        expanded: list[IRStep] = []
        saved_path = ctx.path
        for i, v in enumerate(values):
            iter_vars = dict(variables)
            iter_vars[s.parameter] = v
            for j, body_step in enumerate(s.body):
                ctx.path = f"{saved_path}.sweep[{i}].body[{j}]"
                converted = _convert_step(ctx, body_step, step_index, iter_vars)
                for ir_step in converted:
                    if isinstance(ir_step, CommandStep):
                        ir_step.sweep_index = i
                        ir_step.sweep_param = s.parameter
                        ir_step.sweep_value = v
                expanded.extend(converted)
                ctx.total_expanded_steps += 1
        ctx.path = saved_path
        return expanded

    if isinstance(s, DSLParallelStep):
        # parallel: 各 branch を別 Plan として収集 → ctx.parallel_groups に
        saved_path = ctx.path
        # v0.8.1: branch ごとに使用 resource を集計し、共有 resource を warning
        branch_resources_list: list[set[str]] = []
        branch_plans: list[Plan] = []
        for i, branch in enumerate(s.branches):
            ctx.path = f"{saved_path}.parallel.branches[{i}]"
            branch_ir: list[IRStep] = []
            # v0.8.1: branch 内で出現した resource を直接記録 (set 差分では共有検出不可)
            ctx._branch_resources = set()
            # v2.13.0: branch 内では予測 history を積まない (並列内は
            # 順序保証なし)。snapshot を取って branch 終了時に復元
            # することで「branch 内で積まれた history が他 branch に
            # 漏れる」事故を防ぐ
            ctx._parallel_depth += 1
            history_snapshot = {
                k: list(v) for k, v in ctx._predicted_history.items()
            }
            for j, b_step in enumerate(branch):
                # v0.8.1: branch 内に nested parallel が含まれていたら reject
                if isinstance(b_step, DSLParallelStep):
                    ctx.add_error(
                        "nested_parallel",
                        "parallel.branches 内の nested parallel は v0.8.x で禁止です。"
                        "branches を平坦化するか、plan を分割してください",
                        step_index=step_index,
                        field_path=f"{ctx.path}[{j}]",
                        recommended_next_actions=[
                            {"action": "flatten_branches",
                             "reason": "nested parallel は実行順序が複雑化するため禁止"},
                        ],
                    )
                    continue
                old_path = ctx.path
                ctx.path = f"{ctx.path}[{j}]"
                branch_ir.extend(_convert_step(ctx, b_step, step_index, variables))
                ctx.path = old_path
            branch_resources_list.append(set(ctx._branch_resources))
            branch_plans.append(Plan(
                name=f"parallel_branch_{i}",
                steps=branch_ir,
            ))
            # v2.13.0: branch 終了で snapshot を戻す + parallel_depth--
            ctx._predicted_history = history_snapshot
            ctx._parallel_depth -= 1
        ctx._branch_resources = None
        ctx.path = saved_path

        # v0.8.1: 共有 resource 検出 → warning
        # (各 branch の resource 集合の積を取り、空でないものがあれば共有あり)
        for i in range(len(branch_resources_list)):
            for k in range(i + 1, len(branch_resources_list)):
                shared = branch_resources_list[i] & branch_resources_list[k]
                if shared:
                    for sr in sorted(shared):
                        ctx.add_warning(
                            "parallel_shared_resource",
                            f"parallel.branches[{i}] と branches[{k}] が同じ resource "
                            f"'{sr}' を使用します。実行は target-level lock で直列化される"
                            f"ため、真の並列にはなりません",
                            step_index=step_index,
                            field_path=f"{saved_path}.parallel",
                            resource=sr,
                            branches=[i, k],
                            recommended_next_actions=[
                                {"action": "merge_branches",
                                 "reason": "共有 resource を使う branch は単一 branch に統合"},
                                {"action": "use_distinct_resources",
                                 "reason": "branch ごとに別 resource を割り当てる"},
                            ],
                        )

        ctx.parallel_groups.append({
            "step_index": step_index,
            "concurrency": s.concurrency,
            "branch_plans": branch_plans,
            "branch_resources": [sorted(r) for r in branch_resources_list],
        })
        # IR Plan には parallel を直接落とさない (JobManager が special handle)
        return []

    # 未対応 step (理論上は到達不可、schema discriminator で reject される)
    ctx.add_error(
        "unsupported_step_type",
        f"未対応の step type: {getattr(s, 'type', '?')}",
        step_index=step_index,
    )
    return []


# ============================================================
# Public API
# ============================================================


# ============================================================
# v0.8.3: unit 解決ヘルパ
# ============================================================


def _resolve_unit_bindings(ctx: _Context) -> None:
    """plan.unit が指定されていれば experiment_units から bindings を引き、
    explicit bindings との merge 結果を ctx に格納する。

    解決順序 (実装方針 #2):
        unit_bindings → explicit_bindings override → alias → raw resource
    """
    unit_name = ctx.plan.unit
    if not unit_name:
        # unit 指定無し → effective_bindings は plan.bindings そのまま
        return

    unit = ctx.system_config.get_unit(unit_name)
    if unit is None:
        ctx.add_error(
            "unknown_unit",
            f"unit '{unit_name}' が system_config.experiment_units に存在しません",
            field_path="unit",
            recommended_next_actions=[
                {"action": "list_experiment_units",
                 "tool": "list_experiment_units"},
                {"action": "fix_unit_name",
                 "reason": "system_config.yaml の experiment_units に該当 unit を追加"},
            ],
        )
        # effective_bindings は explicit のみで継続 (後続 step の解析を進めるため)
        return

    ctx.unit_bindings = dict(unit.bindings)
    # merge: unit_bindings + explicit_bindings (explicit が override)
    merged: dict[str, str] = dict(ctx.unit_bindings)
    overridden: list[str] = []
    for k, v in ctx.explicit_bindings.items():
        if k in merged and merged[k] != v:
            overridden.append(k)
        merged[k] = v
    ctx.effective_bindings = merged
    ctx.overridden_roles = sorted(overridden)


def validate_and_compile(
    plan_dict: dict[str, Any],
    session_mgr: SessionManager,
    system_config: SystemConfig,
) -> CompiledPlan:
    """JSON dict (LLM 出力) を validate + compile。

    返り値の `CompiledPlan.valid` で成否を判定。
    `errors` が空でない場合は valid=False。warnings は補助情報。
    """
    # schema validation
    try:
        plan = ExperimentPlan(**plan_dict)
    except Exception as e:
        return CompiledPlan(
            valid=False,
            errors=[{
                "error_class": "schema_invalid",
                "message": str(e),
                "path": "$",
            }],
        )

    ctx = _Context(plan, session_mgr, system_config)

    # v0.8.3: unit 解決 (effective_bindings の生成 + unit_resolution summary)
    _resolve_unit_bindings(ctx)

    # v0.8.0.1: parallel placement 制約 ── top-level steps の末尾 1 回のみ許可。
    # 中間に parallel を置くと前後の step との実行順序が曖昧になるため、
    # MVP では「最後の step に 1 つだけ」を validation で強制する。
    # body 内 (sweep の中など) の parallel も MVP では不可。
    parallel_indices = [
        i for i, s in enumerate(plan.steps) if isinstance(s, DSLParallelStep)
    ]
    if parallel_indices:
        last_idx = len(plan.steps) - 1
        if len(parallel_indices) > 1:
            ctx.add_error(
                "parallel_placement",
                f"parallel step は 1 plan に 1 つだけ許可されます "
                f"(検出: index {parallel_indices})",
                step_index=parallel_indices[1],
                recommended_next_actions=[
                    {"action": "split_plan",
                     "reason": "複数 parallel を使う場合は plan を分割してください"},
                ],
            )
        elif parallel_indices[0] != last_idx:
            ctx.add_error(
                "parallel_placement",
                f"parallel step は top-level steps の末尾 (index {last_idx}) に "
                f"配置してください (現在 index {parallel_indices[0]})。"
                f"末尾以降に他の step を置けません",
                step_index=parallel_indices[0],
                recommended_next_actions=[
                    {"action": "move_parallel_to_end",
                     "reason": "parallel の前段に置きたい step は parallel.branches の "
                               "各 branch 先頭に複製してください"},
                ],
            )

    ir_steps: list[IRStep] = []
    for i, s in enumerate(plan.steps):
        ctx.path = f"steps[{i}]"
        ir_steps.extend(_convert_step(ctx, s, i, dict(plan.variables)))
        ctx.total_expanded_steps += 1

    # v0.8.1: used_resources = parallel branches も含めた完全な使用 resource 集合
    # ctx.required_resources は main_plan + parallel branches を walk する過程ですべて
    # 加算されているため used_resources としても利用可能
    used = sorted(ctx.required_resources)
    # required_resources (scheduler 投入用) は main_plan の resource のみ:
    # parallel branches 含めた used resource は別途 scheduler に渡されるため、
    # 通常の単一 Plan path では main_plan.required_resources = used
    # (現状は両者同値、将来 parallel が独立スケジューラに渡される時に分離)

    unit_resolution = _build_unit_resolution(ctx)

    if ctx.errors:
        return CompiledPlan(
            valid=False,
            errors=ctx.errors,
            warnings=ctx.warnings,
            summary=_make_summary(ctx, plan, used, unit_resolution),
            rendered_steps=list(ctx.rendered_steps),
            resolved_instruments=dict(ctx.resolved),
            used_resources=used,
            unit_resolution=unit_resolution,
        )

    main_plan = Plan(
        name=plan.name or "experiment",
        steps=ir_steps,
        required_resources=used,
        metadata={
            "dsl_version": plan.dsl_version,
            "rendered_step_count": len(ctx.rendered_steps),
        },
    )

    return CompiledPlan(
        valid=True,
        errors=[],
        warnings=ctx.warnings,
        summary=_make_summary(ctx, plan, used, unit_resolution),
        main_plan=main_plan,
        parallel_groups=ctx.parallel_groups,
        has_safe_shutdown=ctx.has_safe_shutdown,
        safe_shutdown_targets=ctx.safe_shutdown_targets,
        resolved_instruments=dict(ctx.resolved),
        rendered_steps=list(ctx.rendered_steps),
        used_resources=used,
        unit_resolution=unit_resolution,
    )


def _build_unit_resolution(ctx: _Context) -> dict[str, Any]:
    """v0.8.3: unit_resolution summary を組み立てる"""
    return {
        "unit": ctx.unit_name,
        "unit_bindings": dict(ctx.unit_bindings),
        "explicit_bindings": dict(ctx.explicit_bindings),
        "effective_bindings": dict(ctx.effective_bindings),
        "overridden_roles": list(ctx.overridden_roles),
    }


def _make_summary(
    ctx: _Context, plan: ExperimentPlan, used: list[str],
    unit_resolution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    s: dict[str, Any] = {
        "dsl_version": plan.dsl_version,
        "name": plan.name,
        "step_count_dsl": len(plan.steps),
        "step_count_expanded": ctx.total_expanded_steps,
        "rendered_step_count": len(ctx.rendered_steps),
        "required_resources": used,                 # backward compat (== used_resources)
        "used_resources": used,                     # v0.8.1: 明示
        "resolved_instruments": dict(ctx.resolved),
        "estimated_duration_s": round(ctx.estimated_duration_s, 2),
        "uses_verify": ctx.uses_verify_count > 0,
        "uses_verify_count": ctx.uses_verify_count,
        "uses_polling": ctx.uses_polling,
        "has_safe_shutdown": ctx.has_safe_shutdown,
        "safe_shutdown_scope": (
            "explicit" if ctx.safe_shutdown_targets else
            ("all_used_resources" if ctx.has_safe_shutdown else "none")
        ),
        "safe_shutdown_targets": (
            ctx.safe_shutdown_targets if ctx.safe_shutdown_targets
            else (used if ctx.has_safe_shutdown else None)
        ),
        "has_parallel": len(ctx.parallel_groups) > 0,
        "parallel_group_count": len(ctx.parallel_groups),
    }
    # v0.8.3: unit_resolution を summary に同梱 (None でなければ)
    if unit_resolution is not None:
        s["unit_resolution"] = unit_resolution
    return s
