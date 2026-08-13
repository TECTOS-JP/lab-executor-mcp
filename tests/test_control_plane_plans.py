"""Read-only plan endpoints: check and render a procedure before running it.

Both tools are documented to perform no instrument I/O, which is what lets them
sit next to the instrument reads. Starting a plan does touch the instruments, so
it lives on its own route with its own discipline (see test_control_plane_jobs).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.testclient import TestClient

from lab_executor.control_plane import _READ_ONLY_TOOLS, create_control_app
from lab_executor.job import JobManager, JobStore

TOKEN = "t" * 64
PLAN = {
    "dsl_version": "0.8",
    "name": "test",
    "steps": [
        {"type": "command", "instrument": "DAQ::Dev2", "command": "write_ao0"},
    ],
}


class _FakeToolResult:
    def __init__(self, payload):
        self.structured_content = payload
        self.data = payload


class _FakeMcp:
    def __init__(self, available=None):
        self.calls: list[tuple[str, dict]] = []
        self._available = (
            {
                "validate_experiment_plan": {"status": "ok", "data": {"valid": True}},
                "dry_run_plan": {"status": "ok", "data": {"steps": []}},
            }
            if available is None
            else available
        )

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
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


def test_plan_endpoints_require_a_token(client):
    for path in ("/control/plans/validate", "/control/plans/dry-run"):
        assert client.post(path, json={"plan": PLAN}).status_code == 401, path


def test_validate_goes_through_the_allowlisted_tool(client, mcp):
    response = client.post(
        "/control/plans/validate", json={"plan": PLAN}, headers=_auth()
    )
    assert response.status_code == 200
    body = response.json()
    assert body["tool"] == "validate_experiment_plan"
    assert body["result"]["data"]["valid"] is True
    assert mcp.calls[0][0] == "validate_experiment_plan"
    assert mcp.calls[0][1] == {"plan": PLAN}


def test_dry_run_goes_through_the_allowlisted_tool(client, mcp):
    response = client.post(
        "/control/plans/dry-run", json={"plan": PLAN}, headers=_auth()
    )
    assert response.status_code == 200
    assert response.json()["tool"] == "dry_run_plan"
    assert mcp.calls[0][0] == "dry_run_plan"


def test_only_allowlisted_tools_are_reachable(client, mcp):
    client.post("/control/plans/validate", json={"plan": PLAN}, headers=_auth())
    client.post("/control/plans/dry-run", json={"plan": PLAN}, headers=_auth())
    for name, _arguments in mcp.calls:
        assert name in _READ_ONLY_TOOLS, name
    # Starting a plan must not be reachable from the read-only surface.
    assert "start_experiment_job" not in _READ_ONLY_TOOLS
    assert "execute_named_command" not in _READ_ONLY_TOOLS


def test_a_missing_plan_is_refused(client):
    for body in ({}, {"plan": "not an object"}, {"plan": []}):
        response = client.post("/control/plans/validate", json=body, headers=_auth())
        assert response.status_code == 422, body


def test_starting_a_plan_lives_on_its_own_route(client):
    """Check and start are separate routes, so neither can be reached by accident.

    ``/control/plans/start`` exists and is covered by its own tests; what must
    not exist is a second way in.
    """
    for path in ("/control/plans/run", "/control/plans/execute"):
        response = client.post(path, json={"plan": PLAN}, headers=_auth())
        assert response.status_code in (404, 405), path


def test_a_server_without_the_tools_says_so(job_mgr):
    app = create_control_app(
        job_mgr, token=TOKEN, backend_id="mock", mcp=_FakeMcp(available={})
    )
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(
        "/control/plans/validate", json={"plan": PLAN}, headers=_auth()
    )
    assert response.status_code == 503
    assert response.json()["error"] == "not_available"
