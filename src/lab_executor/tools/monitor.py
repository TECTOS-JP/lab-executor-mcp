"""v0.7.0: Monitor MCP ツール (3 個)

- start_monitor: 定期測定 Job を起動
- stop_monitor:  既存 monitor を cancel (cancel_job alias)
- get_monitor_data: monitor_data テーブルから時系列データ取得
"""
from __future__ import annotations
import logging

from fastmcp import FastMCP

from lab_executor.job import CancelMode, JobManager
from lab_executor.job.state_machine import JobStatus, is_terminal
from lab_executor.response_envelope import make_envelope, make_error

logger = logging.getLogger(__name__)


def register_tools(mcp: FastMCP, job_mgr: JobManager) -> None:

    @mcp.tool()
    async def start_monitor(
        instrument: str,
        command: str,
        interval_s: float = 5.0,
        duration_s: float = 600.0,
        stop_condition: str = "",
        value_path: str = "",
        args: dict | None = None,
        owner: str = "",
        queue_policy: str = "queue",
    ) -> dict:
        """機器を定期測定する Monitor Job を起動 (v0.7.0)

        instrument: VISA resource 名 / alias
        command: query 系 command 名 (polling_safe=True 推奨)
        interval_s: poll 間隔 (>=1.0)
        duration_s: 全体制限 (<=86400=24h)
        stop_condition: 条件式 ("value > 80" 等)。空なら duration まで継続
        value_path: parsed response の数値フィールド名 (任意)
        args: command への引数

        測定値は monitor_data テーブルに保存され、get_monitor_data で取得可能。
        cancel_job(job_id) で停止可能。
        """
        if queue_policy not in ("queue", "reject_if_busy"):
            return make_envelope(
                "error",
                errors=[make_error(
                    "validation",
                    f"queue_policy: {queue_policy}", recoverable=False,
                )],
            )
        try:
            rec = await job_mgr.start_monitor_job(
                instrument=instrument,
                command_name=command,
                interval_s=interval_s,
                duration_s=duration_s,
                stop_condition_expr=(stop_condition or None),
                value_path=(value_path or None),
                args=args or {},
                owner=owner,
                queue_policy=queue_policy,
            )
        except Exception as e:
            logger.exception("start_monitor 失敗")
            return make_envelope(
                "error",
                errors=[make_error("internal", str(e), recoverable=False)],
            )
        data = {
            "job_id": rec.job_id,
            "monitor_id": rec.job_id,    # monitor_data の monitor_id = job_id
            "status": rec.status.value,
            "instrument": instrument,
            "command": command,
            "created_at": rec.created_at,
        }
        try:
            sch = await job_mgr.scheduler.get_scheduling_info(rec.job_id)
            sch["queue_policy"] = queue_policy
            data["scheduling"] = sch
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
    async def stop_monitor(
        monitor_id: str,
        timeout_s: float = 10.0,
    ) -> dict:
        """Monitor Job を停止 (cancel_job のエイリアス、after_current_step mode)"""
        try:
            rec = await job_mgr.cancel(
                monitor_id, CancelMode.AFTER_CURRENT_STEP, timeout_s=timeout_s,
            )
        except Exception as e:
            return make_envelope(
                "error",
                errors=[make_error("internal", str(e))],
            )
        return make_envelope(
            "ok",
            data={
                "monitor_id": rec.job_id,
                "status": rec.status.value,
                "is_terminal": is_terminal(rec.status),
                "samples_recorded": (rec.result or {}).get("samples_recorded"),
            },
            job_id=rec.job_id,
        )

    # v0.7.0.1: limit 上限 (1 ツール呼び出しで返す最大行数)
    _MAX_MONITOR_LIMIT = 10000

    @mcp.tool()
    async def get_monitor_data(
        monitor_id: str,
        limit: int = 1000,
        offset: int = 0,
    ) -> dict:
        """Monitor Job の時系列測定データを取得 (時系列順)

        get_job_result は monitor_data を含めないため、大量データはこちらで取る。

        v0.7.0.1: limit は 1 〜 10000 に強制クランプ (上限を超える指定は警告込みで丸める)。
        全件取得には offset を進めながら複数回呼ぶ。
        """
        # validation
        if limit <= 0:
            limit = 1000
        clamp_warning = None
        if limit > _MAX_MONITOR_LIMIT:
            clamp_warning = (
                f"limit={limit} が上限 {_MAX_MONITOR_LIMIT} を超過したため "
                f"{_MAX_MONITOR_LIMIT} にクランプしました。"
                f"全件取得には offset を進めて複数回呼んでください"
            )
            limit = _MAX_MONITOR_LIMIT
        if offset < 0:
            offset = 0
        try:
            total = job_mgr.store.count_monitor_data(monitor_id)
            data = job_mgr.store.list_monitor_data(monitor_id, limit=limit, offset=offset)
        except Exception as e:
            return make_envelope(
                "error",
                errors=[make_error("internal", str(e))],
            )
        result_data = {
            "monitor_id": monitor_id,
            "total_samples": total,
            "returned": len(data),
            "limit_used": limit,
            "offset": offset,
            "has_more": (offset + len(data)) < total,
            "data": data,
        }
        if clamp_warning:
            result_data["clamp_warning"] = clamp_warning
        return make_envelope("ok", data=result_data)

    @mcp.tool()
    async def prune_monitor_data(
        monitor_id: str = "",
        older_than_days: float = 0.0,
    ) -> dict:
        """Monitor data を削除する (v0.7.0.1 新設、DB 肥大化対策)

        monitor_id: 指定 monitor_id の全 data を削除 (older_than_days=0 と排他的に使う)
        older_than_days: 指定日数より古い全 monitor_data を削除
                         (monitor_id="" と組み合わせて全体 prune)

        どちらか一方を指定すること。両方指定された場合は monitor_id が優先。
        どちらも省略 (monitor_id="" かつ older_than_days=0) なら validation error。

        運用例:
          - 単一 monitor 削除: prune_monitor_data(monitor_id="job_abc123")
          - 7 日以上前の全 data 削除: prune_monitor_data(older_than_days=7)
        """
        if not monitor_id and older_than_days <= 0:
            return make_envelope(
                "error",
                errors=[make_error(
                    "validation",
                    "monitor_id または older_than_days のどちらかを指定してください",
                    recoverable=False,
                )],
            )
        try:
            if monitor_id:
                deleted = job_mgr.store.delete_monitor_data(monitor_id)
                return make_envelope("ok", data={
                    "monitor_id": monitor_id,
                    "deleted_rows": deleted,
                    "mode": "by_monitor_id",
                })
            else:
                deleted = job_mgr.store.prune_monitor_data(older_than_days)
                return make_envelope("ok", data={
                    "older_than_days": older_than_days,
                    "deleted_rows": deleted,
                    "mode": "by_age",
                })
        except Exception as e:
            return make_envelope(
                "error",
                errors=[make_error("internal", str(e))],
            )
