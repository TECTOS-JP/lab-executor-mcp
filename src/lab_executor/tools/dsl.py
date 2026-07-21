"""
v0.8.0: Experiment DSL MCP ツール (4 個)

- validate_experiment_plan: 構文 + resource + safety + verify 検証
- dry_run_plan: 実機 I/O 無しの rendered + safety + verify summary
- start_experiment_job: validate → compile → persist → Job 実行
- save_experiment_template: 再利用可能テンプレート保存
"""
from __future__ import annotations
import logging
import uuid as _uuid
from typing import TYPE_CHECKING

from fastmcp import FastMCP

from lab_executor.dsl import CURRENT_DSL_VERSION
from lab_executor.dsl.compiler import validate_and_compile
from lab_executor.dsl.template import apply_template_override, TemplateOverrideError
from lab_executor.job import JobManager
from lab_executor.job.state_machine import JobStatus
from lab_executor.response_envelope import make_envelope, make_error

if TYPE_CHECKING:
    # v1.11: backend layer top-level import 排除
    from lab_visa_mcp.session_manager import SessionManager

logger = logging.getLogger(__name__)


def register_tools(
    mcp: FastMCP,
    session_mgr: SessionManager,
    job_mgr: JobManager,
) -> None:

    @mcp.tool()
    async def validate_experiment_plan(plan: dict) -> dict:
        """LLM が生成した DSL plan を検証する (v0.8.0)

        実機通信は行わず、以下を検証:
          - JSON schema (dsl_version, step type 等)
          - instrument / binding 解決
          - command 存在 + type + parameter 範囲
          - safety constraints (strict/advisory mode 連携)
          - verify 設定 (readback_command の存在 / query 型)
          - polling_safe ヒント
          - sweep 展開サイズ / parallel concurrency 上限

        返り値の data:
          - valid: bool
          - summary: { step_count_dsl, step_count_expanded, required_resources,
                       resolved_instruments, estimated_duration_s, uses_verify,
                       uses_polling, has_safe_shutdown, has_parallel }
        errors[]: validation 失敗時の構造化エラー (各 error に
                  recommended_next_actions を含む)
        warnings[]: 非致命的な注意事項 (例: polling_safe_false, raw_resource_used)
        """
        if not isinstance(plan, dict):
            return make_envelope(
                "error",
                errors=[make_error("validation",
                    "plan は JSON object である必要があります", recoverable=False)],
            )
        try:
            compiled = validate_and_compile(plan, session_mgr, job_mgr.system_config)
        except Exception as e:
            logger.exception("validate_experiment_plan 内部エラー")
            return make_envelope(
                "error",
                errors=[make_error("internal", str(e), recoverable=False)],
            )
        if compiled.valid:
            return make_envelope("ok", data={
                "valid": True,
                "summary": compiled.summary,
                "warnings": compiled.warnings,
            })
        else:
            # 構造化 errors を envelope errors にマップ
            envelope_errors = [
                make_error(
                    e.get("error_class", "validation"),
                    e.get("message", "?"),
                    recoverable=True,
                    recommended_next_actions=e.get("recommended_next_actions"),
                    details={k: v for k, v in e.items()
                             if k not in ("error_class", "message", "recommended_next_actions")},
                )
                for e in compiled.errors
            ]
            return make_envelope(
                "error",
                data={
                    "valid": False,
                    "summary": compiled.summary,
                    "warnings": compiled.warnings,
                },
                errors=envelope_errors,
            )

    @mcp.tool()
    async def dry_run_plan(plan: dict) -> dict:
        """DSL plan を実機 I/O 無しで「実行予定」として展開する (v0.8.0)

        validate_experiment_plan の結果に加え、各 command step の
        rendered SCPI / safety / verify 予定を返す。

        実機通信:
          - 一切行わない (*IDN? / state_query / verify readback も呼ばない)
          - YAML 定義 + parameter renderer + safety_validator のみ使用
        """
        if not isinstance(plan, dict):
            return make_envelope(
                "error",
                errors=[make_error("validation", "plan は JSON object", recoverable=False)],
            )
        try:
            compiled = validate_and_compile(plan, session_mgr, job_mgr.system_config)
        except Exception as e:
            logger.exception("dry_run_plan 内部エラー")
            return make_envelope(
                "error",
                errors=[make_error("internal", str(e), recoverable=False)],
            )
        # v0.8.0.1: CompiledPlan.rendered_steps を直接利用 (再 compile 不要)
        rendered_steps = compiled.rendered_steps

        envelope_status = "ok" if compiled.valid else "error"
        # v0.8.1.1: validate_experiment_plan と同じく、recommended_next_actions を
        # top-level 引数で渡し、details に埋もれさせない (AI エージェント可読性)
        envelope_errors = (
            [
                make_error(
                    e.get("error_class", "validation"),
                    e.get("message", "?"),
                    recoverable=True,
                    recommended_next_actions=e.get("recommended_next_actions"),
                    details={k: v for k, v in e.items()
                             if k not in ("error_class", "message",
                                          "recommended_next_actions")},
                )
                for e in compiled.errors
            ]
            if not compiled.valid else None
        )
        return make_envelope(
            envelope_status,
            data={
                "dry_run": True,
                "valid": compiled.valid,
                "summary": compiled.summary,
                "warnings": compiled.warnings,
                "rendered_steps": rendered_steps,
                "note": "実機通信は一切行っていません",
            },
            errors=envelope_errors,
        )

    @mcp.tool()
    async def start_experiment_job(
        plan: dict,
        owner: str = "",
        override_safety: bool = False,
        override_reason: str = "",
        job_timeout_s: float = 0.0,
        queue_policy: str = "queue",
    ) -> dict:
        """DSL plan を validate + compile + 実行する (v0.8.0)

        plan: ExperimentPlan に準拠した JSON
        owner / override_safety / override_reason: 既存 Job API と同様
        job_timeout_s: 0 で 24h default、それ以外は秒指定
        queue_policy: "queue" / "reject_if_busy"

        Job として登録された後、`get_job_status` / `get_job_result` /
        `cancel_job` で操作可能。original plan は experiment_plans テーブルに
        永続化される。
        """
        if queue_policy not in ("queue", "reject_if_busy"):
            return make_envelope(
                "error",
                errors=[make_error("validation",
                    f"queue_policy: {queue_policy}", recoverable=False)],
            )
        try:
            rec = await job_mgr.start_experiment_job(
                plan_dict=plan or {},
                owner=owner,
                override_safety=override_safety,
                override_reason=override_reason,
                job_timeout_s=(job_timeout_s if job_timeout_s > 0 else None),
                queue_policy=queue_policy,
            )
        except Exception as e:
            logger.exception("start_experiment_job 失敗")
            return make_envelope(
                "error",
                errors=[make_error("internal", str(e), recoverable=False)],
            )
        data = {
            "job_id": rec.job_id,
            "status": rec.status.value,
            "name": (plan or {}).get("name", ""),
            "dsl_version": (plan or {}).get("dsl_version", CURRENT_DSL_VERSION),
            "created_at": rec.created_at,
        }
        # plan_id を補完 (parameters_json に含まれている)
        if rec.parameters and rec.parameters.get("plan_id"):
            data["plan_id"] = rec.parameters["plan_id"]
        try:
            sch = await job_mgr.scheduler.get_scheduling_info(rec.job_id)
            sch["queue_policy"] = queue_policy
            data["scheduling"] = sch
        except Exception:
            pass

        # 失敗時は validation_errors を errors[] に
        envelope_errors = None
        if rec.status == JobStatus.FAILED and rec.result:
            verr = rec.result.get("validation_errors") or []
            if verr:
                envelope_errors = [
                    make_error(
                        e.get("error_class", "validation"),
                        e.get("message", "?"),
                        recoverable=True,
                        details={k: v for k, v in e.items()
                                 if k not in ("error_class", "message")},
                    ) for e in verr
                ]

        return make_envelope(
            "ok" if rec.status != JobStatus.FAILED else "error",
            data=data,
            errors=envelope_errors or (
                [make_error(
                    rec.error_class or "validation",
                    rec.last_step_summary or "failed",
                    recoverable=False,
                )] if rec.status == JobStatus.FAILED else None
            ),
            job_id=rec.job_id,
        )

    @mcp.tool()
    async def start_experiment_job_from_template(
        name: str,
        override: dict | None = None,
        owner: str = "",
        dry_run: bool = False,
        include_expanded_plan: bool = False,
        override_safety: bool = False,
        override_reason: str = "",
        job_timeout_s: float = 0.0,
        queue_policy: str = "queue",
    ) -> dict:
        """**(experimental, v0.8.3)** 保存済み template を override 適用して実行する

        Override は **限定キーのみ許可**:
          name / unit / bindings / parameters / owner
        steps / dsl_version / variables 直接上書き等は拒否される (構造を変えるなら
        通常 `start_experiment_job(plan)` を使う)。

        dry_run=True なら Job を開始せず、override 後の Plan を validate +
        rendered_steps で返す (`dry_run_plan` 相当)。

        include_expanded_plan=True で expanded_plan (override 適用後の Plan JSON)
        も data に同梱。default False (応答サイズ抑制)。

        Job metadata の parameters / experiment_plans.compiled_summary に
        `template_source` が記録され、後から Job がどの template + override から
        生成されたかを追える。
        """
        if not name or not name.strip():
            return make_envelope(
                "error",
                errors=[make_error("validation", "name が必須",
                                   recoverable=False)],
            )
        if queue_policy not in ("queue", "reject_if_busy"):
            return make_envelope(
                "error",
                errors=[make_error("validation",
                    f"queue_policy: {queue_policy}", recoverable=False)],
            )

        try:
            tpl = job_mgr.store.get_experiment_template(name.strip())
        except Exception as e:
            return make_envelope(
                "error",
                errors=[make_error("internal", str(e))],
            )
        if tpl is None:
            return make_envelope(
                "error",
                errors=[make_error(
                    "not_found",
                    f"template '{name}' は存在しません",
                    recoverable=False,
                    recommended_next_actions=[
                        {"action": "list_experiment_templates",
                         "tool": "list_experiment_templates"},
                    ],
                )],
            )

        template_plan = tpl.get("plan")
        if not isinstance(template_plan, dict):
            return make_envelope(
                "error",
                errors=[make_error(
                    "internal",
                    f"template '{name}' の plan が JSON object ではありません",
                    recoverable=False,
                )],
            )

        # override 適用
        try:
            expanded_plan, applied_summary = apply_template_override(
                template_plan, override or {},
            )
        except TemplateOverrideError as e:
            return make_envelope(
                "error",
                errors=[make_error(
                    "validation",
                    str(e),
                    recoverable=True,
                    details={
                        "sub_class": "template_override_invalid",
                        "rejected_keys": e.rejected_keys,
                    },
                    recommended_next_actions=[
                        {"action": "use_start_experiment_job",
                         "reason": "steps/構造を変える場合は通常 Plan を使ってください"},
                    ],
                )],
            )

        template_source = {
            "template_name": name.strip(),
            "template_version": tpl.get("dsl_version") or CURRENT_DSL_VERSION,
            "override_json": override or {},
            "override_keys": applied_summary.get("override_keys", []),
        }

        # dry_run: Job 開始せず validate + rendered_steps
        if dry_run:
            try:
                compiled = validate_and_compile(
                    expanded_plan, session_mgr, job_mgr.system_config,
                )
            except Exception as e:
                return make_envelope(
                    "error",
                    errors=[make_error("internal", str(e), recoverable=False)],
                )
            envelope_status = "ok" if compiled.valid else "error"
            envelope_errors = (
                [
                    make_error(
                        e.get("error_class", "validation"),
                        e.get("message", "?"),
                        recoverable=True,
                        recommended_next_actions=e.get("recommended_next_actions"),
                        details={k: v for k, v in e.items()
                                 if k not in ("error_class", "message",
                                              "recommended_next_actions")},
                    )
                    for e in compiled.errors
                ]
                if not compiled.valid else None
            )
            data: dict = {
                "dry_run": True,
                "valid": compiled.valid,
                "template": {
                    "name": name.strip(),
                    "version": tpl.get("dsl_version") or CURRENT_DSL_VERSION,
                    "override_applied": applied_summary.get("override_applied", False),
                    "override_keys": applied_summary.get("override_keys", []),
                },
                "summary": compiled.summary,
                "warnings": compiled.warnings,
                "rendered_steps": compiled.rendered_steps,
                "note": "Job は開始されていません",
            }
            if include_expanded_plan:
                data["expanded_plan"] = expanded_plan
            return make_envelope(
                envelope_status, data=data, errors=envelope_errors,
            )

        # v0.8.3.1: override.owner が指定されていればそれを優先 (P2)
        effective_owner = applied_summary.get("owner") or owner

        # 実行
        try:
            rec = await job_mgr.start_experiment_job(
                plan_dict=expanded_plan,
                owner=effective_owner,
                override_safety=override_safety,
                override_reason=override_reason,
                job_timeout_s=(job_timeout_s if job_timeout_s > 0 else None),
                queue_policy=queue_policy,
                template_source=template_source,
            )
        except Exception as e:
            logger.exception("start_experiment_job_from_template 失敗")
            return make_envelope(
                "error",
                errors=[make_error("internal", str(e), recoverable=False)],
            )

        data = {
            "job_id": rec.job_id,
            "status": rec.status.value,
            "owner": rec.owner or effective_owner,
            "name": expanded_plan.get("name", ""),
            "dsl_version": expanded_plan.get("dsl_version", CURRENT_DSL_VERSION),
            "created_at": rec.created_at,
            "template": {
                "name": name.strip(),
                "version": tpl.get("dsl_version") or CURRENT_DSL_VERSION,
                "override_applied": applied_summary.get("override_applied", False),
                "override_keys": applied_summary.get("override_keys", []),
            },
        }
        if include_expanded_plan:
            data["expanded_plan"] = expanded_plan
        if rec.parameters and rec.parameters.get("plan_id"):
            data["plan_id"] = rec.parameters["plan_id"]

        envelope_errors = None
        if rec.status == JobStatus.FAILED and rec.result:
            verr = rec.result.get("validation_errors") or []
            if verr:
                envelope_errors = [
                    make_error(
                        e.get("error_class", "validation"),
                        e.get("message", "?"),
                        recoverable=True,
                        details={k: v for k, v in e.items()
                                 if k not in ("error_class", "message")},
                    ) for e in verr
                ]
        return make_envelope(
            "ok" if rec.status != JobStatus.FAILED else "error",
            data=data,
            errors=envelope_errors or (
                [make_error(
                    rec.error_class or "validation",
                    rec.last_step_summary or "failed",
                    recoverable=False,
                )] if rec.status == JobStatus.FAILED else None
            ),
            job_id=rec.job_id,
        )

    @mcp.tool()
    async def list_experiment_templates() -> dict:
        """保存済み実験テンプレート一覧を取得 (v0.8.0.1)

        plan_json 本体は返さず、name / dsl_version / description / timestamps のみ。
        詳細は get_experiment_template(name) で取得する。
        """
        try:
            items = job_mgr.store.list_experiment_templates()
        except Exception as e:
            return make_envelope(
                "error",
                errors=[make_error("internal", str(e))],
            )
        return make_envelope("ok", data={
            "count": len(items),
            "templates": items,
        })

    @mcp.tool()
    async def get_experiment_template(name: str) -> dict:
        """指定 name のテンプレートを取得 (plan JSON 本体含む) (v0.8.0.1)"""
        if not name or not name.strip():
            return make_envelope(
                "error",
                errors=[make_error("validation", "name が必須", recoverable=False)],
            )
        try:
            tpl = job_mgr.store.get_experiment_template(name.strip())
        except Exception as e:
            return make_envelope(
                "error",
                errors=[make_error("internal", str(e))],
            )
        if tpl is None:
            return make_envelope(
                "error",
                errors=[make_error(
                    "not_found",
                    f"template '{name}' は存在しません",
                    recoverable=False,
                )],
            )
        return make_envelope("ok", data=tpl)

    @mcp.tool()
    async def save_experiment_template(
        name: str,
        plan: dict,
        description: str = "",
    ) -> dict:
        """experiment_templates テーブルに再利用可能 template を保存 / 上書き (v0.8.0)

        保存前に validate_experiment_plan を実行 (実行はしない)。
        validation 失敗時は保存しない。

        name: テンプレート名 (主キー、重複は上書き)
        plan: ExperimentPlan JSON
        description: 説明 (任意)
        """
        if not name or not name.strip():
            return make_envelope(
                "error",
                errors=[make_error("validation", "name が必須", recoverable=False)],
            )
        if not isinstance(plan, dict):
            return make_envelope(
                "error",
                errors=[make_error("validation", "plan は JSON object", recoverable=False)],
            )
        try:
            compiled = validate_and_compile(plan, session_mgr, job_mgr.system_config)
        except Exception as e:
            return make_envelope(
                "error",
                errors=[make_error("internal", str(e), recoverable=False)],
            )
        if not compiled.valid:
            return make_envelope(
                "error",
                data={"valid": False, "warnings": compiled.warnings},
                errors=[
                    make_error(
                        e.get("error_class", "validation"),
                        e.get("message", "?"),
                        recoverable=True,
                        details={k: v for k, v in e.items()
                                 if k not in ("error_class", "message")},
                    ) for e in compiled.errors
                ],
            )
        try:
            job_mgr.store.save_experiment_template(
                name=name.strip(),
                dsl_version=plan.get("dsl_version", CURRENT_DSL_VERSION),
                plan=plan,
                description=description,
            )
        except Exception as e:
            return make_envelope(
                "error",
                errors=[make_error("internal", str(e), recoverable=False)],
            )
        return make_envelope("ok", data={
            "name": name.strip(),
            "dsl_version": plan.get("dsl_version", CURRENT_DSL_VERSION),
            "description": description,
            "summary": compiled.summary,
            "warnings": compiled.warnings,
        })


# v0.8.0.1: _extract_rendered_from_compile は CompiledPlan.rendered_steps の
# 正式フィールド化により不要となったため削除。tools 層が compiler の private
# helper (_Context / _convert_step) に依存しなくなった。
