"""ビューモデル構築 (純関数のみ、FastAPI 非依存)。

``lab_executor.observation`` の既存純関数 (compute_job_outcome /
compute_current_phase / normalize_event / build_run_summary /
latest_event_kind) を **再実装せず import** して、表示用 dict に整える。
AI (MCP) と人間 (UI) が同じ観測ビューを見ることが設計の核。
"""
from __future__ import annotations

from typing import Any

from lab_executor.job.state_machine import JobStatus
from lab_executor.observation import (
    build_run_summary,
    compute_current_phase,
    compute_job_outcome,
    latest_event_kind,
    normalize_event,
)

# 8 状態 (JobStatus) + outcome (compute_job_outcome の返り値) の色クラス名。
# テンプレートで `status-<class>` の CSS クラスとして使う。
STATUS_COLORS: dict[str, str] = {
    # JobStatus
    "queued": "queued",
    "running": "running",
    "waiting": "waiting",
    "completed": "completed",
    "failed": "failed",
    "cancelling": "cancelling",
    "cancelled": "cancelled",
    "timeout": "failed",
    "interrupted": "interrupted",
    # outcome (compute_job_outcome)
    "success": "completed",
    "partial_failure": "partial_failure",
    "failure": "failed",
    # phase の一部で使う派生
    "unknown": "unknown",
}

# 終端でない状態 (ポーリング継続の判定に使う)。
_TERMINAL_STATUSES = {
    JobStatus.COMPLETED.value,
    JobStatus.FAILED.value,
    JobStatus.CANCELLED.value,
    JobStatus.TIMEOUT.value,
    JobStatus.INTERRUPTED.value,
}


def status_color(key: str | None) -> str:
    """status / outcome / phase 文字列 → CSS クラス名 (未知は 'unknown')。"""
    if not key:
        return "unknown"
    return STATUS_COLORS.get(key, "unknown")


def is_terminal_status(status: str | None) -> bool:
    return status in _TERMINAL_STATUSES


def job_row_view(
    job: dict[str, Any],
    last_event_type: str | None,
) -> dict[str, Any]:
    """一覧 1 行の表示用 dict。

    一覧では target_runs を引かないため outcome は target 無し (None) 扱いで計算する
    (completed → success / failed → failure など、target 集約なしの粗い値)。
    """
    status = job.get("status", "unknown")
    outcome = compute_job_outcome(status, None)
    phase = compute_current_phase(
        status,
        last_event_type,
        last_step_summary=job.get("last_step_summary"),
        job_outcome=outcome,
    )
    return {
        "job_id": job.get("job_id"),
        "owner": job.get("owner", ""),
        "resource_name": job.get("resource_name", ""),
        "recipe": job.get("recipe", ""),
        "status": status,
        "status_color": status_color(status),
        "phase": phase,
        "phase_color": status_color(phase),
        "outcome": outcome,
        "current_step_index": job.get("current_step_index", -1),
        "error_class": job.get("error_class", ""),
        "last_step_summary": job.get("last_step_summary", ""),
        "created_at": job.get("created_at", ""),
        "updated_at": job.get("updated_at", ""),
        "is_terminal": is_terminal_status(status),
    }


def sweep_chart_view(
    sweep_points: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """sweep 点列 (``_extract_sweep_views`` の返り値) を uPlot が直接食える
    形の dict に変換する。sweep 点が 0 個なら ``None``。

    入力は ``lab_executor.tools.observation._extract_sweep_views(store, job_id)``
    の返り値。この private ヘルパは ``store.list_steps()`` しか呼ばないため
    ReadOnlyJobStore をそのまま渡せる。M1 の ``_row_to_record`` 再利用と同様、
    同一プロジェクト内の意図的な観測ロジック再利用 (再実装しない)。

    出力::

        {
          "x": [sweep_value か、全点 None なら sweep_index],   # 昇順
          "series": [
            {"label": "psu1: measure_voltage", "values": [...]},
          ],
          "x_label": "sweep_value" | "sweep_index",
        }

    - ``value_numeric`` が None の点は null として保持 (uPlot は gap 表示)。
    - series は (instrument, command) 毎。x 軸は sweep_index 昇順に整列済み。
    """
    if not sweep_points:
        return None

    # sweep_index 昇順 (念のため。_extract_sweep_views は既に sort 済み)。
    points = sorted(sweep_points, key=lambda p: p.get("sweep_index", 0))

    # x 軸: 全点で sweep_value があれば sweep_value、1 点でも None なら
    # sweep_index にフォールバック。
    sweep_values = [p.get("sweep_value") for p in points]
    if all(v is not None for v in sweep_values):
        x = sweep_values
        x_label = "sweep_value"
    else:
        x = [p.get("sweep_index") for p in points]
        x_label = "sweep_index"

    # series は (instrument, command) の組を出現順に収集する。
    series_keys: list[tuple[str | None, str]] = []
    for p in points:
        for m in p.get("measurements", []):
            command = m.get("command")
            if not command:
                continue
            key = (m.get("instrument"), command)
            if key not in series_keys:
                series_keys.append(key)

    series: list[dict[str, Any]] = []
    for instrument, command in series_keys:
        values: list[float | int | None] = []
        for p in points:
            # 同一点内で (instrument, command) にマッチする最初の measurement。
            match = None
            for m in p.get("measurements", []):
                if m.get("instrument") == instrument and m.get("command") == command:
                    match = m
                    break
            values.append(match.get("value_numeric") if match else None)
        label = f"{instrument}: {command}" if instrument else str(command)
        series.append({"label": label, "values": values})

    return {"x": x, "series": series, "x_label": x_label}


def job_detail_view(
    job: dict[str, Any],
    steps: list[dict[str, Any]],
    events: list[dict[str, Any]],
    target_runs: list[dict[str, Any]],
) -> dict[str, Any]:
    """ジョブ詳細の表示用 dict。

    - ``events`` は list_events の返り値 (新しい順)。timeline は
      normalize_event で正規化し、表示は **古い順** に並べ直す。
    - 終端ジョブなら build_run_summary を含める。
    """
    status = job.get("status", "unknown")
    last_event_type = latest_event_kind(events)  # events[0].event_type
    outcome = compute_job_outcome(status, target_runs)
    phase = compute_current_phase(
        status,
        last_event_type,
        last_step_summary=job.get("last_step_summary"),
        job_outcome=outcome,
    )

    # timeline: normalize してから古い順 (timestamp / event_id 昇順) に。
    timeline = [normalize_event(e) for e in events]
    timeline.sort(
        key=lambda it: (it.get("timestamp") or "", it.get("event_id") or 0)
    )
    for it in timeline:
        it["severity_color"] = it.get("severity", "info")

    steps_view = [
        {
            **s,
            "status_color": status_color(
                "completed" if s.get("status") == "ok"
                else "failed" if s.get("status") == "failed"
                else "running"
            ),
        }
        for s in steps
    ]

    summary = None
    if is_terminal_status(status):
        summary = build_run_summary(job, steps, target_runs)

    return {
        "job": job,
        "job_id": job.get("job_id"),
        "status": status,
        "status_color": status_color(status),
        "phase": phase,
        "phase_color": status_color(phase),
        "outcome": outcome,
        "is_terminal": is_terminal_status(status),
        "steps": steps_view,
        "timeline": timeline,
        "target_runs": target_runs,
        "summary": summary,
    }
