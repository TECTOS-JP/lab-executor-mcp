"""Web UI M2 (SSE ライブ更新 + スイープグラフ + N+1 解消) のテスト。

fixture は M1 と同じく tmp_path に JobStore (書き込み可) でシード →
ReadOnlyJobStore / create_app はそのパスを読む。UI は書き込み経路を持たない。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from lab_executor.job.state_machine import JobStatus
from lab_executor.job.store import JobStore
from lab_executor.ui.app import _sse_frame, create_app
from lab_executor.ui.readonly_store import ReadOnlyJobStore
from lab_executor.ui.views import sweep_chart_view

COMPLETED_JOB = "job_done"
RUNNING_JOB = "job_run"
FAILED_JOB = "job_fail"
SWEEP_JOB = "job_sweep"


def _seed_sweep_step(store, job_id, *, step_index, instrument, command,
                     status="ok", raw_response=None, parsed=None,
                     sweep_index=None, sweep_param=None, sweep_value=None):
    """test_v2_7 のシード形式に合わせて sweep 文脈付き step を書き込む。"""
    row_id = store.record_step_started(job_id, step_index, "command")
    result = {"command": command, "instrument": instrument}
    if raw_response is not None:
        result["raw_response"] = raw_response
    if parsed is not None:
        result["parsed"] = parsed
    if sweep_index is not None:
        result["sweep_index"] = sweep_index
        result["sweep_param"] = sweep_param
        result["sweep_value"] = sweep_value
    result["success"] = (status == "ok")
    store.record_step_completed(
        row_id, status=status,
        result=result if status == "ok" else None,
        error=result if status != "ok" else None,
    )


def _seed(db_path) -> None:
    """M1 と同等の 3 件 + sweep payload 付き completed 1 件をシードする。"""
    store = JobStore(db_path=db_path)
    try:
        # --- completed (M1 と同様) ---
        store.create_job(
            COMPLETED_JOB, "agent_a", "GPIB0::1::INSTR",
            "safe_output_on", {"target_v": 5.0},
        )
        store.transition_status(COMPLETED_JOB, JobStatus.RUNNING, current_step_index=0)
        store.record_event(COMPLETED_JOB, "job_started")
        row = store.record_step_started(COMPLETED_JOB, 0, "set_voltage")
        store.record_event(COMPLETED_JOB, "step_started",
                           step_index=0, payload={"command": "set_voltage"})
        store.record_step_completed(row, "ok", result={"ok": True})
        store.record_event(COMPLETED_JOB, "step_completed",
                           step_index=0, payload={"command": "set_voltage"})
        store.transition_status(
            COMPLETED_JOB, JobStatus.COMPLETED,
            result={"success": True, "steps_executed": [{"step": 0}]},
        )
        store.record_event(COMPLETED_JOB, "job_completed")

        # --- running ---
        store.create_job(
            RUNNING_JOB, "agent_b", "GPIB0::2::INSTR",
            "ramp_voltage", {"to": 10.0},
        )
        store.transition_status(RUNNING_JOB, JobStatus.RUNNING, current_step_index=1)
        store.record_event(RUNNING_JOB, "job_started")
        store.record_step_started(RUNNING_JOB, 1, "measure_voltage")
        store.record_event(RUNNING_JOB, "step_started",
                           step_index=1, payload={"command": "measure_voltage"})

        # --- failed ---
        store.create_job(
            FAILED_JOB, "agent_c", "GPIB0::3::INSTR",
            "risky_recipe", {},
        )
        store.transition_status(FAILED_JOB, JobStatus.RUNNING, current_step_index=0)
        store.record_event(FAILED_JOB, "job_started")
        store.transition_status(
            FAILED_JOB, JobStatus.FAILED,
            error_class="hardware", result={"success": False},
        )
        store.record_event(FAILED_JOB, "job_failed",
                           payload={"error_class": "hardware"})

        # --- sweep (completed, sweep payload 付き 3 点) ---
        store.create_job(
            SWEEP_JOB, "agent_d", "GPIB0::4::INSTR",
            "iv_sweep", {"points": 3},
        )
        store.transition_status(SWEEP_JOB, JobStatus.RUNNING, current_step_index=0)
        store.record_event(SWEEP_JOB, "job_started")
        # 3 sweep 点 x (psu measure + dmm read)。1 点は value_numeric None。
        _seed_sweep_step(store, SWEEP_JOB, step_index=1, instrument="psu1",
                         command="measure_voltage", raw_response="+1.0E+0",
                         parsed={"value_numeric": 1.0},
                         sweep_index=0, sweep_param="v", sweep_value=1.0)
        _seed_sweep_step(store, SWEEP_JOB, step_index=2, instrument="dmm1",
                         command="read_measurement", raw_response="x",
                         parsed={"value_numeric": 10.0},
                         sweep_index=0, sweep_param="v", sweep_value=1.0)
        _seed_sweep_step(store, SWEEP_JOB, step_index=3, instrument="psu1",
                         command="measure_voltage", raw_response="+2.0E+0",
                         parsed={"value_numeric": 2.0},
                         sweep_index=1, sweep_param="v", sweep_value=2.0)
        _seed_sweep_step(store, SWEEP_JOB, step_index=4, instrument="dmm1",
                         command="read_measurement", raw_response="y",
                         parsed={},  # value_numeric なし → None
                         sweep_index=1, sweep_param="v", sweep_value=2.0)
        _seed_sweep_step(store, SWEEP_JOB, step_index=5, instrument="psu1",
                         command="measure_voltage", raw_response="+3.0E+0",
                         parsed={"value_numeric": 3.0},
                         sweep_index=2, sweep_param="v", sweep_value=3.0)
        _seed_sweep_step(store, SWEEP_JOB, step_index=6, instrument="dmm1",
                         command="read_measurement", raw_response="z",
                         parsed={"value_numeric": 30.0},
                         sweep_index=2, sweep_param="v", sweep_value=3.0)
        store.transition_status(
            SWEEP_JOB, JobStatus.COMPLETED,
            result={"success": True},
        )
        store.record_event(SWEEP_JOB, "job_completed")
    finally:
        store.close()


@pytest.fixture
def db_path(tmp_path):
    p = tmp_path / "state.sqlite"
    _seed(p)
    return p


@pytest.fixture
def client(db_path):
    app = create_app(db_path)
    return TestClient(app, raise_server_exceptions=False)


# ============================================================
# N+1 解消
# ============================================================


def test_list_jobs_with_last_event_equivalence(db_path):
    """新メソッドの結果が「list_jobs + 各 job の list_events(limit=1)」と一致。"""
    ro = ReadOnlyJobStore(db_path)
    combined = ro.list_jobs_with_last_event(limit=100)

    # 旧 N+1 方式で等価な参照を組む。
    jobs = ro.list_jobs(limit=100)
    reference = []
    for job in jobs:
        events = ro.list_events(job["job_id"], limit=1)
        last = events[0]["event_type"] if events else None
        row = dict(job)
        row["last_event_type"] = last
        reference.append(row)

    assert [r["job_id"] for r in combined] == [r["job_id"] for r in reference]
    for got, exp in zip(combined, reference):
        assert got["last_event_type"] == exp["last_event_type"]
        # 元 record のキーも保持されていること。
        assert got["job_id"] == exp["job_id"]
        assert got["status"] == exp["status"]


# ============================================================
# _sse_frame ヘルパ
# ============================================================


def test_sse_frame_helper():
    # 単一行
    frame = _sse_frame("jobs-table", "hello")
    assert frame == "event: jobs-table\ndata: hello\n\n"

    # 複数行: 各行に data: プレフィックス。
    multi = _sse_frame("timeline", "line1\nline2\nline3")
    assert multi == (
        "event: timeline\n"
        "data: line1\n"
        "data: line2\n"
        "data: line3\n\n"
    )


# ============================================================
# SSE ルート
# ============================================================


def _read_stream(client, url):
    """SSE ストリームを最後まで読んで返す。

    無限ストリームは Starlette TestClient でクローズをジェネレータに
    伝えられない (途中 break してもデッドロックする) ため、URL 側で
    ``?max_cycles=N`` を付けて有限化して呼ぶこと。
    """
    with client.stream("GET", url) as r:
        assert r.status_code == 200
        assert "text/event-stream" in r.headers["content-type"]
        assert r.headers.get("cache-control") == "no-cache"
        buf = ""
        for chunk in r.iter_text():
            buf += chunk
    return buf


def test_sse_dashboard_first_frame(client):
    # max_cycles=3 はテスト用有限化 (本番はブラウザ EventSource が無限に受ける)。
    buf = _read_stream(client, "/sse/dashboard?max_cycles=3")
    assert "event: jobs-table" in buf
    # シードした job_id がフラグメントに含まれる。
    assert COMPLETED_JOB in buf


def test_sse_job_terminal_closes(client):
    # 終端 (completed) ジョブの SSE は最終フラグメント送信後にストリーム終了。
    with client.stream("GET", f"/sse/jobs/{COMPLETED_JOB}") as r:
        assert r.status_code == 200
        buf = ""
        for chunk in r.iter_text():
            buf += chunk
        # ストリームは自然終了する (無限ループしない)。
    assert "event: timeline" in buf
    # 終端後に長い retry を送っている。
    assert "retry: 3600000" in buf


# ============================================================
# sweep_chart_view (純関数)
# ============================================================


def test_sweep_chart_view_series(db_path):
    from lab_executor.tools.observation import _extract_sweep_views

    ro = ReadOnlyJobStore(db_path)
    points = _extract_sweep_views(ro, SWEEP_JOB)
    view = sweep_chart_view(points)

    assert view is not None
    # x は sweep_value 昇順。
    assert view["x_label"] == "sweep_value"
    assert view["x"] == [1.0, 2.0, 3.0]
    labels = {s["label"] for s in view["series"]}
    assert "psu1: measure_voltage" in labels
    assert "dmm1: read_measurement" in labels
    # psu series は 3 点とも数値。
    psu = next(s for s in view["series"] if s["label"] == "psu1: measure_voltage")
    assert psu["values"] == [1.0, 2.0, 3.0]
    # dmm series は 2 点目 (sweep_index=1) が None (value_numeric 欠落) を保持。
    dmm = next(s for s in view["series"] if s["label"] == "dmm1: read_measurement")
    assert dmm["values"] == [10.0, None, 30.0]


def test_sweep_chart_view_empty():
    assert sweep_chart_view([]) is None


# ============================================================
# /api/jobs/{id}/sweep
# ============================================================


def test_api_sweep_endpoint(client):
    r = client.get(f"/api/jobs/{SWEEP_JOB}/sweep")
    assert r.status_code == 200
    payload = r.json()
    sweep = payload["sweep"]
    assert sweep is not None
    assert "x" in sweep and "series" in sweep and "x_label" in sweep
    assert len(sweep["x"]) == 3

    # sweep 無しジョブは null。
    r2 = client.get(f"/api/jobs/{COMPLETED_JOB}/sweep")
    assert r2.status_code == 200
    assert r2.json()["sweep"] is None

    # 不在ジョブは 404。
    r3 = client.get("/api/jobs/nonexistent/sweep")
    assert r3.status_code == 404


# ============================================================
# ダッシュボード / 静的配信
# ============================================================


def test_dashboard_has_sse_attrs(client):
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert 'sse-connect="/sse/dashboard"' in body
    assert 'hx-ext="sse"' in body
    # 旧 2 秒ポーリング属性は無い。
    assert 'hx-trigger="every 2s"' not in body

    # ベンダリングした静的資産が 200 で配信される。
    for path in (
        "/static/vendor/uPlot.iife.min.js",
        "/static/vendor/uPlot.min.css",
        "/static/vendor/htmx-sse.js",
    ):
        rs = client.get(path)
        assert rs.status_code == 200, path


def test_job_detail_sweep_chart_embedded(client):
    r = client.get(f"/jobs/{SWEEP_JOB}")
    assert r.status_code == 200
    body = r.text
    assert 'id="sweep-data"' in body
    assert "new uPlot" in body
    # 埋め込み JSON がパース可能。
    start = body.index('id="sweep-data">') + len('id="sweep-data">')
    end = body.index("</script>", start)
    parsed = json.loads(body[start:end])
    assert "series" in parsed
