"""
v0.8.2: Observation MCP ツール (3 個)

- `get_experiment_timeline`: 何がいつ起きたか (時系列)
- `get_job_live_view`:        いま何が起きているか (実行中 Job)
- `get_job_summary`:          終了後に何が分かったか (完了 Job)

設計原則:
- 既存 get_job_status / get_job_result を置き換えず、補助 read API として追加
- judgement (次条件提案など) は MCP 側で行わず、客観的構造化のみ
- monitor_sample はデフォルト除外 (kinds 明示時のみ含む)
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone

from fastmcp import FastMCP

from lab_executor.job import JobManager
from lab_executor.job.state_machine import is_terminal
from lab_executor.observation import (
    PHASE_ENUM, compute_current_phase, compute_job_outcome, normalize_event,
    latest_event_kind, filter_kinds, build_run_summary,
)
from lab_executor.response_envelope import make_envelope, make_error


def _value_numeric_from_result(result: dict | None) -> float | int | None:
    if not isinstance(result, dict):
        return None
    parsed = result.get("parsed")
    if not isinstance(parsed, dict):
        return None

    def _as_number(value):
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return None
        return None

    value = _as_number(parsed.get("value_numeric"))
    if value is not None:
        return value
    fields = parsed.get("fields")
    if isinstance(fields, dict):
        return _as_number(fields.get("value"))
    return None


def _step_payload(step: dict) -> dict | None:
    result = step.get("result")
    if isinstance(result, dict):
        return result
    error = step.get("error")
    if isinstance(error, dict):
        return error
    return None


def _extract_instrument_views(
    store, job_id: str, instrument: str | None = None,
) -> list[dict]:
    """Build per-instrument observation views from job_steps.

    Pure helper for the v2.6 MCP tool. It intentionally reads only
    store.list_steps(job_id) and does not mutate any runtime state.
    """
    steps = store.list_steps(job_id)
    grouped: dict[str, list[tuple[dict, dict]]] = {}

    for step in steps:
        payload = _step_payload(step)
        if not payload:
            continue
        instr = payload.get("instrument")
        if not instr:
            continue
        if instrument is not None and instr != instrument:
            continue
        grouped.setdefault(str(instr), []).append((step, payload))

    views: list[dict] = []
    for instr, items in grouped.items():
        items.sort(key=lambda item: (
            item[0].get("step_index") if item[0].get("step_index") is not None else -1,
            item[0].get("id") or 0,
        ))
        last_step, last_payload = items[-1]

        ok_count = sum(1 for step, _ in items if step.get("status") == "ok")
        failed_count = sum(1 for step, _ in items if step.get("status") == "failed")
        measurements: list[dict] = []
        for step, payload in items:
            command = payload.get("command")
            has_raw = "raw_response" in payload
            if not command or not has_raw:
                continue
            measurements.append({
                "step_index": step.get("step_index"),
                "command": command,
                "raw_response": payload.get("raw_response"),
                "value_numeric": _value_numeric_from_result(payload),
                "status": step.get("status"),
                "ended_at": step.get("ended_at"),
            })

        views.append({
            "instrument": instr,
            "step_count": len(items),
            "ok_count": ok_count,
            "failed_count": failed_count,
            "last_status": last_step.get("status"),
            "last_command": last_payload.get("command"),
            "last_raw_response": last_payload.get("raw_response"),
            "last_value_numeric": _value_numeric_from_result(last_payload),
            "last_step_index": last_step.get("step_index"),
            "last_ended_at": last_step.get("ended_at"),
            "measurements": measurements,
        })

    views.sort(key=lambda view: (
        view["measurements"][0]["step_index"]
        if view["measurements"] else view["last_step_index"]
    ) if (
        view["measurements"] or view["last_step_index"] is not None
    ) else -1)
    return views


def _parse_iso8601(s: str, field_name: str) -> datetime | None:
    """ISO8601 を堅牢にパース。失敗時は None。

    v0.8.2.1: 文字列比較ではなく datetime に正規化。
    末尾 'Z' は '+00:00' に置換 (Python 3.10 fromisoformat 互換)。
    """
    if not s:
        return None
    text = s.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return None

logger = logging.getLogger(__name__)


def register_tools(mcp: FastMCP, job_mgr: JobManager) -> None:

    @mcp.tool()
    async def get_experiment_timeline(
        job_id: str,
        since: str = "",
        until: str = "",
        limit: int = 200,
        kinds: list | None = None,
        include_raw: bool = False,
    ) -> dict:
        """Job 実行中に発生した時系列イベントを構造化して取得 (v0.8.2)

        単に job_events を dump せず、外部公開用 kind / severity / title / summary を
        付与した timeline item として返す。

        kinds (例: ["step", "barrier", "failure", "verify"]):
          step / barrier / stagger / verify / failure / monitor_sample /
          target / job / safe_shutdown のいずれか。
          **省略時は `monitor_sample` を除外**して返す (大量データを混ぜないため)。
        since: ISO8601 (例: "2026-05-22T10:00:00+09:00")。これより新しいイベントのみ
        until: ISO8601。これより古いイベントのみ
        limit: 最大返却数 (default 200、上限 5000)
        include_raw: True なら各 item に元の job_events 行を含める

        v0.8.2.1:
          - since/until を datetime.fromisoformat で比較 (string compare 廃止)
          - invalid timestamp は validation error (invalid_since_timestamp /
            invalid_until_timestamp) で返す
          - pagination は (timestamp, event_id) 複合 cursor に変更
            (同一 timestamp の複数 event の取りこぼし対策)

        返り値: data.timeline (新しい順) + data.pagination (limit / returned /
        has_more / next_cursor: {timestamp, event_id} | None)
        """
        try:
            rec = job_mgr.get(job_id)
        except Exception:
            return make_envelope(
                "error",
                errors=[make_error("not_found", f"job not found: {job_id}",
                                   recoverable=False)],
            )

        # since/until を datetime にパース (v0.8.2.1)
        since_dt: datetime | None = None
        until_dt: datetime | None = None
        if since:
            since_dt = _parse_iso8601(since, "since")
            if since_dt is None:
                return make_envelope(
                    "error",
                    errors=[make_error(
                        "validation",
                        f"since must be a valid ISO8601 timestamp, got: {since!r}",
                        recoverable=True,
                        details={
                            "sub_class": "invalid_since_timestamp",
                            "field": "since",
                            "value": since,
                        },
                    )],
                )
        if until:
            until_dt = _parse_iso8601(until, "until")
            if until_dt is None:
                return make_envelope(
                    "error",
                    errors=[make_error(
                        "validation",
                        f"until must be a valid ISO8601 timestamp, got: {until!r}",
                        recoverable=True,
                        details={
                            "sub_class": "invalid_until_timestamp",
                            "field": "until",
                            "value": until,
                        },
                    )],
                )

        # limit clamp
        if limit <= 0:
            limit = 200
        clamp_warning = None
        if limit > 5000:
            clamp_warning = f"limit={limit} は上限 5000 にクランプされました"
            limit = 5000

        # ALL イベントを取得 (since/until フィルタは Python 側で実施)
        # 内部 list_events は新しい順
        raw_events = job_mgr.store.list_events(job_id, limit=limit * 4)

        # since/until で絞り込み (datetime 比較)
        def _in_range(e: dict) -> bool:
            ts_str = e.get("timestamp")
            if not ts_str:
                return True
            try:
                ts = _parse_iso8601(ts_str, "event_ts")
            except Exception:
                return True
            if ts is None:
                return True
            if since_dt is not None and ts < since_dt:
                return False
            if until_dt is not None and ts >= until_dt:
                return False
            return True

        filtered_raw = [e for e in raw_events if _in_range(e)]

        # normalize
        items = [normalize_event(e, include_raw=include_raw) for e in filtered_raw]
        # kinds フィルタ (デフォルト monitor_sample 除外)
        items = filter_kinds(items, kinds)

        # limit
        truncated = items[:limit]
        has_more = len(items) > limit
        next_cursor: dict | None = None
        if has_more and truncated:
            # 新しい順なので最後 = 最も古い。次ページはその event より「古い」もの
            # 同一 timestamp 取りこぼし対策のため event_id を併せて返す。
            tail = truncated[-1]
            next_cursor = {
                "timestamp": tail.get("timestamp"),
                "event_id": tail.get("event_id"),
            }

        data = {
            "job_id": job_id,
            "timeline": truncated,
            "pagination": {
                "limit": limit,
                "returned": len(truncated),
                "has_more": has_more,
                "next_cursor": next_cursor,
            },
        }
        if clamp_warning:
            data["clamp_warning"] = clamp_warning
        return make_envelope("ok", data=data, job_id=job_id)

    @mcp.tool()
    async def get_job_live_view(job_id: str) -> dict:
        """実行中 Job の集約ビュー (v0.8.2)

        AI / 人間が「いま何が起きているか」を 1 tool call で把握できる。
        get_job_status の進化形として位置付け、低レベル状態は引き続き get_job_status を使う。

        返り値 data:
          - job_id / job_status (raw)
          - current_phase (enum: queued / running_step / waiting_for_stable /
            barrier_wait / polling / monitoring / safe_shutdown / completed 等)
          - current_activity: { kind, description, target_id?, step_index?, step_path? }
          - progress: { completed_steps, total_steps, ... } (進捗が判明している場合)
          - latest_measurements: [{ instrument, measurement, value, unit, age_s }, ...]
          - active_waits: [{ type, target_id, elapsed_s, timeout_s, ... }] (polling 中)
          - active_barriers: [...]
          - recent_errors: 最新 5 件 (severity error/critical のみ)
          - recent_warnings
        """
        try:
            rec = job_mgr.get(job_id)
        except Exception:
            return make_envelope(
                "error",
                errors=[make_error("not_found", f"job not found: {job_id}",
                                   recoverable=False)],
            )

        # 最新 events (新しい順)
        events = job_mgr.store.list_events(job_id, limit=50)
        last_evt = latest_event_kind(events)

        # runtime の polling/group progress (live のみ)
        progress = None
        progress_type = None
        active_waits: list[dict] = []
        active_barriers: list[dict] = []
        try:
            prog = job_mgr.get_progress(job_id)
            if prog:
                progress_type = prog.get("type") or prog.get("step_type")
                progress = dict(prog)
                # polling 進捗 → active_waits に
                if prog.get("step_type") in (
                    "wait_for_condition", "wait_for_stable", "wait_until",
                ):
                    active_waits.append({
                        "type": prog["step_type"],
                        "elapsed_s": prog.get("elapsed_s"),
                        "timeout_remaining_s": prog.get("timeout_remaining_s"),
                        "instrument": prog.get("instrument"),
                        "command": prog.get("command"),
                        "last_value": prog.get("last_value"),
                        "next_poll_in_s": prog.get("next_poll_in_s"),
                    })
                # barrier 進捗 → active_barriers に
                if "barrier" in prog:
                    br = prog["barrier"]
                    active_barriers.append({
                        "barrier_name": br.get("barrier_name"),
                        "step_index": br.get("step_index"),
                        "arrived": br.get("arrived"),
                        "total_expected": br.get("total_expected"),
                        "waiting_for": br.get("waiting_for"),
                        "elapsed_s": br.get("elapsed_s"),
                    })
        except Exception:
            pass

        # v0.8.2.1: job_outcome を分離計算 (job_status != partial_failure)
        try:
            _trs = job_mgr.store.list_target_runs(job_id)
        except Exception:
            _trs = []
        job_outcome = compute_job_outcome(rec.status.value, _trs)

        phase = compute_current_phase(
            rec.status.value, last_evt, rec.last_step_summary, progress_type,
            job_outcome=job_outcome,
        )

        # current_activity
        current_activity: dict = {
            "kind": phase,
            "description": rec.last_step_summary or "",
        }
        # 直近 event payload から target_id / step_index を採用
        if events:
            ev = events[0]
            if ev.get("target_id"):
                current_activity["target_id"] = ev["target_id"]
            if ev.get("step_index") is not None:
                current_activity["step_index"] = ev["step_index"]
            payload = ev.get("payload") or {}
            if payload.get("step_path"):
                current_activity["step_path"] = payload["step_path"]

        # progress (Group/Map の場合は target counts、それ以外は step counts 推定)
        progress_summary: dict | None = None
        if progress and progress.get("type") == "group_or_map":
            progress_summary = {
                "type": "group_or_map",
                "total_targets": progress.get("total"),
                "completed_targets": progress.get("completed"),
                "failed_targets": progress.get("failed"),
                "running_targets": progress.get("running"),
                "queued_targets": progress.get("queued"),
                "skipped_targets": progress.get("skipped"),
            }
        elif progress and progress.get("type") == "monitor":
            progress_summary = {
                "type": "monitor",
                "samples": progress.get("samples"),
                "elapsed_s": progress.get("elapsed_s"),
                "remaining_s": progress.get("remaining_s"),
            }
        else:
            # 一般 Job: current_step_index のみ
            progress_summary = {
                "type": "single_job",
                "current_step_index": rec.current_step_index,
            }

        # latest_measurements (measurement_cache から)
        # v0.8.2.1: Map/DSL Job の場合 experiment_plans から required/used_resources を
        # 取得して resource を拡張 (P1-5)。private _sessions ではなく public
        # session_manager 経由 (P1-4)。
        latest: list[dict] = []
        try:
            now_dt = datetime.now(timezone.utc)
            instruments_to_check: list[str] = []
            if rec.resource_name:
                instruments_to_check.append(rec.resource_name)
            if progress and progress.get("instrument"):
                if progress["instrument"] not in instruments_to_check:
                    instruments_to_check.append(progress["instrument"])

            # experiment_plans から resource を拡張 (DSL Job 用)
            try:
                exp_plan = job_mgr.store.get_experiment_plan_for_job(job_id)
            except Exception:
                exp_plan = None
            if exp_plan:
                cs = exp_plan.get("compiled_summary") or {}
                for r in (cs.get("used_resources") or []) + (
                    cs.get("required_resources") or []
                ):
                    if r and r not in instruments_to_check:
                        instruments_to_check.append(r)

            # target_runs から resource を補完 (Group/Map Job 用)
            for t in _trs:
                for rr in (t.get("required_resources") or []):
                    if rr and rr not in instruments_to_check:
                        instruments_to_check.append(rr)
                for bv in (t.get("bindings") or {}).values():
                    if bv and bv not in instruments_to_check:
                        instruments_to_check.append(bv)

            session_mgr = job_mgr.session_manager
            # 最大 N (32) 件で打ち切り (live_view は overview なので)
            MAX_INSTRUMENTS = 32
            for instr in instruments_to_check[:MAX_INSTRUMENTS]:
                session = session_mgr.get_session(instr)
                if session is None or session.definition is None:
                    continue
                for key in session.definition.state_query.keys():
                    cached = job_mgr.store.get_measurement_cache(instr, key)
                    if cached is None:
                        continue
                    try:
                        ts = datetime.fromisoformat(cached["timestamp"])
                        age = (now_dt - ts).total_seconds()
                    except Exception:
                        age = None
                    latest.append({
                        "instrument": instr,
                        "measurement": key,
                        "value": cached["value"],
                        "unit": cached["unit"],
                        "timestamp": cached["timestamp"],
                        "age_s": (round(age, 3) if age is not None else None),
                        "source": "measurement_cache",
                    })
        except Exception:
            pass

        # recent_errors / warnings (timeline normalize の結果から)
        normalized = [normalize_event(e) for e in events]
        recent_errors = [
            n for n in normalized if n["severity"] in ("error", "critical")
        ][:5]
        recent_warnings = [n for n in normalized if n["severity"] == "warning"][:5]

        return make_envelope("ok", data={
            "job_id": job_id,
            "job_status": rec.status.value,
            "job_outcome": job_outcome,
            "current_phase": phase,
            "current_activity": current_activity,
            "progress": progress_summary,
            "latest_measurements": latest,
            "active_waits": active_waits,
            "active_barriers": active_barriers,
            "recent_errors": recent_errors,
            "recent_warnings": recent_warnings,
        }, job_id=job_id)

    @mcp.tool()
    async def get_job_instrument_view(
        job_id: str,
        instrument: str | None = None,
    ) -> dict:
        """Per-instrument observation view (experimental, v2.6).

        Re-aggregates job_steps by result.instrument without changing the
        existing live view, summary, or experiment results APIs.
        """
        try:
            rec = job_mgr.get(job_id)
        except Exception:
            return make_envelope(
                "error",
                errors=[make_error("not_found", f"job not found: {job_id}",
                                   recoverable=False)],
            )
        if rec is None:
            return make_envelope(
                "error",
                errors=[make_error("not_found", f"job not found: {job_id}",
                                   recoverable=False)],
            )

        try:
            views = _extract_instrument_views(
                job_mgr.store, job_id, instrument=instrument,
            )
        except Exception as e:
            return make_envelope(
                "error",
                errors=[make_error(
                    "internal",
                    f"failed to build instrument view: {e}",
                    recoverable=True,
                )],
                job_id=job_id,
            )

        return make_envelope("ok", data={
            "job_id": job_id,
            "job_status": rec.status.value,
            "instruments": views,
            "instrument_count": len(views),
            "schema_version": "2.6",
        }, job_id=job_id)

    @mcp.tool()
    async def get_job_summary(job_id: str) -> dict:
        """完了 Job の構造化要約 (v0.8.2)

        get_job_result の補完: 「LLM が次の判断に使える」レベルの集約。

        - summary: total/completed/failed steps + targets, duration_s
        - key_results: 主要測定値 (現状は monitor latest を含む)
        - failures: target_id ごとの失敗詳細 (recoverable フラグ付き)
        - verify_summary: verify 試行数 / passed / failed
        - recommended_next_actions: retry_failed_targets / inspect_state など、
          客観的に導ける action のみ
        """
        try:
            rec = job_mgr.get(job_id)
        except Exception:
            return make_envelope(
                "error",
                errors=[make_error("not_found", f"job not found: {job_id}",
                                   recoverable=False)],
            )

        if not is_terminal(rec.status):
            return make_envelope(
                "running",
                data={
                    "job_id": job_id,
                    "status": rec.status.value,
                    "note": "Job is still running. Use get_job_live_view instead.",
                },
                job_id=job_id,
            )

        try:
            steps = job_mgr.store.list_steps(job_id)
        except Exception:
            steps = []
        try:
            target_runs = job_mgr.store.list_target_runs(job_id)
        except Exception:
            target_runs = []

        monitor_count = 0
        monitor_latest: dict | None = None
        try:
            monitor_count = job_mgr.store.count_monitor_data(job_id)
            if monitor_count > 0:
                latest = job_mgr.store.list_monitor_data(
                    job_id, limit=1,
                    offset=max(0, monitor_count - 1),
                )
                if latest:
                    monitor_latest = latest[0]
        except Exception:
            pass

        summary = build_run_summary(
            rec.to_dict(), steps, target_runs,
            monitor_count=monitor_count,
            monitor_latest=monitor_latest,
        )

        return make_envelope("ok", data=summary, job_id=job_id)
