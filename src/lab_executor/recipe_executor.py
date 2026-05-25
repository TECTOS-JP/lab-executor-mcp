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
    CommandStep, Plan, Step, WaitStep,
    WaitUntilStep, WaitForConditionStep, WaitForStableStep,
    BarrierStep,
)
from .models.instrument_def import InstrumentDefinition, RecipeDefinition, RecipeStep
from .step_executor import execute_command_step, execute_wait_step
from .utils.expression import resolve_arg, ExpressionError


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

def recipe_to_plan(
    recipe: RecipeDefinition,
    variables: dict[str, Any],
    *,
    primary_resource: str | None = None,
) -> Plan:
    """
    YAML の RecipeDefinition + 変数辞書 → IR Plan に変換する。
    args 内の `$var` / `$var * 1.1` 等の式は事前に評価して具体値にする。

    primary_resource を渡すと Plan.required_resources の起点となる。
    polling 系 step が別 instrument を参照する場合、そのリソースも required_resources に追加される。
    canonical sorted 順で deduplicate。
    """
    plan_steps: list[Step] = []
    aux_resources: set[str] = set()

    for rs in recipe.steps:
        st = rs.step_type
        if st == "wait":
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
            resolved_args = {k: resolve_arg(v, variables) for k, v in rs.args.items()}
            # v0.6.0: instrument は logical ref ($psu / alias / resource_name) としてそのまま渡す。
            # 実 resource への解決は Job executor / step_executor 側で行う。
            plan_steps.append(CommandStep(
                command=rs.command or "",
                args=resolved_args,
                result_as=rs.result_as,
                description=rs.description,
                instrument=getattr(rs, "instrument", None),
                stagger_ms=getattr(rs, "stagger_ms", None),
            ))

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

    for idx, step in enumerate(plan.steps):
        if isinstance(step, WaitStep):
            result = await execute_wait_step(step)
        elif isinstance(step, CommandStep):
            result = await execute_command_step(
                visa, session, step,
                override_safety=override_safety,
                override_reason=override_reason,
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
            }

    return {
        "success": True,
        "recipe": recipe_name or plan.name,
        "steps_executed": step_results,
        "step_count": len(step_results),
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

    # Recipe → IR Plan 変換
    try:
        plan = recipe_to_plan(recipe, variables)
    except ExpressionError as e:
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
