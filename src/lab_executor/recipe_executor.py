"""
Recipe 実行エンジン (v0.5.0-rc1 で IR ベースに refactor)

設計:
- YAML の `RecipeDefinition` を内部 IR (`experiment_ir.Plan`) に変換
- Plan を `execute_plan()` が walk して実行
- v0.5.0-rc1: CommandStep (従来の機器コマンド) + WaitStep (asyncio.sleep) のみ
- v0.5.1 以降で wait_for_* 系 step が追加されても execute_plan のディスパッチを増やすだけ

外部 API (`execute_recipe`) の戻り値形式は v0.3.0 までと互換性を維持:
- `{"success": bool, "recipe": str, "steps_executed": [...], "step_count": N}`
- 失敗時は `{"success": False, ..., "halted_at_step": idx}`

新しい標準 envelope (`response_envelope.make_envelope`) は v0.5.0+ で新規追加される
MCP ツール (Job 系等) で採用する。既存 `execute_recipe` ツールは後方互換のため従来形式。
"""
from __future__ import annotations
import asyncio
import logging
from typing import Any, TYPE_CHECKING

from .experiment_ir import (
    CommandStep, ComputeStep, Plan, Step, WaitStep,
    WaitUntilStep, WaitForConditionStep, WaitForStableStep,
    BarrierStep, VariableStore,
    GuardStep, BranchCase, BranchStep, RepeatStep,
)
from .models.instrument_def import InstrumentDefinition, RecipeDefinition, RecipeStep
from .step_executor import execute_command_step, execute_wait_step
from .utils.expression import resolve_arg, ExpressionError
from .utils.seq_expression import (
    SeqExpressionError, check_expr, parse_deferred, referenced_names,
)
from . import seq_runtime


# v2.0: backend layer は visa-mcp / shim 経由。型ヒント目的
# は TYPE_CHECKING、VisaError は ImportError fallback。
if TYPE_CHECKING:
    from visa_mcp.session_manager import InstrumentSession
    from visa_mcp.visa_manager import VisaManager

try:
    from visa_mcp.visa_manager import VisaError
except ImportError:
    class VisaError(Exception):  # type: ignore[no-redef]
        """visa-mcp 不在時の VisaError 代替"""
        pass

logger = logging.getLogger(__name__)


# ============================================================
# Recipe → IR Plan 変換
# ============================================================

# 予約 env 名 (実行時に VariableStore が供給する)。
# loop_index は repeat body 内でのみ利用可 (SP-3)。
_ENV_NAMES = frozenset({"job_id", "started_at"})

# v2.29.0 (SP-3): 展開・ネスト上限 (spec §10「レビュー可能性の上限」)。
# 値は docs/sequence_processing.md に記載。
BRANCH_MAX_DEPTH = 3            # branch のネスト最大深さ
REPEAT_MAX_COUNT = 10_000       # repeat count / max_iterations の上限
MAX_TOTAL_STEPS_ESTIMATE = 100_000  # 展開後総ステップ数の静的見積り上限


def _validate_expr_refs(
    expr: str,
    *,
    defined_steps: set[str],
    defined_vars: set[str],
    param_names: set[str],
    context: str,
    env_names: frozenset[str] | set[str] = _ENV_NAMES,
) -> None:
    """compute / ${...} / branch when / guard / repeat while の式をコンパイル時検証する。

    - 構文 + AST ホワイトリスト (check_expr)
    - 参照名がその時点までに定義される名前に含まれること (前方参照禁止)
    - env_names: その文脈で参照可能な env 予約名 (repeat body 内は + loop_index)
    """
    try:
        check_expr(expr)
        refs = referenced_names(expr)
    except SeqExpressionError as e:
        raise SeqExpressionError(f"{context}: {e}")

    tables = {
        "params": param_names,
        "steps": defined_steps,
        "vars": defined_vars,
        "env": set(env_names),
    }
    for ref in refs:
        if "." in ref:
            ns, name = ref.split(".", 1)
            table = tables.get(ns)
            if table is None or name not in table:
                raise SeqExpressionError(
                    f"{context}: 未定義または前方参照の変数 '{ref}'"
                )
        else:
            # 裸名: params / 先行 steps / 先行 vars のいずれかに存在する必要がある
            if (
                ref not in param_names
                and ref not in defined_steps
                and ref not in defined_vars
            ):
                raise SeqExpressionError(
                    f"{context}: 未定義または前方参照の変数 '{ref}'"
                )


def _resolve_range_decl(
    recipe: RecipeDefinition,
    definition: InstrumentDefinition | None,
    command: str,
    arg: str,
) -> tuple[float | None, float | None]:
    """実行時解決 arg の範囲宣言を集約する (安全要件の核, spec §6)。

    ParameterDefinition.range と recipe.requires.ranges の両方を見て、
    どちらも無ければ検証エラー。両方あれば厳しい方 (積集合) を返す。
    """
    mins: list[float] = []
    maxs: list[float] = []
    declared = False

    ranges_decl = (recipe.requires.ranges if recipe.requires else {}) or {}
    rspec = ranges_decl.get(f"{command}.{arg}")
    if rspec is not None:
        declared = True
        if rspec.min is not None:
            mins.append(float(rspec.min))
        if rspec.max is not None:
            maxs.append(float(rspec.max))

    if definition is not None:
        cmd_def = definition.commands.get(command)
        if cmd_def is not None:
            for pdef in cmd_def.parameters:
                if pdef.name == arg and pdef.range:
                    declared = True
                    mins.append(float(pdef.range[0]))
                    maxs.append(float(pdef.range[1]))
                    break

    if not declared:
        raise SeqExpressionError(
            f"実行時解決引数 '{command}.{arg}' に範囲宣言がありません。"
            "ParameterDefinition.range または recipe.requires.ranges "
            f"('{command}.{arg}') のいずれかが必須です (安全要件 §6)"
        )
    return (max(mins) if mins else None, min(maxs) if maxs else None)


def recipe_to_plan(
    recipe: RecipeDefinition,
    variables: dict[str, Any],
    *,
    primary_resource: str | None = None,
    definition: InstrumentDefinition | None = None,
) -> Plan:
    """
    YAML の RecipeDefinition + 変数辞書 → IR Plan に変換する。
    args 内の `$var` / `$var * 1.1` 等の式は事前に評価して具体値にする。
    `${steps.x}` 形式は実行時解決 (deferred) として保持する (SP-2)。

    primary_resource を渡すと Plan.required_resources の起点となる。
    polling 系 step が別 instrument を参照する場合、そのリソースも required_resources に追加される。
    canonical sorted 順で deduplicate。

    definition (機器定義) は ${...} の範囲宣言検証 (ParameterDefinition.range)
    に使う。deferred arg があるのに definition が無く requires.ranges にも
    宣言が無い場合は SeqExpressionError。

    v2.29.0 (SP-3): branch / repeat / guard をネスト構造を保った IR
    (BranchStep.cases[].steps / RepeatStep.body が Step のリスト) に変換する。
    ネスト内では全経路定義検証・branch 深さ上限・展開見積り上限を執行する。
    """
    aux_resources: set[str] = set()

    # コンパイル時検証用: その時点までに定義される名前を追跡する
    param_names: set[str] = set(variables.keys()) | {p.name for p in recipe.parameters}

    conv = _StepConverter(
        recipe=recipe, variables=variables, definition=definition,
        aux_resources=aux_resources, param_names=param_names,
    )
    plan_steps, estimate = conv.convert(
        recipe.steps,
        defined_steps=set(), defined_vars=set(),
        env_names=set(_ENV_NAMES), branch_depth=0, nested=False,
        path="steps",
    )
    if estimate > MAX_TOTAL_STEPS_ESTIMATE:
        raise SeqExpressionError(
            f"展開後の総ステップ数見積り ({estimate}) が上限 "
            f"({MAX_TOTAL_STEPS_ESTIMATE}) を超えています (spec §10 展開上限)"
        )

    # required_resources: primary + aux を canonical sorted
    req: set[str] = set(aux_resources)
    if primary_resource:
        req.add(primary_resource)
    required = sorted(req)

    return Plan(
        name=(recipe.description[:80] if recipe.description else "recipe"),
        parameters=dict(variables),
        steps=plan_steps,
        resource_hint=primary_resource,
        required_resources=required,
    )


class _StepConverter:
    """RecipeStep 列 → IR Step 列の再帰変換器 (v2.29.0 SP-3 で導入)。

    branch / repeat のネスト steps (raw dict) を RecipeStep として検証しつつ
    再帰変換する。defined_steps / defined_vars は呼び出し側の set を直接
    mutate する (branch case は copy を渡して分岐間の漏れを防ぐ)。
    返り値は (steps, 展開後ステップ数の静的見積り)。
    """

    # ネスト (branch case / repeat body) 内で許可しない step 種別。
    # polling / barrier は Job manager のトップレベル状態遷移 (WAITING 等)
    # と密結合のため SP-3 では未対応 (逸脱として docs に記載)。
    _NESTED_FORBIDDEN = frozenset({
        "wait_until", "wait_for_condition", "wait_for_stable", "barrier",
    })

    def __init__(
        self,
        *,
        recipe: RecipeDefinition,
        variables: dict[str, Any],
        definition: InstrumentDefinition | None,
        aux_resources: set[str],
        param_names: set[str],
    ):
        self.recipe = recipe
        self.variables = variables
        self.definition = definition
        self.aux_resources = aux_resources
        self.param_names = param_names

    def convert(
        self,
        raw_steps: list,
        *,
        defined_steps: set[str],
        defined_vars: set[str],
        env_names: set[str],
        branch_depth: int,
        nested: bool,
        path: str,
    ) -> tuple[list[Step], int]:
        out: list[Step] = []
        estimate = 0

        for i, raw in enumerate(raw_steps):
            spath = f"{path}[{i}]"
            rs = self._as_recipe_step(raw, spath)
            st = rs.step_type

            if nested and st in self._NESTED_FORBIDDEN:
                raise SeqExpressionError(
                    f"{spath}: branch / repeat 内の {st} は SP-3 では未対応です"
                )

            if st == "compute":
                out.append(self._convert_compute(
                    rs, defined_steps, defined_vars, env_names, spath,
                ))
                defined_vars.add(rs.compute["set"])
                estimate += 1
            elif st == "guard":
                out.append(self._convert_guard(
                    rs, defined_steps, defined_vars, env_names, spath,
                ))
                estimate += 1
            elif st == "branch":
                step, est = self._convert_branch(
                    rs, defined_steps, defined_vars, env_names,
                    branch_depth, spath,
                )
                out.append(step)
                estimate += est
            elif st == "repeat":
                step, est = self._convert_repeat(
                    rs, defined_steps, defined_vars, env_names,
                    branch_depth, spath,
                )
                out.append(step)
                estimate += est
            elif st == "wait":
                seconds_raw = rs.wait["seconds"]
                seconds = float(resolve_arg(seconds_raw, self.variables))
                out.append(WaitStep(seconds=seconds, description=rs.description))
                estimate += 1
            elif st == "wait_until":
                wu = dict(rs.wait_until)
                sec = wu.get("seconds_from_now")
                if isinstance(sec, str):
                    sec = float(resolve_arg(sec, self.variables))
                    wu["seconds_from_now"] = sec
                out.append(WaitUntilStep(
                    timestamp=wu.get("timestamp"),
                    seconds_from_now=wu.get("seconds_from_now"),
                    description=rs.description,
                ))
                estimate += 1
            elif st == "wait_for_condition":
                wfc = dict(rs.wait_for_condition)
                resolved_args = {
                    k: resolve_arg(v, self.variables)
                    for k, v in (wfc.get("args") or {}).items()
                }
                inst = wfc["instrument"]
                self.aux_resources.add(inst)
                out.append(WaitForConditionStep(
                    instrument=inst,
                    command=wfc["command"],
                    args=resolved_args,
                    condition_expr=wfc["condition_expr"],
                    interval_s=float(resolve_arg(wfc.get("interval_s", 1.0), self.variables)),
                    timeout_s=float(resolve_arg(wfc.get("timeout_s", 60.0), self.variables)),
                    command_timeout_s=(
                        float(resolve_arg(wfc["command_timeout_s"], self.variables))
                        if wfc.get("command_timeout_s") is not None else None
                    ),
                    value_path=wfc.get("value_path"),
                    retry_on_error=int(wfc.get("retry_on_error", 1)),
                    max_consecutive_errors=int(wfc.get("max_consecutive_errors", 3)),
                    description=rs.description,
                ))
                estimate += 1
            elif st == "wait_for_stable":
                wfs = dict(rs.wait_for_stable)
                resolved_args = {
                    k: resolve_arg(v, self.variables)
                    for k, v in (wfs.get("args") or {}).items()
                }
                inst = wfs["instrument"]
                self.aux_resources.add(inst)
                out.append(WaitForStableStep(
                    instrument=inst,
                    command=wfs["command"],
                    args=resolved_args,
                    tolerance=float(resolve_arg(wfs["tolerance"], self.variables)),
                    window_s=float(resolve_arg(wfs["window_s"], self.variables)),
                    interval_s=float(resolve_arg(wfs.get("interval_s", 1.0), self.variables)),
                    timeout_s=float(resolve_arg(wfs.get("timeout_s", 60.0), self.variables)),
                    command_timeout_s=(
                        float(resolve_arg(wfs["command_timeout_s"], self.variables))
                        if wfs.get("command_timeout_s") is not None else None
                    ),
                    value_path=wfs.get("value_path"),
                    min_samples=int(wfs.get("min_samples", 3)),
                    method=wfs.get("method", "range"),
                    retry_on_error=int(wfs.get("retry_on_error", 1)),
                    max_consecutive_errors=int(wfs.get("max_consecutive_errors", 3)),
                    description=rs.description,
                ))
                estimate += 1
            elif st == "barrier":
                br = dict(rs.barrier)
                out.append(BarrierStep(
                    name=br["name"],
                    timeout_s=float(resolve_arg(br.get("timeout_s", 60.0), self.variables)),
                    description=rs.description,
                ))
                estimate += 1
            else:  # command
                out.append(self._convert_command(
                    rs, defined_steps, defined_vars, env_names, spath,
                ))
                if rs.result_as:
                    defined_steps.add(rs.result_as)
                estimate += 1

        return out, estimate

    # ---- 個別変換 ----

    @staticmethod
    def _as_recipe_step(raw, spath: str) -> RecipeStep:
        if isinstance(raw, RecipeStep):
            return raw
        try:
            return RecipeStep.model_validate(raw)
        except Exception as e:  # pydantic ValidationError 等
            raise SeqExpressionError(f"{spath}: ステップ定義が不正です: {e}")

    def _convert_compute(
        self, rs: RecipeStep,
        defined_steps: set[str], defined_vars: set[str],
        env_names: set[str], spath: str,
    ) -> ComputeStep:
        cp = rs.compute or {}
        set_name = cp["set"]
        expr = cp["expr"]
        _validate_expr_refs(
            expr,
            defined_steps=defined_steps, defined_vars=defined_vars,
            param_names=self.param_names, env_names=env_names,
            context=f"{spath} compute(set={set_name})",
        )
        return ComputeStep(
            set=set_name,
            expr=expr,
            unit=cp.get("unit", ""),
            on_error=cp.get("on_error", "abort"),
            description=rs.description,
        )

    def _convert_guard(
        self, rs: RecipeStep,
        defined_steps: set[str], defined_vars: set[str],
        env_names: set[str], spath: str,
    ) -> GuardStep:
        gd = rs.guard or {}
        _validate_expr_refs(
            gd["expr"],
            defined_steps=defined_steps, defined_vars=defined_vars,
            param_names=self.param_names, env_names=env_names,
            context=f"{spath} guard",
        )
        return GuardStep(
            expr=gd["expr"],
            on_fail=gd.get("on_fail", "abort"),
            message=gd.get("message", ""),
            description=rs.description,
        )

    def _convert_branch(
        self, rs: RecipeStep,
        defined_steps: set[str], defined_vars: set[str],
        env_names: set[str], branch_depth: int, spath: str,
    ) -> tuple[BranchStep, int]:
        if branch_depth + 1 > BRANCH_MAX_DEPTH:
            raise SeqExpressionError(
                f"{spath}: branch のネスト深さが上限 ({BRANCH_MAX_DEPTH}) を超えています"
            )
        cases: list[BranchCase] = []
        new_defs: list[tuple[set[str], set[str]]] = []
        case_estimates: list[int] = []
        has_else = False

        for ci, case in enumerate(rs.branch or []):
            is_else = "else" in case
            when = None if is_else else case.get("when")
            if is_else:
                has_else = True
            if when is not None:
                _validate_expr_refs(
                    when,
                    defined_steps=defined_steps, defined_vars=defined_vars,
                    param_names=self.param_names, env_names=env_names,
                    context=f"{spath}/branch case[{ci}].when",
                )
            # 各 case は defined set の copy を使う (分岐間の定義漏れ防止)
            ds = set(defined_steps)
            dv = set(defined_vars)
            sub_steps, est = self.convert(
                case["steps"],
                defined_steps=ds, defined_vars=dv, env_names=env_names,
                branch_depth=branch_depth + 1, nested=True,
                path=f"{spath}/case[{ci}]/steps",
            )
            cases.append(BranchCase(when=when, steps=sub_steps))
            new_defs.append((ds - defined_steps, dv - defined_vars))
            case_estimates.append(est)

        # 全経路定義検証 (spec §3): else がある場合のみ、**全 case で定義された**
        # 名前を分岐後スコープへ伝播する。else が無い場合は「どの case も実行され
        # ない」経路があるため何も伝播しない → 分岐後にその変数を参照すると
        # 未定義参照としてコンパイルエラーになる。
        if has_else and new_defs:
            common_steps = set.intersection(*[d[0] for d in new_defs])
            common_vars = set.intersection(*[d[1] for d in new_defs])
            defined_steps |= common_steps
            defined_vars |= common_vars

        step = BranchStep(cases=cases, description=rs.description)
        return step, 1 + (max(case_estimates) if case_estimates else 0)

    def _convert_repeat(
        self, rs: RecipeStep,
        defined_steps: set[str], defined_vars: set[str],
        env_names: set[str], branch_depth: int, spath: str,
    ) -> tuple[RepeatStep, int]:
        rp = rs.repeat or {}
        body_env = set(env_names) | {"loop_index"}

        if rp.get("count") is not None:
            # count はコンパイル時解決 ($param 可)
            try:
                count = int(float(resolve_arg(rp["count"], self.variables)))
            except (ExpressionError, TypeError, ValueError) as e:
                raise SeqExpressionError(f"{spath}: repeat.count を解決できません: {e}")
            if count < 1:
                raise SeqExpressionError(
                    f"{spath}: repeat.count は 1 以上である必要があります: {count}"
                )
            if count > REPEAT_MAX_COUNT:
                raise SeqExpressionError(
                    f"{spath}: repeat.count ({count}) が上限 ({REPEAT_MAX_COUNT}) を"
                    "超えています"
                )
            # count >= 1 が保証されるため body 内の定義は分岐後スコープへ伝播する
            body_steps, est = self.convert(
                rp["steps"],
                defined_steps=defined_steps, defined_vars=defined_vars,
                env_names=body_env,
                branch_depth=branch_depth, nested=True,
                path=f"{spath}/repeat/steps",
            )
            step = RepeatStep(count=count, body=body_steps, description=rs.description)
            return step, 1 + count * est

        # while 型
        while_expr = rp["while"]
        try:
            max_it = int(rp["max_iterations"])
        except (TypeError, ValueError) as e:
            raise SeqExpressionError(f"{spath}: repeat.max_iterations が不正です: {e}")
        if max_it < 1:
            raise SeqExpressionError(
                f"{spath}: repeat.max_iterations は 1 以上である必要があります: {max_it}"
            )
        if max_it > REPEAT_MAX_COUNT:
            raise SeqExpressionError(
                f"{spath}: repeat.max_iterations ({max_it}) が上限 "
                f"({REPEAT_MAX_COUNT}) を超えています"
            )
        # while 条件は最初の反復の**前**に評価されるため、body 内でのみ定義される
        # 変数は参照できない (外側の定義のみで検証。loop_index も不可)
        _validate_expr_refs(
            while_expr,
            defined_steps=defined_steps, defined_vars=defined_vars,
            param_names=self.param_names, env_names=env_names,
            context=f"{spath}/repeat.while",
        )
        # while は 0 回実行があり得るため body 内の定義は伝播しない (copy で検証)
        ds = set(defined_steps)
        dv = set(defined_vars)
        body_steps, est = self.convert(
            rp["steps"],
            defined_steps=ds, defined_vars=dv, env_names=body_env,
            branch_depth=branch_depth, nested=True,
            path=f"{spath}/repeat/steps",
        )
        step = RepeatStep(
            while_expr=while_expr, max_iterations=max_it,
            body=body_steps, description=rs.description,
        )
        return step, 1 + max_it * est

    def _convert_command(
        self, rs: RecipeStep,
        defined_steps: set[str], defined_vars: set[str],
        env_names: set[str], spath: str,
    ) -> CommandStep:
        resolved_args: dict[str, Any] = {}
        deferred_args: dict[str, Any] = {}
        for k, v in rs.args.items():
            expr = parse_deferred(v)  # ${...} 検出 (SP-2)
            if expr is not None:
                _validate_expr_refs(
                    expr,
                    defined_steps=defined_steps, defined_vars=defined_vars,
                    param_names=self.param_names, env_names=env_names,
                    context=f"{spath} {rs.command}.{k} (${{...}})",
                )
                mn, mx = _resolve_range_decl(
                    self.recipe, self.definition, rs.command or "", k,
                )
                deferred_args[k] = {"expr": expr, "min": mn, "max": mx}
            else:
                resolved_args[k] = resolve_arg(v, self.variables)
        # v0.6.0: instrument は logical ref ($psu / alias / resource_name) としてそのまま渡す。
        # 実 resource への解決は Job executor / step_executor 側で行う。
        return CommandStep(
            command=rs.command or "",
            args=resolved_args,
            deferred_args=deferred_args,
            result_as=rs.result_as,
            value_path=rs.value_path or "",
            unit=rs.unit or "",
            description=rs.description,
            instrument=getattr(rs, "instrument", None),
            stagger_ms=getattr(rs, "stagger_ms", None),
        )


# ============================================================
# Plan executor (各 Step type を dispatch)
# ============================================================

async def execute_plan(
    visa: VisaManager,
    session: InstrumentSession,
    plan: Plan,
    recipe_name: str | None = None,
    override_safety: bool = False,
    override_reason: str = "",
) -> dict:
    """
    IR Plan を実行する。返り値の形式は execute_recipe と同じ (後方互換)。
    """
    if session.definition is None:
        return {
            "success": False,
            "recipe": recipe_name or plan.name,
            "error": "NoDefinitionFound",
            "message": "機器定義が読み込まれていません",
            "steps_executed": [],
        }

    step_results: list[dict] = []

    # v0.5.1.1 / v0.6.1: polling / barrier 系 step は同期 execute_recipe では実行不可。
    # LLM が誤って execute_recipe を選んだ場合に分かりやすく Job 化を促す。
    # barrier は Map Job 内の target 間同期なので、単一 target の execute_recipe では
    # 永遠に成立しない (1 つの target だけが arrive して全 target 揃わない)。
    for s in plan.steps:
        if isinstance(s, (WaitUntilStep, WaitForConditionStep, WaitForStableStep, BarrierStep)):
            is_barrier = isinstance(s, BarrierStep)
            return {
                "success": False,
                "recipe": recipe_name or plan.name,
                "error": "AsyncStepRequiresJob",
                "message": (
                    "wait_until / wait_for_condition / wait_for_stable / barrier を含む recipe は "
                    "execute_recipe では実行できません。"
                    + ("**start_map_recipe_job** を使ってください "
                       "(barrier は target 間同期のため Map Job で意味を持ちます)。"
                       if is_barrier else
                       "**start_recipe_job** を使ってください。")
                    + " (進捗は get_job_status、完了結果は get_job_result で取得)"
                ),
                "async_step_type": getattr(s, "type", "?"),
                "recommended_action": {
                    "tool": "start_map_recipe_job" if is_barrier else "start_recipe_job",
                    "args": (
                        {"recipe": recipe_name or plan.name, "targets": "<target list>"}
                        if is_barrier else
                        {"resource_name": "<同じ resource>",
                         "recipe_name": recipe_name or plan.name}
                    ),
                },
                "steps_executed": [],
            }

    # v2.28.0 (SP-1/2): 変数ストア (params / env)。同期経路では job_id は "" とする。
    import datetime as _dt
    store = VariableStore(
        params=dict(plan.parameters),
        env={"job_id": "", "started_at": _dt.datetime.now().isoformat()},
    )

    async def _safe_shutdown() -> dict:
        return await _run_safe_shutdown_sync(visa, session)

    # v2.29.0 (SP-3): branch / repeat 内のリーフ step 実行コールバック (同期経路)
    async def _nested_run_command(st: CommandStep, path: str) -> dict:
        return await seq_runtime.process_command_step(
            visa, session, st, store,
            override_safety=override_safety,
            override_reason=override_reason,
            source_step_path=path,
            safe_shutdown=_safe_shutdown,
        )

    async def _nested_run_wait(st: WaitStep, path: str) -> dict:
        return await execute_wait_step(st)

    nested_execs = seq_runtime.NestedExecutors(
        run_command=_nested_run_command,
        run_wait=_nested_run_wait,
        cancel_check=None,
    )

    for idx, step in enumerate(plan.steps):
        if isinstance(step, WaitStep):
            result = await execute_wait_step(step)
        elif isinstance(step, ComputeStep):
            result = await seq_runtime.process_compute_step(
                step, store,
                source_step_path=f"steps[{idx}]",
                safe_shutdown=_safe_shutdown,
            )
        elif isinstance(step, GuardStep):
            result = await seq_runtime.process_guard_step(
                step, store,
                source_step_path=f"steps[{idx}]",
                safe_shutdown=_safe_shutdown,
            )
        elif isinstance(step, BranchStep):
            result = await seq_runtime.process_branch_step(
                step, store, nested_execs,
                source_step_path=f"steps[{idx}]",
                safe_shutdown=_safe_shutdown,
            )
        elif isinstance(step, RepeatStep):
            result = await seq_runtime.process_repeat_step(
                step, store, nested_execs,
                source_step_path=f"steps[{idx}]",
                safe_shutdown=_safe_shutdown,
            )
        elif isinstance(step, CommandStep):
            result = await seq_runtime.process_command_step(
                visa, session, step, store,
                override_safety=override_safety,
                override_reason=override_reason,
                source_step_path=f"steps[{idx}]",
                safe_shutdown=_safe_shutdown,
            )
        else:
            # 将来 step type 追加時に備えた fallback
            result = {
                "success": False,
                "error": "UnsupportedStepType",
                "step_type": getattr(step, "type", "unknown"),
                "message": "未対応のステップ型です",
            }

        step_results.append({"step": idx, **result})

        if not result.get("success", False):
            return {
                "success": False,
                "recipe": recipe_name or plan.name,
                "steps_executed": step_results,
                "halted_at_step": idx,
                "variables": store.snapshot(),
            }

    return {
        "success": True,
        "recipe": recipe_name or plan.name,
        "steps_executed": step_results,
        "step_count": len(step_results),
        "variables": store.snapshot(),
    }


async def _run_safe_shutdown_sync(visa: VisaManager, session: InstrumentSession) -> dict:
    """同期経路 (execute_plan) 用の簡易 safe_shutdown。

    機器定義の ``safe_shutdown`` に定義された command ステップを override_safety=True
    で順次実行する (wait は同期経路ではスキップ)。Job 経路は manager 側の
    ``_best_effort_safe_shutdown`` を使う。
    """
    steps = list(getattr(session.definition, "safe_shutdown", None) or [])
    results: list[dict] = []
    all_ok = True
    for idx, rs in enumerate(steps):
        if getattr(rs, "step_type", "command") != "command":
            continue
        try:
            cs = CommandStep(command=rs.command or "", args=rs.args)
            r = await execute_command_step(
                visa, session, cs,
                override_safety=True, override_reason="safe_shutdown (sync)",
            )
            ok = bool(r.get("success"))
            all_ok = all_ok and ok
            results.append({"step": idx, "command": rs.command, "success": ok})
        except Exception as e:  # noqa: BLE001
            all_ok = False
            results.append({
                "step": idx, "command": rs.command,
                "success": False, "error": type(e).__name__,
            })
    return {
        "attempted": bool(steps),
        "source": "yaml" if steps else "none",
        "success": all_ok,
        "steps": results,
    }


# ============================================================
# 公開エントリポイント (既存 API、後方互換維持)
# ============================================================
# 個別 step 実行ロジックは v0.5.0.1 で step_executor.py に切り出し済み。
# このモジュールは Recipe 単位の orchestration のみを担当する。

async def execute_recipe(
    visa: VisaManager,
    session: InstrumentSession,
    recipe_name: str,
    parameters: dict[str, Any] | None,
    override_safety: bool = False,
    override_reason: str = "",
) -> dict:
    """
    指定の recipe を実行する。

    v0.5.0-rc1 で内部実装を IR Plan ベースに refactor したが、戻り値形式は v0.3.0/v0.4.x と同一。
    """
    parameters = parameters or {}

    if session.definition is None:
        return {
            "success": False,
            "error": "NoDefinitionFound",
            "message": "機器定義が読み込まれていません",
        }

    recipe: RecipeDefinition | None = session.definition.recipes.get(recipe_name)
    if recipe is None:
        return {
            "success": False,
            "error": "RecipeNotFound",
            "message": f"recipe '{recipe_name}' は定義されていません",
            "available_recipes": list(session.definition.recipes.keys()),
        }

    # パラメータ検証 (簡易: 必須チェックのみ)
    for p in recipe.parameters:
        if p.required and p.name not in parameters and p.default is None:
            return {
                "success": False,
                "error": "MissingParameter",
                "message": f"必須パラメータ '{p.name}' が指定されていません",
            }
    # default 適用
    variables = dict(parameters)
    for p in recipe.parameters:
        if p.name not in variables and p.default is not None:
            variables[p.name] = p.default

    # Recipe → IR Plan 変換 (definition を渡し ${...} の範囲宣言検証を有効化)
    try:
        plan = recipe_to_plan(recipe, variables, definition=session.definition)
    except (ExpressionError, SeqExpressionError) as e:
        return {
            "success": False,
            "recipe": recipe_name,
            "error": "ExpressionError",
            "message": str(e),
            "steps_executed": [],
        }

    # Plan 実行
    return await execute_plan(
        visa, session, plan,
        recipe_name=recipe_name,
        override_safety=override_safety,
        override_reason=override_reason,
    )
