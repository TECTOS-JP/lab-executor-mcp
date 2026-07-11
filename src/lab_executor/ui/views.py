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


def _seed_ctx_from_test_values(
    plan: Any, test_values: dict[str, Any] | None,
) -> dict[str, dict]:
    """dry-run のテスト値注入用に評価コンテキストを組む。

    test_values のキーは "steps.x" / "vars.y" / "params.z" / "env.w" 形式。
    名前空間を省いた裸のキーは steps.* とみなす (最も一般的な測定値注入)。
    """
    ctx: dict[str, dict] = {
        "params": dict(getattr(plan, "parameters", {}) or {}),
        "steps": {}, "vars": {}, "env": {},
    }
    for key, val in (test_values or {}).items():
        if "." in key:
            ns, name = key.split(".", 1)
            if ns in ctx:
                ctx[ns][name] = val
                continue
        ctx["steps"][key] = val
    return ctx


# dry-run で repeat count をイテレーション展開表示する上限 (超えたら body 1 回 + 省略)
DRYRUN_REPEAT_EXPAND_MAX = 5


def _copy_ctx(ctx: dict[str, dict]) -> dict[str, dict]:
    return {ns: dict(table) for ns, table in ctx.items()}


def _dryrun_step_row(
    st: Any, ctx: dict[str, dict], has_test: bool,
) -> dict[str, Any]:
    """1 つの IR Step を dry-run 表示用 row に変換する (SP-3 で再帰化)。

    branch / repeat のネスト steps は ``cases`` / ``body`` に再帰展開する。
    has_test の場合、compute の代入は ctx を mutate して後続へ伝播する
    (branch は採択 case のみ・repeat は表示イテレーション分)。
    """
    from lab_executor.utils.seq_expression import SeqExpressionError, evaluate

    step_type = getattr(st, "type", "command")
    row: dict[str, Any] = {
        "type": step_type,
        "command": getattr(st, "command", "") or "",
        "instrument": getattr(st, "instrument", None),
        "args": dict(getattr(st, "args", {}) or {}),
        "seconds": getattr(st, "seconds", None),
        "description": getattr(st, "description", "") or "",
    }

    # SP-2: 実行時解決引数
    deferred = dict(getattr(st, "deferred_args", {}) or {})
    if deferred:
        dinfo: list[dict[str, Any]] = []
        for arg, spec in deferred.items():
            entry: dict[str, Any] = {
                "arg": arg,
                "expr": spec.get("expr"),
                "min": spec.get("min"),
                "max": spec.get("max"),
                "range_declared": (
                    spec.get("min") is not None or spec.get("max") is not None
                ),
            }
            if has_test:
                try:
                    val = evaluate(spec["expr"], ctx)
                    entry["resolved"] = val
                    entry["in_range"] = (
                        (spec.get("min") is None or val >= spec["min"])
                        and (spec.get("max") is None or val <= spec["max"])
                    )
                except SeqExpressionError as e:
                    entry["error"] = str(e)
            else:
                entry["resolved"] = "deferred"
            dinfo.append(entry)
        row["deferred_args"] = dinfo

    # SP-1: compute
    if step_type == "compute":
        row["set"] = getattr(st, "set", "")
        row["expr"] = getattr(st, "expr", "")
        row["unit"] = getattr(st, "unit", "")
        if has_test:
            try:
                val = evaluate(st.expr, ctx)
                row["value"] = val
                ctx["vars"][st.set] = val
            except SeqExpressionError as e:
                row["error"] = str(e)

    # SP-3: guard
    elif step_type == "guard":
        row["expr"] = getattr(st, "expr", "")
        row["on_fail"] = getattr(st, "on_fail", "abort")
        row["message"] = getattr(st, "message", "") or ""
        if has_test:
            try:
                row["passed"] = bool(evaluate(st.expr, ctx))
            except SeqExpressionError as e:
                row["error"] = str(e)

    # SP-4: pause (dry-run では実行せず宣言内容のみ表示)
    elif step_type == "pause":
        from lab_executor.utils.seq_expression import interpolate_string
        row["message"] = (
            interpolate_string(getattr(st, "message", "") or "", ctx)
            if has_test else (getattr(st, "message", "") or "")
        )
        row["timeout_s"] = getattr(st, "timeout_s", None)
        row["on_timeout"] = getattr(st, "on_timeout", "safe_shutdown")
        expose_rows: list[dict[str, Any]] = []
        for expr in getattr(st, "expose", []) or []:
            entry: dict[str, Any] = {"expr": expr}
            if has_test:
                try:
                    entry["value"] = evaluate(expr, ctx)
                except SeqExpressionError as e:
                    entry["error"] = str(e)
            expose_rows.append(entry)
        row["expose"] = expose_rows

    # SP-3: branch — 全 case を展開表示。test_values があれば採択 case を併記
    elif step_type == "branch":
        cases_out: list[dict[str, Any]] = []
        taken_index: int | None = None
        for ci, case in enumerate(getattr(st, "cases", []) or []):
            centry: dict[str, Any] = {
                "case_index": ci,
                "when": case.when,
                "is_else": case.when is None,
            }
            if has_test and taken_index is None:
                if case.when is None:
                    centry["taken"] = True
                    taken_index = ci
                else:
                    try:
                        val = evaluate(case.when, ctx)
                        centry["when_value"] = val
                        if bool(val):
                            centry["taken"] = True
                            taken_index = ci
                        else:
                            centry["taken"] = False
                    except SeqExpressionError as e:
                        centry["error"] = str(e)
                        centry["taken"] = False
            # 各 case は ctx の copy で展開し、採択 case のみ親 ctx へ反映
            case_ctx = _copy_ctx(ctx)
            centry["steps"] = [
                _dryrun_step_row(s, case_ctx, has_test) for s in case.steps
            ]
            if has_test and centry.get("taken"):
                for ns in ("steps", "vars"):
                    ctx[ns].update(case_ctx[ns])
            cases_out.append(centry)
        row["cases"] = cases_out
        if has_test:
            row["taken_case"] = taken_index

    # SP-3: repeat — count が小さければイテレーション展開、大きければ省略表示
    elif step_type == "repeat":
        count = getattr(st, "count", None)
        row["count"] = count
        row["while"] = getattr(st, "while_expr", None)
        row["max_iterations"] = getattr(st, "max_iterations", None)
        body = list(getattr(st, "body", []) or [])
        if count is not None and count <= DRYRUN_REPEAT_EXPAND_MAX:
            iters: list[dict[str, Any]] = []
            for i in range(count):
                ctx["env"]["loop_index"] = i
                iters.append({
                    "loop_index": i,
                    "steps": [_dryrun_step_row(s, ctx, has_test) for s in body],
                })
            ctx["env"].pop("loop_index", None)
            row["iterations"] = iters
        else:
            # 省略表示: body を 1 回だけ (loop_index=0) 展開する
            ctx["env"]["loop_index"] = 0
            row["body"] = [_dryrun_step_row(s, ctx, has_test) for s in body]
            ctx["env"].pop("loop_index", None)
            row["iterations_omitted"] = True

    # capture 注記
    if getattr(st, "result_as", None):
        row["result_as"] = st.result_as
        row["value_path"] = getattr(st, "value_path", "") or ""

    return row


def dryrun_view(plan: Any, test_values: dict[str, Any] | None = None) -> dict[str, Any]:
    """recipe_to_plan の返り値 (Plan) を dry-run 表示用 dict に整える。

    展開された IR Step 列を「種別 / コマンド名 / instrument / 解決済み引数 /
    wait 秒数 / description」に整形する。

    v2.28.0 (SP-2): 実行時解決 (${...}) の引数は ``deferred_args`` として
    式・範囲宣言の有無を明示し、``test_values`` があれば解決値も表示する。
    compute ステップは式と (test_values があれば) 評価値を表示する。

    v2.29.0 (SP-3): branch は全 case を展開表示 (test_values があれば採択 case を
    併記)、repeat は count が小さければイテレーション展開 (大きければ省略表示)、
    guard は式と on_fail (test_values があれば判定結果) を表示する。

    Plan の import はしない (FastAPI 非依存を保つため型は Any で受ける)。
    """
    ctx = _seed_ctx_from_test_values(plan, test_values)
    has_test = bool(test_values)

    steps_out = [_dryrun_step_row(st, ctx, has_test) for st in plan.steps]

    return {
        "name": getattr(plan, "name", ""),
        "parameters": dict(getattr(plan, "parameters", {}) or {}),
        "required_resources": list(getattr(plan, "required_resources", []) or []),
        "step_count": len(steps_out),
        "steps": steps_out,
    }


def job_detail_view(
    job: dict[str, Any],
    steps: list[dict[str, Any]],
    events: list[dict[str, Any]],
    target_runs: list[dict[str, Any]],
    pause: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """ジョブ詳細の表示用 dict。

    - ``events`` は list_events の返り値 (新しい順)。timeline は
      normalize_event で正規化し、表示は **古い順** に並べ直す。
    - 終端ジョブなら build_run_summary を含める。
    - ``pause`` (v2.30.0 SP-4): 未解決 pause レコード。あれば phase="paused" +
      応答パネル用に detail["pause"] へ載せる。
    """
    status = job.get("status", "unknown")
    last_event_type = latest_event_kind(events)  # events[0].event_type
    outcome = compute_job_outcome(status, target_runs)
    phase = compute_current_phase(
        status,
        last_event_type,
        last_step_summary=job.get("last_step_summary"),
        job_outcome=outcome,
        pause_active=pause is not None,
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
        "pause": pause,
    }
