"""v0.6.0: Group / Map MCP ツール (4 個)

- list_groups
- list_experiment_units
- start_group_query_job
- start_map_recipe_job

`get_group_status` は新設せず、既存 `get_job_status` の `data.progress` に
group/map 進捗が載るため不要。
"""
from __future__ import annotations
import logging

from fastmcp import FastMCP

from lab_executor.job import JobManager
from lab_executor.job.state_machine import JobStatus
from lab_executor.response_envelope import make_envelope, make_error

logger = logging.getLogger(__name__)


def register_tools(mcp: FastMCP, job_mgr: JobManager) -> None:

    @mcp.tool()
    async def list_groups() -> dict:
        """instrument_groups の一覧を返す (v0.6.0)。

        各 group には members (alias リスト) と description が含まれる。
        members の alias は instruments セクションで定義されている必要がある。
        """
        cfg = job_mgr.system_config
        groups = []
        for name, g in cfg.instrument_groups.items():
            groups.append({
                "name": name,
                "members": list(g.members),
                "member_count": len(g.members),
                "description": g.description,
            })
        return make_envelope("ok", data={"groups": groups, "count": len(groups)})

    @mcp.tool()
    async def list_experiment_units() -> dict:
        """experiment_units の一覧を返す (v0.6.0)。

        各 unit には bindings (role → alias) と description が含まれる。
        map_recipe の target.unit から参照される。
        """
        cfg = job_mgr.system_config
        units = []
        for name, u in cfg.experiment_units.items():
            units.append({
                "name": name,
                "bindings": dict(u.bindings),
                "description": u.description,
            })
        return make_envelope("ok", data={"units": units, "count": len(units)})

    @mcp.tool()
    async def start_group_query_job(
        group: str,
        command: str,
        args: dict | None = None,
        concurrency: int = 10,
        failure_policy: dict | None = None,
        owner: str = "",
        job_timeout_s: float = 0.0,
        queue_policy: str = "queue",
    ) -> dict:
        """instrument_groups[group] の全機器に対し同じ query を並列実行する Job (v0.6.0)。

        group: instrument_groups で定義されたグループ名
        command: 各機器の YAML 定義で共通して使えるコマンド名 (query 系)
        args: command への引数 (全機器共通)
        concurrency: 同時実行数 (デフォルト 10)
        failure_policy: {"mode": "continue", "retry": 0, ...}
        queue_policy: "queue" / "reject_if_busy"

        返り値の data.scheduling に enqueue 直後の状態が入る。
        進捗は get_job_status の data.progress、完了結果は get_job_result の
        data.result.results (target 単位、入力順) と data.result.summary を参照。
        """
        if queue_policy not in ("queue", "reject_if_busy"):
            return make_envelope(
                "error",
                errors=[make_error(
                    "validation",
                    f"queue_policy は 'queue' / 'reject_if_busy': {queue_policy}",
                    recoverable=False,
                )],
            )
        args = args or {}
        try:
            rec = await job_mgr.start_group_query_job(
                group_name=group,
                command_name=command,
                args=args,
                concurrency=concurrency,
                failure_policy=failure_policy,
                owner=owner,
                job_timeout_s=(job_timeout_s if job_timeout_s > 0 else None),
                queue_policy=queue_policy,
            )
        except Exception as e:
            logger.exception("start_group_query_job 失敗")
            return make_envelope(
                "error",
                errors=[make_error("internal", str(e), recoverable=False)],
            )

        data = {
            "job_id": rec.job_id,
            "status": rec.status.value,
            "group": group,
            "command": command,
            "created_at": rec.created_at,
        }
        try:
            scheduling = await job_mgr.scheduler.get_scheduling_info(rec.job_id)
            scheduling["queue_policy"] = queue_policy
            data["scheduling"] = scheduling
        except Exception:
            pass

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
    async def start_map_recipe_job(
        recipe: str,
        targets: list,
        concurrency: int = 10,
        failure_policy: dict | None = None,
        owner: str = "",
        job_timeout_s: float = 0.0,
        queue_policy: str = "queue",
        primary_role: str = "",
    ) -> dict:
        """recipe を異なる条件で各 target に並列適用する Job (v0.6.0)。

        targets: 各要素は dict
          {
            "target_id": "sample001",          # 必須、結果に紐づく ID
            "unit": "unit001",                 # 任意、experiment_units からの取り込み
            "bindings": {"psu": "psu001", ...}, # 任意、unit と merge (明示優先)
            "parameters": {"voltage": 1.0},    # recipe parameters
          }
        concurrency: 同時 active target 数
        failure_policy: {
          "mode": "continue"|"stop_on_first_error"|"stop_if_failure_rate_exceeds",
          "retry": 0,
          "stop_if_failure_rate_exceeds": 0.5,
          # v0.6.1.1: 以下は予約フィールド (現状未実装、入力されても無視)
          "cancel_running_on_policy_stop": false,     # reserved (v0.6.1+ に予約)
          "retry_safe_shutdown_before_retry": false,  # reserved (v0.7.0+ に予約)
        }
        primary_role: recipe を取得する主 instrument の役割名。
          - bindings (unit と merge 後) に 1 つしか role がなければ自動推定
          - **bindings に複数 role がある場合は必須** (v0.6.0.1)。
            未指定だと validation error を返す (推定の罠を防ぐため)

        進捗は get_job_status の data.progress (target counts) を参照。
        完了後は get_job_result の data.result.results (入力 target_id 順) と
        data.result.summary を参照。partial_failure は正常系 (成功 target の結果も返る)。
        """
        if queue_policy not in ("queue", "reject_if_busy"):
            return make_envelope(
                "error",
                errors=[make_error("validation",
                    f"queue_policy: {queue_policy}", recoverable=False)],
            )
        if not targets:
            return make_envelope(
                "error",
                errors=[make_error("validation",
                    "targets が空です", recoverable=False)],
            )
        try:
            rec = await job_mgr.start_map_recipe_job(
                recipe_name=recipe,
                targets_spec=list(targets),
                concurrency=concurrency,
                failure_policy=failure_policy,
                owner=owner,
                job_timeout_s=(job_timeout_s if job_timeout_s > 0 else None),
                queue_policy=queue_policy,
                primary_role=(primary_role or None),
            )
        except Exception as e:
            logger.exception("start_map_recipe_job 失敗")
            return make_envelope(
                "error",
                errors=[make_error("internal", str(e), recoverable=False)],
            )

        data = {
            "job_id": rec.job_id,
            "status": rec.status.value,
            "recipe": recipe,
            "target_count": len(targets),
            "created_at": rec.created_at,
        }
        try:
            scheduling = await job_mgr.scheduler.get_scheduling_info(rec.job_id)
            scheduling["queue_policy"] = queue_policy
            data["scheduling"] = scheduling
        except Exception:
            pass

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
