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

# 予約 env 名 (実行時に VariableStore が供給する)。SP-3 で loop_index を追加予定。
_ENV_NAMES = frozenset({"job_id", "started_at"})


def _validate_expr_refs(
    expr: str,
    *,
    defined_steps: set[str],
    defined_vars: set[str],
    param_names: set[str],
    context: str,
) -> None:
    """compute / ${...} の式をコンパイル時検証する。

    - 構文 + AST ホワイトリスト (check_expr)
    - 参照名がその時点までに定義される名前に含まれること (前方参照禁止)
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
        "env": _ENV_NAMES,
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
    """
    plan_steps: list[Step] = []
    aux_resources: set[str] = set()

    # コンパイル時検証用: その時点までに定義される名前を追跡する
    param_names: set[str] = set(variables.keys()) | {p.name for p in recipe.parameters}
    defined_steps: set[str] = set()
    defined_vars: set[str] = set()

    for rs in recipe.steps:
        st = rs.step_type
        if st == "compute":
            cp = rs.compute or {}
            set_name = cp["set"]
            expr = cp["expr"]
            _validate_expr_refs(
                expr,
                defined_steps=defined_steps, defined_vars=defined_vars,
                param_names=param_names, context=f"compute(set={set_name})",
            )
            plan_steps.append(ComputeStep(
                set=set_name,
                expr=expr,
                unit=cp.get("unit", ""),
                on_error=cp.get("on_error", "abort"),
                description=rs.description,
            ))
            defined_vars.add(set_name)
        elif st == "wait":
            seconds_raw = rs.wait["seconds"]
            seconds = float(resolve_arg(seconds_raw, variables))
            plan_steps.append(WaitStep(
                seconds=seconds,
                description=rs.description,
            ))
        elif st == "wait_until":
            wu = dict(rs.wait_until)
            sec = wu.get("seconds_from_now")
            if isinstance(sec, str):
                sec = float(resolve_arg(sec, variables))
                wu["seconds_from_now"] = sec
            plan_steps.append(WaitUntilStep(
                timestamp=wu.get("timestamp"),
                seconds_from_now=wu.get("seconds_from_now"),
                description=rs.description,
            ))
        elif st == "wait_for_condition":
            wfc = dict(rs.wait_for_condition)
            resolved_args = {
                k: resolve_arg(v, variables) for k, v in (wfc.get("args") or {}).items()
            }
            inst = wfc["instrument"]
            aux_resources.add(inst)
            plan_steps.append(WaitForConditionStep(
                instrument=inst,
                command=wfc["command"],
                args=resolved_args,
                condition_expr=wfc["condition_expr"],
                interval_s=float(resolve_arg(wfc.get("interval_s", 1.0), variables)),
                timeout_s=float(resolve_arg(wfc.get("timeout_s", 60.0), variables)),
                command_timeout_s=(
                    float(resolve_arg(wfc["command_timeout_s"], variables))
                    if wfc.get("command_timeout_s") is not None else None
                ),
                value_path=wfc.get("value_path"),
                retry_on_error=int(wfc.get("retry_on_error", 1)),
                max_consecutive_errors=int(wfc.get("max_consecutive_errors", 3)),
                description=rs.description,
            ))
        elif st == "wait_for_stable":
            wfs = dict(rs.wait_for_stable)
            resolved_args = {
                k: resolve_arg(v, variables) for k, v in (wfs.get("args") or {}).items()
            }
            inst = wfs["instrument"]
            aux_resources.add(inst)
            plan_steps.append(WaitForStableStep(
                instrument=inst,
                command=wfs["command"],
                args=resolved_args,
                tolerance=float(resolve_arg(wfs["tolerance"], variables)),
                window_s=float(resolve_arg(wfs["window_s"], variables)),
                interval_s=float(resolve_arg(wfs.get("interval_s", 1.0), variables)),
                timeout_s=float(resolve_arg(wfs.get("timeout_s", 60.0), variables)),
                command_timeout_s=(
                    float(resolve_arg(wfs["command_timeout_s"], variables))
                    if wfs.get("command_timeout_s") is not None else None
                ),
                value_path=wfs.get("value_path"),
                min_samples=int(wfs.get("min_samples", 3)),
                method=wfs.get("method", "range"),
                retry_on_error=int(wfs.get("retry_on_error", 1)),
                max_consecutive_errors=int(wfs.get("max_consecutive_errors", 3)),
                description=rs.description,
            ))
        elif st == "barrier":
            br = dict(rs.barrier)
            plan_steps.append(BarrierStep(
                name=br["name"],
                timeout_s=float(resolve_arg(br.get("timeout_s", 60.0), variables)),
                description=rs.description,
            ))
        else:  # command
            resolved_args: dict[str, Any] = {}
            deferred_args: dict[str, Any] = {}
            for k, v in rs.args.items():
                expr = parse_deferred(v)  # ${...} 検出 (SP-2)
                if expr is not None:
                    _validate_expr_refs(
                        expr,
                        defined_steps=defined_steps, defined_vars=defined_vars,
                        param_names=param_names,
                        context=f"{rs.command}.{k} (${{...}})",
                    )
                    mn, mx = _resolve_range_decl(
                        recipe, definition, rs.command or "", k,
                    )
                    deferred_args[k] = {"expr": expr, "min": mn, "max": mx}
                else:
                    resolved_args[k] = resolve_arg(v, variables)
            # v0.6.0: instrument は logical ref ($psu / alias / resource_name) としてそのまま渡す。
            # 実 resource への解決は Job executor / step_executor 側で行う。
            plan_steps.append(CommandStep(
                command=rs.command or "",
                args=resolved_args,
                deferred_args=deferred_args,
                result_as=rs.result_as,
                value_path=rs.value_path or "",
                unit=rs.unit or "",
                description=rs.description,
                instrument=getattr(rs, "instrument", None),
                stagger_ms=getattr(rs, "stagger_ms", None),
            ))
            if rs.result_as:
                defined_steps.add(rs.result_as)

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

    for idx, step in enumerate(plan.steps):
        if isinstance(step, WaitStep):
            result = await execute_wait_step(step)
        elif isinstance(step, ComputeStep):
            result = await seq_runtime.process_compute_step(
                step, store,
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
