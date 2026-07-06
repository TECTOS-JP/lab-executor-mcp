"""Web UI M4 (UI プロセス側: コントロールプレーン proxy) のテスト。

fake ControlClient を create_app に注入し、UI が実行系操作を **プロキシ** する
こと・token をブラウザに渡さないこと・control 無効時にボタンが消えることを
検証する。実 uvicorn は立てない (無限ループ・ポート衝突を避ける)。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from lab_executor.job.store import JobStore
from lab_executor.ui.app import create_app


@pytest.fixture
def db_path(tmp_path):
    p = tmp_path / "state.sqlite"
    store = JobStore(db_path=p)
    # job_detail 画面用に非終端ジョブを 1 件シードする。
    from lab_executor.job.state_machine import JobStatus

    store.create_job(
        "job_live", "agent", "MOCK::INSTR", "quick", {"v": 5.0},
    )
    store.transition_status(
        "job_live", JobStatus.RUNNING, current_step_index=0
    )
    store.close()
    return p


class _FakeControl:
    """テスト用 ControlClient。転送内容を記録する。"""

    def __init__(self, available_info=None):
        self._available = available_info
        self.calls = []

    def available(self):
        return self._available

    def cancel(self, job_id, cancel_mode="after_current_step",
               timeout_s=30.0, *, owner="web-ui"):
        self.calls.append(
            ("cancel", job_id, cancel_mode, timeout_s)
        )
        if self._available is None:
            return 503, {"error": "control_unavailable"}
        return 200, {
            "job_id": job_id,
            "status": "cancelled",
            "is_terminal": True,
            "cancel_mode": cancel_mode,
            "last_step_summary": "cancelled",
        }

    def start_recipe(self, resource_name, recipe_name, parameters=None,
                     *, owner="web-ui", job_timeout_s=None):
        self.calls.append(
            ("start", resource_name, recipe_name, parameters)
        )
        if self._available is None:
            return 503, {"error": "control_unavailable"}
        return 200, {
            "job_id": "job_new",
            "status": "queued",
            "resource_name": resource_name,
            "recipe": recipe_name,
            "created_at": "2026-07-06T00:00:00+00:00",
        }


AVAILABLE = {"backend_id": "mock", "pid": 1, "started_at": "t"}


def _client(db_path, control):
    app = create_app(db_path, control_client=control)
    return TestClient(app, raise_server_exceptions=False)


# ============================================================
# status
# ============================================================


def test_status_unavailable_without_file(db_path):
    control = _FakeControl(available_info=None)
    client = _client(db_path, control)
    r = client.get("/api/control/status")
    assert r.status_code == 200
    assert r.json()["available"] is False


def test_status_available(db_path):
    control = _FakeControl(available_info=AVAILABLE)
    client = _client(db_path, control)
    r = client.get("/api/control/status")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True
    assert body["backend_id"] == "mock"


# ============================================================
# proxy: cancel / start
# ============================================================


def test_proxy_cancel_and_start(db_path):
    control = _FakeControl(available_info=AVAILABLE)
    client = _client(db_path, control)

    r = client.post(
        "/api/control/jobs/job_live/cancel",
        json={"cancel_mode": "immediate"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"
    assert ("cancel", "job_live", "immediate", 30.0) in control.calls

    r2 = client.post(
        "/api/control/start-recipe",
        json={
            "resource_name": "MOCK::INSTR",
            "recipe_name": "quick",
            "parameters": {"v": 5.0},
        },
    )
    assert r2.status_code == 200
    assert r2.json()["job_id"] == "job_new"
    assert (
        "start", "MOCK::INSTR", "quick", {"v": 5.0}
    ) in control.calls


def test_proxy_returns_503_when_unavailable(db_path):
    control = _FakeControl(available_info=None)
    client = _client(db_path, control)
    r = client.post(
        "/api/control/jobs/job_live/cancel",
        json={"cancel_mode": "immediate"},
    )
    assert r.status_code == 503


def test_proxy_never_exposes_token(db_path):
    # UI レスポンス body に token が一切含まれないこと (ブラウザに渡さない)。
    control = _FakeControl(available_info=AVAILABLE)
    client = _client(db_path, control)
    r = client.get("/api/control/status")
    assert "token" not in r.text.lower()


# ============================================================
# 非 JSON 拒否
# ============================================================


def test_proxy_rejects_non_json(db_path):
    control = _FakeControl(available_info=AVAILABLE)
    client = _client(db_path, control)
    r = client.post(
        "/api/control/jobs/job_live/cancel",
        content="cancel_mode=immediate",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 415

    r2 = client.post(
        "/api/control/start-recipe",
        content="x",
        headers={"Content-Type": "text/plain"},
    )
    assert r2.status_code == 415


def test_start_recipe_missing_fields(db_path):
    control = _FakeControl(available_info=AVAILABLE)
    client = _client(db_path, control)
    r = client.post(
        "/api/control/start-recipe",
        json={"resource_name": "MOCK::INSTR"},
    )
    assert r.status_code == 422


# ============================================================
# HTML: ボタンの表示・非表示
# ============================================================


def test_job_detail_shows_cancel_buttons(db_path):
    control = _FakeControl(available_info=AVAILABLE)
    client = _client(db_path, control)
    r = client.get("/jobs/job_live")
    assert r.status_code == 200
    assert "cancel-panel" in r.text
    assert 'data-mode="immediate"' in r.text
    assert 'data-mode="safe_shutdown"' in r.text


def test_job_detail_hides_cancel_buttons_when_unavailable(db_path):
    control = _FakeControl(available_info=None)
    client = _client(db_path, control)
    r = client.get("/jobs/job_live")
    assert r.status_code == 200
    assert "cancel-panel" not in r.text


def test_recipes_run_form_shown_when_available(db_path, tmp_path):
    # edit_dir 有効 + control available → 実行フォームが出る。
    import shutil
    from pathlib import Path

    example = (
        Path(__file__).resolve().parents[1]
        / "examples" / "instruments" / "kikusui_pmx35_3a.yaml"
    )
    edit_dir = tmp_path / "defs"
    edit_dir.mkdir()
    shutil.copy(example, edit_dir / "psu.yaml")

    control = _FakeControl(available_info=AVAILABLE)
    app = create_app(db_path, edit_dir=edit_dir, control_client=control)
    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/recipes")
    assert r.status_code == 200
    assert "run-recipe-panel" in r.text

    # control 無効時はフォームが消える
    control2 = _FakeControl(available_info=None)
    app2 = create_app(db_path, edit_dir=edit_dir, control_client=control2)
    client2 = TestClient(app2, raise_server_exceptions=False)
    r2 = client2.get("/recipes")
    assert r2.status_code == 200
    assert "run-recipe-panel" not in r2.text


# ============================================================
# monitor / edit ルートが M4 でも従来どおり
# ============================================================


def test_monitor_routes_unaffected(db_path):
    control = _FakeControl(available_info=None)
    client = _client(db_path, control)
    assert client.get("/").status_code == 200
    assert client.get("/api/jobs").status_code == 200
