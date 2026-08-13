"""Starting a plan, and watching any job — including one an agent started.

Two different kinds of route meet here. Reading job state is safe to poll, so
it goes through the read-only allowlist. Starting a plan touches instruments,
so it follows the recipe route's discipline: safety cannot be switched off from
a browser, and both the request and its outcome are audited.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.testclient import TestClient

from lab_executor.audit import AuditStore
from lab_executor.control_plane import _READ_ONLY_TOOLS, create_control_app
from lab_executor.job import JobManager, JobStore

TOKEN = "t" * 64
PLAN = {
    "dsl_version": "0.8",
    "name": "test",
    "steps": [{"type": "query", "instrument": "DAQ::Dev2", "command": "read_ai0"}],
}


class _FakeToolResult:
    def __init__(self, payload):
        self.structured_content = payload
        self.data = payload


class _FakeMcp:
    def __init__(self, available=None, *, fail=False):
        self.calls: list[tuple[str, dict]] = []
        self.fail = fail
        self._available = (
            {
                "list_jobs": {"success": True, "data": {"jobs": [
                    {"job_id": "j1", "status": "running", "owner": "agent"},
                ]}},
                "get_job_status": {"success": True, "data": {
                    "job_id": "j1", "status": "running", "owner": "agent"}},
                "get_job_live_view": {"success": True, "data": {"current_step": 2}},
                "start_experiment_job": {"success": True, "data": {
                    "job_id": "j9", "status": "queued"}},
            }
            if available is None
            else available
        )

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self.fail and name == "start_experiment_job":
            raise RuntimeError("boom")
        if name not in self._available:
            raise LookupError(f"Unknown tool: {name!r}")
        return _FakeToolResult(self._available[name])


@pytest.fixture
def job_mgr(tmp_path, monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    backend = MagicMock()
    backend.list_resources = AsyncMock(return_value=["DAQ::Dev2"])

    class _SM:
        def get_session(self, name):
            return None

    store = JobStore(db_path=tmp_path / "state.sqlite")
    manager = JobManager(backend=backend, session_mgr=_SM(), store=store)
    yield manager
    store.close()


@pytest.fixture
def mcp():
    return _FakeMcp()


@pytest.fixture
def client(job_mgr, mcp):
    app = create_control_app(job_mgr, token=TOKEN, backend_id="mock", mcp=mcp)
    return TestClient(app, raise_server_exceptions=False)


def _auth():
    return {"X-Control-Token": TOKEN}


def test_job_routes_require_a_token(client):
    assert client.get("/control/jobs").status_code == 401
    assert client.get("/control/jobs/j1").status_code == 401
    assert client.post("/control/plans/start", json={"plan": PLAN}).status_code == 401


def test_jobs_can_be_listed(client, mcp):
    body = client.get("/control/jobs", headers=_auth()).json()
    assert body["result"]["data"]["jobs"][0]["job_id"] == "j1"
    assert mcp.calls[0][0] == "list_jobs"


def test_a_status_filter_is_passed_through(client, mcp):
    client.get("/control/jobs?status=running", headers=_auth())
    assert mcp.calls[0][1] == {"status": "running"}


def test_job_detail_merges_status_and_live_view(client):
    body = client.get("/control/jobs/j1", headers=_auth()).json()
    assert body["job_id"] == "j1"
    assert body["status"]["data"]["status"] == "running"
    assert body["live_view"]["data"]["current_step"] == 2


def test_an_agent_started_job_is_visible_the_same_way(client):
    """Watching an agent's run is the point; its owner must come through."""
    body = client.get("/control/jobs/j1", headers=_auth()).json()
    assert body["status"]["data"]["owner"] == "agent"


def test_reading_jobs_only_uses_read_only_tools(client, mcp):
    client.get("/control/jobs", headers=_auth())
    client.get("/control/jobs/j1", headers=_auth())
    for name, _arguments in mcp.calls:
        assert name in _READ_ONLY_TOOLS, name


def test_starting_a_plan_goes_through_the_runtime(client, mcp):
    response = client.post(
        "/control/plans/start", json={"plan": PLAN, "owner": "human"}, headers=_auth()
    )
    assert response.status_code == 200
    assert response.json()["result"]["data"]["job_id"] == "j9"
    name, arguments = mcp.calls[0]
    assert name == "start_experiment_job"
    assert arguments["owner"] == "human"


def test_safety_cannot_be_switched_off_from_a_browser(client, mcp):
    """A body asking for an override must not reach the runtime as one."""
    client.post(
        "/control/plans/start",
        json={"plan": PLAN, "override_safety": True, "override_reason": "please"},
        headers=_auth(),
    )
    _name, arguments = mcp.calls[0]
    assert arguments["override_safety"] is False
    assert "override_reason" not in arguments


def test_starting_is_not_in_the_read_only_allowlist():
    """The read-only surface must never be able to run an experiment."""
    assert "start_experiment_job" not in _READ_ONLY_TOOLS
    assert "execute_named_command" not in _READ_ONLY_TOOLS


def test_starting_a_plan_is_audited_before_and_after(client, job_mgr):
    client.post(
        "/control/plans/start", json={"plan": PLAN, "owner": "human"}, headers=_auth()
    )
    events, _cursor = AuditStore(job_mgr.store).query(limit=50)
    kinds = {e["event_type"] for e in events}
    assert "plan_start_requested" in kinds
    assert "plan_started" in kinds


def test_a_failed_start_is_audited_and_reported(job_mgr):
    app = create_control_app(
        job_mgr, token=TOKEN, backend_id="mock", mcp=_FakeMcp(fail=True)
    )
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(
        "/control/plans/start", json={"plan": PLAN}, headers=_auth()
    )
    assert response.status_code == 500
    events, _cursor = AuditStore(job_mgr.store).query(
        event_type="plan_start_failed", limit=10
    )
    assert events


def test_a_malformed_plan_is_refused_before_anything_runs(client, mcp):
    for body in ({}, {"plan": "text"}, {"plan": []}):
        assert (
            client.post("/control/plans/start", json=body, headers=_auth()).status_code
            == 422
        )
    assert not mcp.calls


# ---------- recorded measurements ----------

_RESULTS_TOOLS = {
    "get_experiment_results": {
        "success": True,
        "data": {"steps": [{"response": '{"artifact":"v1","name":"acq-a.npz"}'}]},
    },
    "get_monitor_data": {
        "success": True,
        "data": {"data": [{"instrument": "DAQ::Dev2", "value": 1.0}]},
    },
}


@pytest.fixture
def results_client(job_mgr):
    mcp = _FakeMcp(available=_RESULTS_TOOLS)
    app = create_control_app(job_mgr, token=TOKEN, backend_id="mock", mcp=mcp)
    return TestClient(app, raise_server_exceptions=False), mcp


def test_results_routes_require_a_token(results_client):
    client, _mcp = results_client
    assert client.get("/control/jobs/j1/results").status_code == 401
    assert client.get("/control/jobs/j1/monitor").status_code == 401


def test_recorded_results_come_back_for_a_job(results_client):
    client, mcp = results_client
    body = client.get("/control/jobs/j1/results", headers=_auth()).json()
    assert body["result"]["data"]["steps"][0]["response"].startswith('{"artifact"')
    assert mcp.calls == [("get_experiment_results", {"job_id": "j1"})]


def test_recorded_monitor_samples_come_back_for_a_job(results_client):
    client, mcp = results_client
    body = client.get("/control/jobs/j1/monitor", headers=_auth()).json()
    assert body["result"]["data"]["data"][0]["instrument"] == "DAQ::Dev2"
    assert mcp.calls[0][1] == {"monitor_id": "j1", "limit": 1000}


def test_a_monitor_limit_is_passed_through_and_a_bad_one_falls_back(results_client):
    client, mcp = results_client
    client.get("/control/jobs/j1/monitor?limit=50", headers=_auth())
    assert mcp.calls[0][1]["limit"] == 50
    client.get("/control/jobs/j1/monitor?limit=lots", headers=_auth())
    assert mcp.calls[1][1]["limit"] == 1000


def test_reading_results_only_uses_read_only_tools(results_client):
    client, mcp = results_client
    client.get("/control/jobs/j1/results", headers=_auth())
    client.get("/control/jobs/j1/monitor", headers=_auth())
    for name, _arguments in mcp.calls:
        assert name in _READ_ONLY_TOOLS, name


def test_a_runtime_without_the_results_tools_says_so_rather_than_failing(job_mgr):
    """An older runtime has no results tools; that is a 503, not a crash."""
    app = create_control_app(
        job_mgr, token=TOKEN, backend_id="mock", mcp=_FakeMcp(available={})
    )
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/control/jobs/j1/results", headers=_auth()).status_code == 503
    assert client.get("/control/jobs/j1/monitor", headers=_auth()).status_code == 503


def test_a_results_path_is_not_swallowed_by_the_job_detail_route(results_client):
    """``/jobs/{id}`` must not match ``/jobs/{id}/results`` and hide it."""
    client, mcp = results_client
    assert client.get("/control/jobs/j1/results", headers=_auth()).status_code == 200
    assert mcp.calls[0][0] == "get_experiment_results"
