"""Job 系 MCP ツール (v0.5.0-rc2)

5 個の高レベル Job 操作ツール:
  - start_recipe_job
  - get_job_status
  - get_job_result
  - list_jobs
  - cancel_job

全て v0.5.0+ の標準レスポンス形式 (response_envelope) で返す。
"""
from __future__ import annotations
import logging

from fastmcp import FastMCP

from lab_executor.job import CancelMode, JobManager
from lab_executor.job.state_machine import JobStatus, is_terminal
from lab_executor.response_envelope import make_envelope, make_error

logger = logging.getLogger(__name__)


def _recommended_actions_for(rec) -> list[dict]:
    """
    Job 終端状態に応じて LLM 向けの次手候補を返す。

    各 action は下記キー:
      action       : 名称 (retry / inspect_state / safe_shutdown / resume_from_step / give_up)
      tool         : 関連 MCP ツール (任意)
      args         : 推奨引数 (任意)
      reason       : 理由 (人間向け、簡潔)
    """
    actions: list[dict] = []

    if rec.status == JobStatus.TIMEOUT:
        actions.append({
            "action": "retry",
            "tool": "start_recipe_job",
            "args": {
                "resource_name": rec.resource_name,
                "recipe_name": rec.recipe,
                "parameters": rec.parameters,
                "job_timeout_s": "<より大きな値>",
            },
            "reason": "より長い job_timeout_s で再実行する",
        })
        actions.append({
            "action": "inspect_state",
            "tool": "get_job_result",
            "args": {"job_id": rec.job_id},
            "reason": "どこで時間切れになったか steps_executed で確認",
        })
        actions.append({
            "action": "safe_shutdown",
            "reason": "機器が中途半端な状態の可能性。次の操作前に出力 OFF を確認",
        })

    elif rec.status == JobStatus.INTERRUPTED:
        actions.append({
            "action": "inspect_state",
            "tool": "get_job_result",
            "args": {"job_id": rec.job_id},
            "reason": "サーバ再起動前の last_completed_step を確認",
        })
        actions.append({
            "action": "safe_shutdown",
            "reason": "機器の現在状態が不明。安全停止コマンドで初期化を推奨",
        })
        actions.append({
            "action": "resume_from_step",
            "reason": "v0.9.0+ で実装予定。現在は手動再実行のみ",
        })

    elif rec.status == JobStatus.FAILED:
        err = rec.error_class or "internal"
        if err == "safety":
            actions.append({
                "action": "review_safety_constraints",
                "tool": "list_safety_constraints",
                "args": {"resource_name": rec.resource_name},
                "reason": "違反した安全制約の内容を確認 (まずこちらを推奨)",
            })
            actions.append({
                "action": "ask_human_for_decision",
                "reason": (
                    "安全制約違反のため、override するか諦めるかは "
                    "**人間の判断を仰ぐ必要がある**。LLM が単独で次の retry_with_override を"
                    "選んではいけない"
                ),
            })
            actions.append({
                "action": "retry_with_override",
                "tool": "start_recipe_job",
                "args": {
                    "resource_name": rec.resource_name,
                    "recipe_name": rec.recipe,
                    "parameters": rec.parameters,
                    "override_safety": True,
                    "override_reason": "<人間が明示的に承認した理由を必ず記入>",
                },
                "requires_human_confirmation": True,
                "reason": (
                    "⚠️ 危険操作: 安全制約を意図的に無視する。"
                    "**advisory モード時かつ人間が事前に明示的に承認した場合のみ**実行可能。"
                    "LLM が単独で判断・実行することは禁止"
                ),
            })
        elif err == "validation":
            actions.append({
                "action": "fix_parameters",
                "reason": "パラメータの値・型・範囲を見直して再実行",
            })
        elif err == "not_found":
            actions.append({
                "action": "list_recipes",
                "tool": "list_recipes",
                "args": {"resource_name": rec.resource_name},
                "reason": "利用可能な recipe を確認",
            })
            actions.append({
                "action": "list_resources",
                "tool": "list_resources",
                "reason": "接続中のリソース名を再確認",
            })
        else:  # timeout / hardware / protocol / internal
            actions.append({
                "action": "retry",
                "tool": "start_recipe_job",
                "args": {
                    "resource_name": rec.resource_name,
                    "recipe_name": rec.recipe,
                    "parameters": rec.parameters,
                },
                "reason": "一時的なエラーの可能性。同じ条件で再試行",
            })
            actions.append({
                "action": "inspect_state",
                "tool": "get_job_result",
                "args": {"job_id": rec.job_id},
                "reason": "失敗した step の詳細を確認",
            })

    elif rec.status == JobStatus.CANCELLED:
        actions.append({
            "action": "inspect_state",
            "tool": "get_job_result",
            "args": {"job_id": rec.job_id},
            "reason": "どこまで実行されたか確認",
        })

    return actions


def register_tools(mcp: FastMCP, job_mgr: JobManager) -> None:

    @mcp.tool()
    async def start_recipe_job(
        resource_name: str,
        recipe_name: str,
        parameters: dict = {},
        owner: str = "",
        override_safety: bool = False,
        override_reason: str = "",
        job_timeout_s: float = 0.0,
        queue_policy: str = "queue",
    ) -> dict:
        """
        Recipe をバックグラウンド Job として登録し、即座に job_id を返す。
        LLM のツール呼び出しはブロックされない。進捗は get_job_status で確認する。

        resource_name: VISA リソース文字列
        recipe_name: YAML 定義された recipe のキー
        parameters: recipe パラメータ (例: {"target_v": 5.0})
        owner: 所有者識別子 (将来のマルチエージェント用、任意)
        override_safety: 安全制約警告を override (advisory モード時のみ有効)
        override_reason: override 理由 (override_safety=True 時必須)
        job_timeout_s: Job 全体の制限秒数。0 または未指定なら 24 時間。
                       経過すると Job は自動で TIMEOUT 状態に遷移する。
        queue_policy: "queue" (デフォルト、busy 時は queued で順番待ち) /
                      "reject_if_busy" (busy 時は failed、error_class='blocked')

        返り値の data.scheduling フィールドに、scheduler の状態を含む:
          - immediate_start: True なら enqueue 時点で即実行可能だった
          - blocked_by_job: queue 待ち中の場合の blocker job_id
          - queue_position: queue 内位置 (即実行なら -1)
        """
        if queue_policy not in ("queue", "reject_if_busy"):
            return make_envelope(
                "error",
                errors=[make_error(
                    "validation",
                    f"queue_policy は 'queue' / 'reject_if_busy' のいずれか: {queue_policy}",
                    recoverable=False,
                )],
            )
        try:
            rec = await job_mgr.start_recipe_job(
                resource_name, recipe_name, parameters,
                owner=owner,
                override_safety=override_safety,
                override_reason=override_reason,
                job_timeout_s=(job_timeout_s if job_timeout_s > 0 else None),
                queue_policy=queue_policy,
            )
        except Exception as e:
            logger.exception("start_recipe_job 失敗")
            return make_envelope(
                "error",
                errors=[make_error("internal", str(e), recoverable=False)],
            )

        data = {
            "job_id": rec.job_id,
            "status": rec.status.value,
            "resource_name": rec.resource_name,
            "recipe": rec.recipe,
            "created_at": rec.created_at,
        }
        # v0.5.0.4: scheduling 情報を embedded
        try:
            scheduling = await job_mgr.scheduler.get_scheduling_info(rec.job_id)
            scheduling["queue_policy"] = queue_policy
            data["scheduling"] = scheduling
        except Exception:
            # scheduler 未登録 (validation/recipe not_found で failed の場合等)
            data["scheduling"] = {
                "immediate_start": False,
                "blocked_by_job": None,
                "queue_position": -1,
                "queue_policy": queue_policy,
            }

        return make_envelope(
            "ok" if rec.status != JobStatus.FAILED else "error",
            data=data,
            errors=([make_error(
                rec.error_class or "validation",
                rec.last_step_summary or "failed",
                recoverable=False,
            )] if rec.status == JobStatus.FAILED else None),
            job_id=rec.job_id,
        )

    @mcp.tool()
    async def get_job_status(job_id: str) -> dict:
        """
        Job の現在状態を取得。短いレスポンス。

        返却フィールド (data):
          status / current_step_index / last_step_summary /
          error_class / created_at / updated_at / is_terminal /
          queue (status=queued の場合に queue_position / blocking_job_id を含む)
        """
        try:
            rec = job_mgr.get(job_id)
        except Exception:
            return make_envelope(
                "error",
                errors=[make_error("not_found", f"job not found: {job_id}", recoverable=False)],
            )
        data = {
            "job_id": rec.job_id,
            "status": rec.status.value,
            "is_terminal": is_terminal(rec.status),
            "current_step_index": rec.current_step_index,
            "last_step_summary": rec.last_step_summary,
            "error_class": rec.error_class,
            "owner": rec.owner,
            "resource_name": rec.resource_name,
            "recipe": rec.recipe,
            "created_at": rec.created_at,
            "updated_at": rec.updated_at,
        }
        # v0.5.0.2: queued 状態時に queue 情報を付与
        if rec.status == JobStatus.QUEUED:
            try:
                qinfo = await job_mgr.scheduler.get_queue_info(rec.job_id)
                if qinfo is not None:
                    data["queue"] = qinfo
            except Exception:
                pass
        # v0.5.1: polling 進捗の公開
        # v0.6.0: group/map 進捗 (type=group_or_map) も同じ data.progress に含める
        if rec.status in (JobStatus.WAITING, JobStatus.RUNNING):
            try:
                prog = job_mgr.get_progress(rec.job_id)
                if prog:
                    # group/map 進捗は data.progress、polling 進捗は data.polling
                    # 同じ runtime.current_progress を共有しているため、type で振り分け
                    if prog.get("type") == "group_or_map":
                        data["progress"] = prog
                    else:
                        data["polling"] = prog
            except Exception:
                pass
        return make_envelope("ok", data=data, job_id=rec.job_id)

    @mcp.tool()
    async def get_job_result(job_id: str) -> dict:
        """
        終端 Job の完全な結果を取得 (steps_executed を含む)。
        まだ実行中の場合は status が "running" として返る。
        """
        try:
            rec = job_mgr.get(job_id)
        except Exception:
            return make_envelope(
                "error",
                errors=[make_error("not_found", f"job not found: {job_id}", recoverable=False)],
            )

        if not is_terminal(rec.status):
            return make_envelope(
                "running",
                data={
                    "job_id": rec.job_id,
                    "status": rec.status.value,
                    "current_step_index": rec.current_step_index,
                    "last_step_summary": rec.last_step_summary,
                },
                job_id=rec.job_id,
            )

        status_map = {
            JobStatus.COMPLETED: "ok",
            JobStatus.FAILED: "error",
            JobStatus.CANCELLED: "error",
            JobStatus.TIMEOUT: "error",
            JobStatus.INTERRUPTED: "error",
        }
        envelope_status = status_map.get(rec.status, "error")
        errors_field = None
        if envelope_status == "error":
            errors_field = [make_error(
                rec.error_class or "internal",
                rec.last_step_summary or rec.status.value,
                recoverable=(rec.status not in (JobStatus.FAILED,)),
                recommended_next_actions=_recommended_actions_for(rec),
            )]
        return make_envelope(
            envelope_status,
            data={
                "job_id": rec.job_id,
                "status": rec.status.value,
                "resource_name": rec.resource_name,
                "recipe": rec.recipe,
                "result": rec.result or {},
                "created_at": rec.created_at,
                "updated_at": rec.updated_at,
            },
            errors=errors_field,
            job_id=rec.job_id,
        )

    @mcp.tool()
    async def list_jobs(
        status_filter: list = None,
        owner: str = "",
        limit: int = 50,
    ) -> dict:
        """
        Job 一覧を取得 (新しい順)。

        status_filter: ["running", "completed"] など。空 None なら全件
        owner: 所有者で絞り込み (空文字列は無視)
        limit: 最大件数
        """
        sf = list(status_filter) if status_filter else None
        owner_filter = owner if owner else None
        recs = job_mgr.list_jobs(status_filter=sf, limit=limit, owner=owner_filter)
        return make_envelope(
            "ok",
            data={
                "count": len(recs),
                "jobs": [
                    {
                        "job_id": r.job_id,
                        "status": r.status.value,
                        "is_terminal": is_terminal(r.status),
                        "resource_name": r.resource_name,
                        "recipe": r.recipe,
                        "owner": r.owner,
                        "current_step_index": r.current_step_index,
                        "error_class": r.error_class,
                        "created_at": r.created_at,
                        "updated_at": r.updated_at,
                    }
                    for r in recs
                ],
            },
        )

    @mcp.tool()
    async def cancel_job(
        job_id: str,
        cancel_mode: str = "after_current_step",
        timeout_s: float = 30.0,
    ) -> dict:
        """
        Job のキャンセルを要求し、終端まで待機する。

        cancel_mode:
          - "immediate"           : asyncio.Task を直ちにキャンセル
          - "after_current_step"  : 現在の step 完了後に停止
          - "safe_shutdown"       : OUTP OFF / VOLT 0 を試みてから停止
        timeout_s: 終端遷移を待つ最大秒数
        """
        try:
            mode = CancelMode(cancel_mode)
        except ValueError:
            return make_envelope(
                "error",
                errors=[make_error(
                    "validation",
                    f"不正な cancel_mode: {cancel_mode}",
                    details={"valid": [m.value for m in CancelMode]},
                    recoverable=False,
                )],
            )
        try:
            rec = await job_mgr.cancel(job_id, mode, timeout_s=timeout_s)
        except Exception as e:
            return make_envelope(
                "error",
                errors=[make_error("internal", str(e))],
            )
        return make_envelope(
            "ok",
            data={
                "job_id": rec.job_id,
                "status": rec.status.value,
                "is_terminal": is_terminal(rec.status),
                "cancel_mode": cancel_mode,
                "last_step_summary": rec.last_step_summary,
            },
            job_id=rec.job_id,
        )

    @mcp.tool()
    async def resume_job(
        job_id: str,
        from_step: int | None = None,
        dry_run: bool = False,
        safe_shutdown_before_resume: bool = False,
        owner: str = "",
    ) -> dict:
        """**(experimental, v0.9.0)** interrupted / cancelled / failed / timeout
        Job を **新規 Job として** 手動再開する。

        設計 (実装方針 #10 案 B):
          - 元 Job の status は変えず、resumed_from_job_id を持つ新 Job を作る
          - 履歴は両 Job の job_events に残す (`resume_started` /`job_resumed`)

        引数:
          job_id: 再開元の元 Job
          from_step: 再開する **DSL top-level step index** (original_plan の
                     `steps[]` における 0-origin index)。sweep 展開後の
                     IR step index ではなく、**LLM が書いた original DSL の
                     steps[] index** を指す (v0.9.0.1 で明文化)。
                     `None` の場合は実行されず、`suggested_from_step` を返すだけ。
          dry_run: True で Job を起動せず steps_to_execute / required_resources
                   / warnings を返す。AI/人間が再開内容を事前確認するため。
          safe_shutdown_before_resume: True で再開前に required_resources に
                   best_effort_safe_shutdown を試行 (結果は warnings に記録)。
          owner: 新 Job の owner (省略時は元 Job の owner を継承)。

        resume 可能条件 (満たさないと resume_not_allowed):
          - 元 Job が interrupted / cancelled / failed / timeout のいずれか
          - experiment_plan が保存されている (DSL Job 由来)
          - dsl_version が現バージョンと互換
          - safe_shutdown_failed 終端でない

        ⚠ from_step より前の step は完了済みと仮定される。
        `resume_may_repeat_side_effects` warning が必ず付く。
        """
        try:
            data = await job_mgr.resume_job(
                job_id, from_step=from_step, dry_run=dry_run,
                safe_shutdown_before_resume=safe_shutdown_before_resume,
                owner=owner,
            )
        except Exception as e:
            return make_envelope(
                "error",
                errors=[make_error("internal", str(e), recoverable=False)],
            )
        errs = data.get("errors") or []
        if errs:
            return make_envelope(
                "error",
                data=data,
                errors=[
                    make_error(
                        e.get("error_class", "validation"),
                        e.get("message", "?"),
                        recoverable=True,
                        recommended_next_actions=e.get("recommended_next_actions"),
                        details=e.get("details"),
                    )
                    for e in errs
                ],
            )
        return make_envelope("ok", data=data,
                             job_id=data.get("resumed_job_id") or job_id)
