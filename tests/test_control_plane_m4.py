"""Web UI M4 (コントロールプレーン: ジョブキャンセル + レシピ実行) のテスト。

serve プロセス側のコントロールプレーンを Starlette TestClient で in-process に
検証する。JobManager は MockBackend 相当の MagicMock write/query + recipes 付き
InstrumentSession で構成する (test_job_manager / test_dsl の実績パターン)。

絶対制約の確認:
- token 検証 (constant-time)、不正 mode の 422。
- override_safety は body に来ても False 固定。
- audit に tool_name="control.*" / client_id="control-plane" が残る。
- 無限ループを TestClient に食わせない (cancel は timeout_s を小さく、job は
  すぐ終端する recipe を使う)。
"""
from __future__ import annotations

import textwrap
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from starlette.testclient import TestClient

from lab_executor.audit import AuditStore
from lab_executor.control_plane import (
    create_control_app,
    default_control_path,
    read_control_file,
    remove_control_file,
    write_control_file,
)
from lab_executor.job import JobManager, JobStore
from lab_executor.job.state_machine import JobStatus
from lab_executor.models.instrument_def import InstrumentDefinition
from lab_visa_mcp.session_manager import InstrumentSession

RESOURCE = "MOCK::INSTR"
TOKEN = "a" * 64

SAMPLE_YAML = """
metadata:
  manufacturer: "Test"
  model: "PSU"
commands:
  reset:
    scpi: "*RST"
    type: "write"
  set_voltage:
    scpi: "VOLT {voltage}"
    type: "write"
    parameters:
      - { name: voltage, type: float, range: [0, 100] }
recipes:
  quick:
    parameters:
      - { name: v, type: float }
    steps:
      - { command: "reset" }
      - { command: "set_voltage", args: { voltage: "$v" } }
  slow:
    parameters:
      - { name: w, type: float, default: 2.0 }
    steps:
      - { command: "reset" }
      - wait: { seconds: "$w" }
      - { command: "set_voltage", args: { voltage: 1 } }
"""


@pytest.fixture
def job_mgr(tmp_path, monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    visa = MagicMock()
    visa.write = AsyncMock(return_value=None)
    visa.query = AsyncMock(return_value="+1.0")

    d = InstrumentDefinition(**yaml.safe_load(textwrap.dedent(SAMPLE_YAML)))
    session = InstrumentSession(
        resource_name=RESOURCE,
        idn_response="<test>",
        idn_parsed={"manufacturer": "Test", "model": "PSU"},
        definition=d,
    )

    class _SM:
        def get_session(self, name):
            return session if name == RESOURCE else None

    store = JobStore(db_path=tmp_path / "state.sqlite")
    mgr = JobManager(backend=visa, session_mgr=_SM(), store=store)
    yield mgr
    store.close()


@pytest.fixture
def client(job_mgr):
    app = create_control_app(job_mgr, token=TOKEN, backend_id="mock")
    return TestClient(app, raise_server_exceptions=False)


def _auth(headers=None):
    h = {"X-Control-Token": TOKEN}
    if headers:
        h.update(headers)
    return h


# ============================================================
# token
# ============================================================


def test_health_requires_token(client):
    # token 無し → 401
    assert client.get("/control/health").status_code == 401
    # 不一致 → 401
    r = client.get(
        "/control/health", headers={"X-Control-Token": "b" * 64}
    )
    assert r.status_code == 401
    # 一致 → 200
    r2 = client.get("/control/health", headers=_auth())
    assert r2.status_code == 200
    body = r2.json()
    assert body["ok"] is True
    assert body["backend_id"] == "mock"
    assert "pid" in body and "started_at" in body


# ============================================================
# cancel
# ============================================================


def test_cancel_invalid_mode(client):
    r = client.post(
        "/control/jobs/whatever/cancel",
        headers=_auth(),
        json={"cancel_mode": "bogus"},
    )
    assert r.status_code == 422
    assert "valid" in r.json()


@pytest.mark.asyncio
async def test_cancel_running_job(job_mgr):
    # start と cancel を **同一 event loop** で回す (JobManager の runtime task
    # は起動した loop に属するため)。httpx ASGITransport でルートを in-process
    # に叩き、TestClient の別スレッド portal を避ける。
    import asyncio

    import httpx

    app = create_control_app(job_mgr, token=TOKEN, backend_id="mock")
    transport = httpx.ASGITransport(app=app)
    rec = await job_mgr.start_recipe_job(RESOURCE, "slow", {"w": 30.0})
    # wait step に入るまで少し待つ (running/waiting を確実にする)。
    for _ in range(20):
        cur = job_mgr.get(rec.job_id)
        if cur.status in (JobStatus.RUNNING, JobStatus.WAITING):
            break
        await asyncio.sleep(0.02)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://ctl"
    ) as ac:
        r = await ac.post(
            f"/control/jobs/{rec.job_id}/cancel",
            headers=_auth(),
            json={"cancel_mode": "after_current_step", "timeout_s": 5.0},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["job_id"] == rec.job_id
    assert body["is_terminal"] is True
    assert body["status"] in ("cancelled", "completed")


# ============================================================
# start-recipe
# ============================================================


def test_start_recipe_creates_job(client, job_mgr):
    r = client.post(
        "/control/jobs/start-recipe",
        headers=_auth(),
        json={
            "resource_name": RESOURCE,
            "recipe_name": "quick",
            "parameters": {"v": 5.0},
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["job_id"].startswith("job_")
    assert body["recipe"] == "quick"
    # store に載っている
    rec = job_mgr.get(body["job_id"])
    assert rec.recipe == "quick"


def test_start_recipe_ignores_override_safety(client, job_mgr):
    captured = {}
    orig = job_mgr.start_recipe_job

    async def _spy(resource_name, recipe_name, parameters, **kwargs):
        captured["override_safety"] = kwargs.get("override_safety")
        captured["override_reason"] = kwargs.get("override_reason")
        return await orig(resource_name, recipe_name, parameters, **kwargs)

    job_mgr.start_recipe_job = _spy  # type: ignore[assignment]
    r = client.post(
        "/control/jobs/start-recipe",
        headers=_auth(),
        json={
            "resource_name": RESOURCE,
            "recipe_name": "quick",
            "parameters": {"v": 5.0},
            "override_safety": True,
            "override_reason": "should be ignored",
        },
    )
    assert r.status_code == 200
    # body に True を入れても False 固定で JobManager に渡る
    assert captured["override_safety"] is False


def test_start_recipe_missing_fields(client):
    r = client.post(
        "/control/jobs/start-recipe",
        headers=_auth(),
        json={"resource_name": RESOURCE},
    )
    assert r.status_code == 422


# ============================================================
# audit
# ============================================================


def test_audit_recorded(client, job_mgr):
    # start
    client.post(
        "/control/jobs/start-recipe",
        headers=_auth(),
        json={
            "resource_name": RESOURCE,
            "recipe_name": "quick",
            "parameters": {"v": 5.0},
        },
    )
    # cancel (存在しない job でも audit の cancel_requested は残る)
    client.post(
        "/control/jobs/job_absent/cancel",
        headers=_auth(),
        json={"cancel_mode": "after_current_step", "timeout_s": 1.0},
    )
    audit = AuditStore(job_mgr.store)
    events, _ = audit.query(limit=100, include_details=True)
    # control-plane 由来の行だけ抽出 (JobManager 内部の cancel audit も混ざる)。
    cp = [e for e in events if e["client_id"] == "control-plane"]
    cp_tools = {e["tool_name"] for e in cp}
    assert "control.start_recipe_job" in cp_tools
    assert "control.cancel_job" in cp_tools
    # 全 control-plane 行の client_id は必ず "control-plane"。
    assert all(e["client_id"] == "control-plane" for e in cp)


# ============================================================
# control.json roundtrip
# ============================================================


def test_control_file_roundtrip(tmp_path):
    p = tmp_path / "control.json"
    write_control_file(
        p, url="http://127.0.0.1:8300", token="deadbeef",
        pid=1234, backend_id="mock",
    )
    data = read_control_file(p)
    assert data is not None
    assert data["url"] == "http://127.0.0.1:8300"
    assert data["token"] == "deadbeef"
    assert data["pid"] == 1234
    assert data["backend_id"] == "mock"
    assert "started_at" in data

    remove_control_file(p)
    assert read_control_file(p) is None
    # 二重 remove は無害
    remove_control_file(p)


def test_control_file_broken_json_is_none(tmp_path):
    p = tmp_path / "control.json"
    p.write_text("{ not json", encoding="utf-8")
    assert read_control_file(p) is None
    # 必須キー欠落も None
    p.write_text('{"url": ""}', encoding="utf-8")
    assert read_control_file(p) is None


def test_default_control_path_next_to_state_db(tmp_path, monkeypatch):
    monkeypatch.setenv("VISA_MCP_STATE_DB", str(tmp_path / "state.sqlite"))
    assert default_control_path() == tmp_path / "control.json"


# ============================================================
# serve --state-db (compose_server store_path)
# ============================================================


def _register_mock_session(job_mgr, resource=RESOURCE):
    """compose_server の session facade に recipes 付き session を登録する。"""
    d = InstrumentDefinition(**yaml.safe_load(textwrap.dedent(SAMPLE_YAML)))
    session = InstrumentSession(
        resource_name=resource,
        idn_response="<test>",
        idn_parsed={"manufacturer": "Test", "model": "PSU"},
        definition=d,
    )
    job_mgr.session_manager.register_session(resource, session)


@pytest.mark.asyncio
async def test_compose_server_store_path_persists_jobs(
    tmp_path, monkeypatch
):
    """store_path 指定時、start した job が別プロセス相当の新規 JobStore
    (同パス) から読める (= lab-executor ui のモニタに見える E2E ループ)。"""
    import asyncio

    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    from lab_executor.server import compose_server

    db = tmp_path / "s.sqlite"
    _mcp, job_mgr = compose_server(store_path=db)
    _register_mock_session(job_mgr)

    rec = await job_mgr.start_recipe_job(RESOURCE, "quick", {"v": 5.0})
    # 終端まで待つ (quick recipe はすぐ完了する)。
    for _ in range(100):
        cur = job_mgr.get(rec.job_id)
        if cur.status in (JobStatus.COMPLETED, JobStatus.FAILED):
            break
        await asyncio.sleep(0.05)

    # 別プロセス相当: 同じパスの新規 JobStore から読めること。
    other = JobStore(db_path=db)
    try:
        row = other.get(rec.job_id)
        assert row is not None
        assert row.recipe == "quick"
        assert row.status == JobStatus.COMPLETED
    finally:
        other.close()


def test_compose_server_default_stays_in_memory(tmp_path, monkeypatch):
    """store_path 省略時は従来どおり in-memory で、default_store_path() の
    ファイルは作成も変更もされない。"""
    default_db = tmp_path / "default_state.sqlite"
    monkeypatch.setenv("VISA_MCP_STATE_DB", str(default_db))
    from lab_executor.server import compose_server

    _mcp, job_mgr = compose_server()
    # JobManager は生成済みだが、default_store_path() のファイルは汚れない。
    assert not default_db.exists()
    # store は in-memory (jobs は空リストを返せる)。
    assert job_mgr.list_jobs(limit=5) == []


# ============================================================
# v2.24.0: 公開 API run_mcp_with_control / resolve_control_port
# ============================================================


def test_public_api_exists():
    """v2.24.0: runner / port resolver が公開 API として import できる。"""
    from lab_executor import control_plane

    assert hasattr(control_plane, "run_mcp_with_control")
    assert hasattr(control_plane, "resolve_control_port")


def test_resolve_control_port_cli_and_env(monkeypatch):
    from lab_executor.control_plane import resolve_control_port

    # CLI 値優先
    monkeypatch.setenv("LAB_EXECUTOR_CONTROL_PORT", "9999")
    assert resolve_control_port(8300) == 8300
    # CLI None → env
    assert resolve_control_port(None) == 9999
    # env 未設定 → None
    monkeypatch.delenv("LAB_EXECUTOR_CONTROL_PORT", raising=False)
    assert resolve_control_port(None) is None
    # env 空文字 → None
    monkeypatch.setenv("LAB_EXECUTOR_CONTROL_PORT", "")
    assert resolve_control_port(None) is None
    # env 非整数 → None (警告は stderr)
    monkeypatch.setenv("LAB_EXECUTOR_CONTROL_PORT", "notanint")
    assert resolve_control_port(None) is None


@pytest.mark.asyncio
async def test_run_mcp_with_control_writes_actual_port(
    job_mgr, tmp_path
):
    """v2.24.0: port=0 で uvicorn を実起動し、実ポートが control.json に
    書かれることを検証する。MCP は run_async をすぐ返す fake で代替し、
    uvicorn 起動は短時間 (ソケット bind 直後に停止)。"""
    import asyncio

    from lab_executor.control_plane import (
        read_control_file,
        run_mcp_with_control,
    )

    ctl_path = tmp_path / "control.json"
    started = asyncio.Event()

    class _FakeMCP:
        async def run_async(self, transport="stdio"):
            # control.json が書かれるまで並走を維持する。
            started.set()
            await asyncio.sleep(3600)

    async def _run():
        await run_mcp_with_control(
            _FakeMCP(), job_mgr, 0,
            backend_id="mock", control_path=ctl_path,
        )

    task = asyncio.create_task(_run())
    try:
        # control.json が書かれる (= uvicorn が bind した) まで待つ。
        data = None
        for _ in range(200):
            await asyncio.sleep(0.02)
            data = read_control_file(ctl_path)
            if data is not None:
                break
        assert data is not None, "control.json が書かれなかった"
        assert data["backend_id"] == "mock"
        assert data["url"].startswith("http://127.0.0.1:")
        # port=0 なので OS 割当の実ポート (> 0) が書かれる。
        actual_port = int(data["url"].rsplit(":", 1)[1])
        assert actual_port > 0
        assert data["token"]
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    # finally 経路で control.json が掃除される。
    assert read_control_file(ctl_path) is None
