"""Web UI M1 (読み取り専用モニタ) のテスト。

fixture: tmp_path に JobStore (書き込み可) でシード → ReadOnlyJobStore /
create_app はそのパスを読む。UI は書き込み経路を持たないことを検証する。

シード内容:
- completed 1 件 (steps + events + result 付き)
- running 1 件 (step 途中)
- failed 1 件 (error_class 付き)
"""
from __future__ import annotations

import hashlib
import sqlite3

import pytest
from fastapi.testclient import TestClient

from lab_executor.job.state_machine import JobStatus
from lab_executor.job.store import JobStore
from lab_executor.ui.app import create_app
from lab_executor.ui.readonly_store import ReadOnlyJobStore, UiStoreError

COMPLETED_JOB = "job_done"
RUNNING_JOB = "job_run"
FAILED_JOB = "job_fail"


def _seed(db_path) -> None:
    """3 件のジョブを JobStore (書き込み可) でシードする。"""
    store = JobStore(db_path=db_path)
    try:
        # --- completed ---
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

        # --- running (step 途中) ---
        store.create_job(
            RUNNING_JOB, "agent_b", "GPIB0::2::INSTR",
            "ramp_voltage", {"to": 10.0},
        )
        store.transition_status(RUNNING_JOB, JobStatus.RUNNING, current_step_index=1)
        store.record_event(RUNNING_JOB, "job_started")
        store.record_step_started(RUNNING_JOB, 1, "measure_voltage")
        store.record_event(RUNNING_JOB, "step_started",
                           step_index=1, payload={"command": "measure_voltage"})

        # --- failed (error_class 付き) ---
        store.create_job(
            FAILED_JOB, "agent_c", "GPIB0::3::INSTR",
            "risky_recipe", {},
        )
        store.transition_status(FAILED_JOB, JobStatus.RUNNING, current_step_index=0)
        store.record_event(FAILED_JOB, "job_started")
        store.transition_status(
            FAILED_JOB, JobStatus.FAILED,
            error_class="hardware",
            result={"success": False},
        )
        store.record_event(FAILED_JOB, "job_failed",
                           payload={"error_class": "hardware"})
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
# HTML routes
# ============================================================


def test_dashboard_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert COMPLETED_JOB in body
    assert RUNNING_JOB in body
    assert FAILED_JOB in body
    # recipe 名
    assert "safe_output_on" in body
    # 状態ラベル
    assert "completed" in body
    assert "running" in body
    assert "failed" in body


def test_job_detail_completed(client):
    r = client.get(f"/jobs/{COMPLETED_JOB}")
    assert r.status_code == 200
    body = r.text
    # timeline に step_completed 由来の行 (title = "step completed (...)")
    assert "step completed" in body
    # summary セクションあり
    assert "run-summary" in body
    assert "job_outcome" in body


def test_job_detail_running(client):
    r = client.get(f"/jobs/{RUNNING_JOB}")
    assert r.status_code == 200
    body = r.text
    # phase は running 系
    assert "running_step" in body or "phase: running" in body
    # summary なし (終端でない)
    assert "run-summary" not in body


def test_job_not_found(client):
    r = client.get("/jobs/nonexistent")
    assert r.status_code == 404


# ============================================================
# JSON API
# ============================================================


def test_api_jobs_shape(client):
    r = client.get("/api/jobs")
    assert r.status_code == 200
    data = r.json()
    jobs = data["jobs"]
    assert len(jobs) == 3
    keys = set(jobs[0].keys())
    for expected in ("job_id", "status", "phase", "recipe", "status_color"):
        assert expected in keys


def test_api_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    data = r.json()
    # last_write_at が ISO8601 (fromisoformat がパースできる)
    from datetime import datetime
    assert data["last_write_at"] is not None
    datetime.fromisoformat(data["last_write_at"])
    # active_jobs は running 1 件のみ
    assert data["active_jobs"] == 1
    assert data["ui_version"] == "m1"


def test_missing_db_friendly(tmp_path):
    # 存在しないパスで create_app → GET / が 500 でなく案内表示 (503)。
    missing = tmp_path / "does_not_exist.sqlite"
    app = create_app(missing)
    c = TestClient(app, raise_server_exceptions=False)
    r = c.get("/")
    assert r.status_code == 503
    assert "見つかりません" in r.text
    # api ルートは JSON 503
    rj = c.get("/api/jobs")
    assert rj.status_code == 503
    assert rj.json()["error"] == "state_db_unavailable"


# ============================================================
# read-only enforcement
# ============================================================


def test_readonly_enforced(db_path):
    ro = ReadOnlyJobStore(db_path)

    # 1) ReadOnlyJobStore の接続で INSERT を試みると OperationalError。
    conn = ro._connect()
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(
                "INSERT INTO jobs (job_id, status, current_step_index, "
                "created_at, updated_at) VALUES "
                "('x', 'queued', -1, '2026-01-01T00:00:00', "
                "'2026-01-01T00:00:00')"
            )
    finally:
        conn.close()

    # 2) 一連の GET 後に DB ファイルの内容ハッシュが不変であること。
    def _digest(p):
        return hashlib.sha256(p.read_bytes()).hexdigest()

    before = _digest(db_path)
    app = create_app(db_path)
    c = TestClient(app, raise_server_exceptions=False)
    c.get("/")
    c.get(f"/jobs/{COMPLETED_JOB}")
    c.get(f"/jobs/{RUNNING_JOB}")
    c.get("/api/jobs")
    c.get("/api/health")
    c.get("/partials/jobs-table")
    c.get(f"/partials/jobs/{RUNNING_JOB}/timeline")
    after = _digest(db_path)
    assert before == after


def test_readonly_store_missing_db(tmp_path):
    ro = ReadOnlyJobStore(tmp_path / "nope.sqlite")
    with pytest.raises(UiStoreError):
        ro.list_jobs()


# ============================================================
# partials
# ============================================================


def test_partials_render(client):
    r1 = client.get("/partials/jobs-table")
    assert r1.status_code == 200
    assert COMPLETED_JOB in r1.text

    r2 = client.get(f"/partials/jobs/{RUNNING_JOB}/timeline")
    assert r2.status_code == 200
