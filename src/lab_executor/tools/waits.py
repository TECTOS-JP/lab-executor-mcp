"""
v0.5.1: start_wait_job MCP ツール

単発の wait ジョブを起動する高レベル API。
recipe を経由せず、4 種の wait_type を直接 Job 化する。

  - seconds:      params={"seconds": float}                           resource 不要
  - until:        params={"timestamp": ISO8601 | "seconds_from_now": float}
  - condition:    params={"instrument", "command", "condition_expr", ...}
  - stable_value: params={"instrument", "command", "tolerance", "window_s", ...}

condition / stable_value は params["instrument"] が required_resources に含まれる。
"""
from __future__ import annotations
import logging

from fastmcp import FastMCP

from lab_executor.job import JobManager
from lab_executor.job.state_machine import JobStatus
from lab_executor.response_envelope import make_envelope, make_error

logger = logging.getLogger(__name__)


_VALID_WAIT_TYPES = ("seconds", "until", "condition", "stable_value")


def register_tools(mcp: FastMCP, job_mgr: JobManager) -> None:

    @mcp.tool()
    async def start_wait_job(
        wait_type: str,
        params: dict | None = None,
        owner: str = "",
        job_timeout_s: float = 0.0,
        queue_policy: str = "queue",
    ) -> dict:
        """
        単発の wait ジョブを起動し、即座に job_id を返す (v0.5.1)。

        wait_type:
          - "seconds":      指定秒数だけ待機  (params={"seconds": float})
          - "until":        絶対 / 相対 deadline (params: timestamp or seconds_from_now)
          - "condition":    条件式が True になるまで polling
              params={
                "instrument": "GPIB0::1::INSTR",
                "command": "measure_temperature",
                "condition_expr": "value > 80",
                "args"?: {...},
                "interval_s"?: 5.0,         # デフォルト 1.0
                "timeout_s"?: 1800,         # デフォルト 60.0
                "value_path"?: "temperature",
                "retry_on_error"?: 1,
                "max_consecutive_errors"?: 3,
                "command_timeout_s"?: 5.0,
              }
          - "stable_value": window 内 (max - min) <= tolerance まで polling
              params={
                "instrument": "...",
                "command": "...",
                "tolerance": 0.2,
                "window_s": 60,
                "interval_s"?: 5.0,
                "timeout_s"?: 1800,
                "min_samples"?: 3,
                "value_path"?: "...",
                "retry_on_error"?: 1,
                "max_consecutive_errors"?: 3,
              }

        owner: 所有者識別子 (任意)
        job_timeout_s: Job 全体の制限秒数 (0 で 24h デフォルト)
        queue_policy: "queue" / "reject_if_busy"

        condition / stable_value は対象 instrument の resource lock を取る。
        seconds / until は resource を取らない (即起動)。
        """
        if wait_type not in _VALID_WAIT_TYPES:
            return make_envelope(
                "error",
                errors=[make_error(
                    "validation",
                    f"不正な wait_type: {wait_type!r} (valid: {list(_VALID_WAIT_TYPES)})",
                    recoverable=False,
                )],
            )
        if queue_policy not in ("queue", "reject_if_busy"):
            return make_envelope(
                "error",
                errors=[make_error(
                    "validation",
                    f"queue_policy は 'queue' / 'reject_if_busy': {queue_policy}",
                    recoverable=False,
                )],
            )

        params = params or {}
        try:
            rec = await job_mgr.start_wait_job(
                wait_type=wait_type,
                params=params,
                owner=owner,
                job_timeout_s=(job_timeout_s if job_timeout_s > 0 else None),
                queue_policy=queue_policy,
            )
        except Exception as e:
            logger.exception("start_wait_job 失敗")
            return make_envelope(
                "error",
                errors=[make_error("internal", str(e), recoverable=False)],
            )

        data = {
            "job_id": rec.job_id,
            "status": rec.status.value,
            "wait_type": wait_type,
            "resource_name": rec.resource_name,
            "created_at": rec.created_at,
        }
        try:
            scheduling = await job_mgr.scheduler.get_scheduling_info(rec.job_id)
            scheduling["queue_policy"] = queue_policy
            data["scheduling"] = scheduling
        except Exception:
            data["scheduling"] = {
                "immediate_start": True if not rec.resource_name else False,
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
