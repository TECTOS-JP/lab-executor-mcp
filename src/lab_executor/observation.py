"""
v0.8.2: Observation 層

JobStore の低レベル行 (job_events / job_steps / target_runs / monitor_data)
を、AI エージェントと人間が「実験の流れとして読める」構造化ビューへ変換する。

3 つの外部 API (`tools/observation.py` から呼ばれる) を支える内部関数:

- `normalize_event(row)`: 内部 event_type → timeline kind / severity / title / summary
- `compute_current_phase(rec, latest_events)`: enum 化された現在フェーズ
- `build_summary(...)`: 完了 Job の構造化要約

設計原則:
- 「見やすいログ」ではなく「次の判断に使える実験状態ビュー」
- monitor_sample 等の大量イベントは timeline には含めず、`get_monitor_data` に任せる
- judgement (次の実験条件提案など) は MCP 側で行わない、客観的な構造化のみ
"""
from __future__ import annotations
from datetime import datetime
from typing import Any


# ============================================================
# Phase / Kind / Severity の安定 enum 候補 (v1.0 で互換保証対象)
# ============================================================


# 内部 event_type → 外部 timeline kind マッピング
_EVENT_KIND_MAP: dict[str, str] = {
    # Job ライフサイクル
    "job_created": "job",
    "job_started": "job",
    "experiment_job_started": "job",
    "job_completed": "job",
    "job_failed": "failure",
    "job_cancelled": "failure",
    "job_interrupted": "failure",
    "job_timeout": "failure",
    "job_resumed": "job",
    # Step
    "step_started": "step",
    "step_completed": "step",
    "step_failed": "failure",
    # Target
    "target_started": "target",
    "target_completed": "target",
    "target_failed": "failure",
    # Barrier
    "barrier_arrived": "barrier",
    "barrier_completed": "barrier",
    "barrier_timeout": "failure",
    # Stagger
    "stagger_wait_started": "stagger",
    "stagger_wait_completed": "stagger",
    # Verify
    "verify_passed": "verify",
    "verify_failed": "failure",
    # Monitor
    "poll_sample": "monitor_sample",
    "poll_condition_met": "monitor_sample",
    "monitor_stop_condition_met": "monitor_sample",
    # Safe shutdown
    "safe_shutdown_started": "safe_shutdown",
    "safe_shutdown_completed": "safe_shutdown",
    "safe_shutdown_failed": "failure",
}


_EVENT_SEVERITY_MAP: dict[str, str] = {
    # critical (緊急 / 監査必須)
    "safe_shutdown_failed": "critical",
    "job_interrupted": "critical",
    # error (実験失敗)
    "step_failed": "error",
    "target_failed": "error",
    "verify_failed": "error",
    "barrier_timeout": "error",
    "job_failed": "error",
    "job_timeout": "error",
    "job_cancelled": "error",
    # その他は info (v0.8.2.1: monitor_stop_condition_met は payload に severity
    # が無い限り info 扱いに変更。正常終了条件 vs 安全停止条件は判別不可なため、
    # 控えめな info をデフォルトとする)
}


def event_kind(event_type: str) -> str:
    """内部 event_type を外部 timeline kind に正規化 (未知は 'job')"""
    return _EVENT_KIND_MAP.get(event_type, "job")


def event_severity(event_type: str) -> str:
    """event_type → severity (info / warning / error / critical)"""
    return _EVENT_SEVERITY_MAP.get(event_type, "info")


# ============================================================
# Phase enum (v1.0 で互換保証候補)
# ============================================================


PHASE_ENUM: tuple[str, ...] = (
    "queued",
    "starting",
    "running_step",
    "waiting",
    "polling",
    "waiting_for_stable",
    "barrier_wait",
    "stagger_wait",
    "monitoring",
    "safe_shutdown",
    "cancelling",
    "completed",
    "failed",
    "partial_failure",
    "interrupted",
    "unknown",
)


def compute_job_outcome(
    job_status: str,
    target_runs: list[dict] | None = None,
) -> str | None:
    """Job 終端時の "結果" を返す。Job state machine とは独立した観測値。

    v0.8.2.1 でレビュー指摘 P1-6 に応答: `partial_failure` を job_status から分離。
    State machine は `partial_failure` を扱わないが、Observation API では target_runs
    を見て `partial_failure` を返す。

    Returns:
        - "success": 全 target が ok / target なしで Job completed
        - "partial_failure": completed だが一部 target 失敗
        - "failure": job_status が failed / timeout
        - "cancelled": cancelled
        - "interrupted": interrupted
        - None: Job がまだ終端でない
    """
    if job_status == "completed":
        if target_runs:
            failed = sum(
                1 for t in target_runs if t.get("status") not in ("ok", None)
            )
            if failed > 0:
                return "partial_failure"
        return "success"
    if job_status == "failed" or job_status == "timeout":
        return "failure"
    if job_status == "cancelled":
        return "cancelled"
    if job_status == "interrupted":
        return "interrupted"
    return None


def compute_current_phase(
    job_status: str,
    last_event_type: str | None,
    last_step_summary: str | None = None,
    progress_type: str | None = None,
    job_outcome: str | None = None,
) -> str:
    """Job 状態 + 最終 event + 進捗から `current_phase` を決定

    v0.8.2.1: job_outcome="partial_failure" の場合 completed だが phase は
    "partial_failure" として返す (人間/AI への注意喚起)。
    """
    # 終端状態
    if job_status == "completed":
        if job_outcome == "partial_failure":
            return "partial_failure"
        return "completed"
    if job_status == "failed":
        return "failed"
    if job_status == "cancelled":
        return "failed"  # 終端の一種として
    if job_status == "timeout":
        return "failed"
    if job_status == "interrupted":
        return "interrupted"
    if job_status == "cancelling":
        return "cancelling"
    if job_status == "queued":
        return "queued"

    # running / waiting 中の細分化
    if progress_type == "monitor":
        return "monitoring"
    if progress_type == "group_or_map":
        return "running_step"
    if last_event_type:
        if "barrier_arrived" in last_event_type or "barrier_wait" in last_event_type:
            return "barrier_wait"
        if "stagger" in last_event_type:
            return "stagger_wait"
        if "safe_shutdown" in last_event_type:
            return "safe_shutdown"
    # last_step_summary を見て polling / waiting を判定
    if last_step_summary:
        s = last_step_summary.lower()
        if "wait_for_stable" in s:
            return "waiting_for_stable"
        if "wait_for_condition" in s:
            return "polling"
        if "wait" in s:
            return "waiting"
    if job_status == "waiting":
        return "waiting"
    if job_status == "running":
        return "running_step"
    return "unknown"


# ============================================================
# timeline item normalizer
# ============================================================


def normalize_event(
    event_row: dict[str, Any],
    *,
    include_raw: bool = False,
) -> dict[str, Any]:
    """job_events 1 行 → 外部 timeline item に正規化

    event_row は JobStore.list_events() の返り値 dict (timestamp / event_type /
    target_id / step_index / payload など)。
    """
    event_type = event_row.get("event_type", "")
    payload = event_row.get("payload") or {}
    kind = event_kind(event_type)
    severity = event_severity(event_type)

    item: dict[str, Any] = {
        "timestamp": event_row.get("timestamp"),
        "event_id": event_row.get("event_id"),
        "kind": kind,
        "event_type": event_type,
        "severity": severity,
        "title": _make_title(event_type, payload),
        "summary": _make_summary_text(event_type, payload, event_row),
    }
    # 任意フィールド
    target_id = event_row.get("target_id") or payload.get("target_id")
    if target_id is not None:
        item["target_id"] = target_id
    step_index = event_row.get("step_index")
    if step_index is not None:
        item["step_index"] = step_index
    if payload:
        # よく使うキーは top-level に昇格
        for k in ("instrument", "command", "step_type", "step_path",
                  "error_class", "recoverable", "measurement", "value", "unit"):
            if k in payload:
                item[k] = payload[k]

    if include_raw:
        item["raw_event"] = dict(event_row)

    return item


def _make_title(event_type: str, payload: dict) -> str:
    """短い title (約 40 文字以内)"""
    cmd = payload.get("command") or payload.get("step_type") or ""
    target = payload.get("target_id") or ""

    if event_type == "step_started":
        return f"step started ({cmd})" if cmd else "step started"
    if event_type == "step_completed":
        return f"step completed ({cmd})" if cmd else "step completed"
    if event_type == "step_failed":
        return f"step failed ({cmd})" if cmd else "step failed"
    if event_type == "target_started":
        return f"target {target} started"
    if event_type == "target_completed":
        return f"target {target} completed"
    if event_type == "target_failed":
        return f"target {target} failed"
    if event_type == "barrier_arrived":
        return f"barrier arrived: {payload.get('barrier_name', '?')}"
    if event_type == "barrier_completed":
        return "barrier completed"
    if event_type == "barrier_timeout":
        return f"barrier timeout: {payload.get('barrier_name', '?')}"
    if event_type == "verify_failed":
        return "verify failed"
    if event_type == "safe_shutdown_started":
        return "safe_shutdown started"
    if event_type == "safe_shutdown_completed":
        return "safe_shutdown completed"
    if event_type.startswith("job_"):
        return event_type.replace("_", " ")
    return event_type


def _make_summary_text(event_type: str, payload: dict, row: dict) -> str:
    """LLM/人間向けの短い 1 行説明"""
    cmd = payload.get("command", "")
    instr = payload.get("instrument", "")
    target = payload.get("target_id") or row.get("target_id") or ""
    step_idx = row.get("step_index")
    parts: list[str] = []
    if target:
        parts.append(f"target={target}")
    if instr:
        parts.append(instr)
    if cmd:
        parts.append(cmd)
    if step_idx is not None:
        parts.append(f"step={step_idx}")
    err = payload.get("error_class") or payload.get("error")
    if err:
        parts.append(f"error={err}")
    return ", ".join(parts) if parts else event_type


# ============================================================
# Live view ヘルパ
# ============================================================


def latest_event_kind(events: list[dict]) -> str | None:
    """list_events (新しい順) の最初の event_type を返す"""
    if not events:
        return None
    return events[0].get("event_type")


def filter_kinds(
    items: list[dict], kinds: list[str] | None, default_exclude_monitor: bool = True,
) -> list[dict]:
    """timeline items を kinds で絞り込む。
    kinds=None なら monitor_sample をデフォルト除外。
    """
    if kinds:
        allowed = set(kinds)
        return [it for it in items if it.get("kind") in allowed]
    if default_exclude_monitor:
        return [it for it in items if it.get("kind") != "monitor_sample"]
    return items


# ============================================================
# Summary 用ロジック
# ============================================================


def build_run_summary(
    job_rec_dict: dict[str, Any],
    steps: list[dict],
    target_runs: list[dict],
    monitor_count: int = 0,
    monitor_latest: dict | None = None,
) -> dict[str, Any]:
    """完了 Job の構造化要約を組み立てる。

    job_rec_dict: jobs テーブル 1 行 (dict 化済み)
    steps: job_steps の全行
    target_runs: target_runs の全行
    """
    status = job_rec_dict.get("status", "unknown")
    result = job_rec_dict.get("result") or {}

    # step 集計
    total_steps = len(steps)
    completed_steps = sum(1 for s in steps if s.get("status") == "ok")
    failed_steps = sum(1 for s in steps if s.get("status") == "failed")

    # target 集計
    total_targets = len(target_runs)
    successful_targets = sum(1 for t in target_runs if t.get("status") == "ok")
    failed_targets = sum(1 for t in target_runs if t.get("status") not in ("ok", None))

    # verify 集計 (job_steps の result/error に verify があれば数える)
    verify_total = 0
    verify_passed = 0
    verify_failed = 0
    for s in steps:
        # v0.9.0.1: strict mode で verify が失敗すると step status=failed となり
        # result ではなく error に verify 情報が入る (step_executor 仕様)。
        # 両方を見るように修正。
        r = s.get("result") or s.get("error") or {}
        if not isinstance(r, dict):
            continue
        v = r.get("verify")
        if isinstance(v, dict):
            verify_total += 1
            if r.get("verified") is True:
                verify_passed += 1
            elif r.get("verified") is False:
                verify_failed += 1

    # duration
    duration_s: float | None = None
    try:
        ca = job_rec_dict.get("created_at")
        ua = job_rec_dict.get("updated_at")
        if ca and ua:
            duration_s = (
                datetime.fromisoformat(ua) - datetime.fromisoformat(ca)
            ).total_seconds()
    except Exception:
        pass

    # failures (target_runs 経由)
    failures: list[dict] = []
    for t in target_runs:
        if t.get("status") not in ("ok", None):
            err = t.get("error") or {}
            failures.append({
                "target_id": t.get("target_id"),
                "error_class": (err.get("error_class") if isinstance(err, dict) else None)
                              or t.get("status") or "unknown",
                "recoverable": _is_recoverable(
                    (err.get("error_class") if isinstance(err, dict) else None)
                    or t.get("status") or "",
                ),
            })

    # key_results (現状: monitor_latest を一覧表示 / target_runs の数値 result)
    key_results: list[dict] = []
    if monitor_latest:
        key_results.append({
            "name": "monitor_latest_value",
            "count": monitor_count,
            "value": monitor_latest.get("value"),
            "instrument": monitor_latest.get("instrument"),
        })

    # recommended_next_actions (控えめに、確実な action のみ)
    recommended: list[dict] = []
    if status in ("failed", "cancelled", "interrupted", "timeout") or failures:
        recoverable_failures = [f for f in failures if f.get("recoverable")]
        if recoverable_failures and total_targets > 0:
            recommended.append({
                "action": "retry_failed_targets",
                "reason": (
                    f"{len(recoverable_failures)} of {total_targets} targets failed "
                    f"with recoverable errors"
                ),
                "target_ids": [f["target_id"] for f in recoverable_failures],
            })
        recommended.append({
            "action": "inspect_job_result",
            "tool": "get_job_result",
            "args": {"job_id": job_rec_dict.get("job_id")},
            "reason": "失敗箇所の詳細を確認",
        })

    job_outcome = compute_job_outcome(status, target_runs)

    return {
        "job_id": job_rec_dict.get("job_id"),
        "job_status": status,
        "job_outcome": job_outcome,
        "summary": {
            "total_steps": total_steps,
            "completed_steps": completed_steps,
            "failed_steps": failed_steps,
            "total_targets": total_targets,
            "successful_targets": successful_targets,
            "failed_targets": failed_targets,
            "duration_s": (round(duration_s, 2) if duration_s is not None else None),
        },
        "verify_summary": {
            "total": verify_total,
            "passed": verify_passed,
            "failed": verify_failed,
        },
        "failures": failures,
        "key_results": key_results,
        "monitor_samples_recorded": monitor_count,
        "warnings": result.get("persistence_warnings") or [],
        "recommended_next_actions": recommended,
    }


def _is_recoverable(error_class: str) -> bool:
    """error_class taxonomy から recoverable かを判定 (簡易)"""
    recoverable = {
        "timeout", "protocol", "hardware", "verify_mismatch", "blocked",
        "partial_failure",
    }
    return error_class.lower() in recoverable
