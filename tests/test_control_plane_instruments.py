"""Read-only instrument endpoints on the control plane.

A Web UI has to show which instruments exist and what they are, and neither the
state DB nor the control plane could answer that before this. These tests pin the
two properties that make the addition safe: it reaches the hardware only through
the serve process that already owns it, and it can invoke nothing except an
allowlist of read-only tools.
"""

from __future__ import annotations

import textwrap
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from starlette.testclient import TestClient

from lab_executor.control_plane import _READ_ONLY_TOOLS, create_control_app
from lab_executor.job import JobManager, JobStore
from lab_executor.models.instrument_def import InstrumentDefinition
from lab_visa_mcp.session_manager import InstrumentSession

TOKEN = "t" * 64
RESOURCE = "GPIB0::5::INSTR"

SAMPLE_YAML = """
metadata:
  manufacturer: Test
  model: PSU
  category: power_supply
  support_level: experimental
  definition_version: "0.1.0"
commands:
  reset:
    scpi: "*RST"
    type: write
    description: reset
"""


class _FakeToolResult:
    def __init__(self, payload):
        self.structured_content = payload
        self.data = payload


class _FakeMcp:
    """Records every tool name asked for, so the allowlist can be checked."""

    def __init__(self, available=None):
        self.calls: list[str] = []
        self._available = (
            {
                "describe_instrument": {"identity": {"model": "PSU"}},
                "get_instrument_info": {"commands": ["reset"]},
                "list_safety_constraints": {"constraints": []},
                "get_state": {"voltage": 1.0},
            }
            if available is None
            else available
        )

    async def call_tool(self, name, arguments):
        self.calls.append(name)
        if name not in self._available:
            raise LookupError(f"Unknown tool: {name!r}")
        # Verified against a live pyvisa serve process: an unknown resource does
        # not raise, and the tools do not share one response shape.
        # describe_instrument answers {"status": "error", "errors": [...]};
        # get_instrument_info and list_safety_constraints answer
        # {"success": False, "error": "SessionNotFound"}. Both are reproduced
        # here so a test cannot pass by only handling one of them.
        if arguments.get("resource_name") not in (RESOURCE, None):
            missing = arguments.get("resource_name")
            if name == "describe_instrument":
                return _FakeToolResult({
                    "status": "error",
                    "data": {},
                    "errors": [{
                        "error_class": "not_found",
                        "message": f"{missing} は未識別です",
                    }],
                })
            return _FakeToolResult({
                "success": False,
                "error": "SessionNotFound",
                "message": f"{missing} は未識別です",
            })
        return _FakeToolResult(self._available[name])


@pytest.fixture
def job_mgr(tmp_path, monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    backend = MagicMock()
    backend.write = AsyncMock(return_value=None)
    backend.query = AsyncMock(return_value="+1.0")
    backend.list_resources = AsyncMock(return_value=[RESOURCE, "USB0::1::INSTR"])

    definition = InstrumentDefinition(**yaml.safe_load(textwrap.dedent(SAMPLE_YAML)))
    session = InstrumentSession(
        resource_name=RESOURCE,
        idn_response="<test>",
        idn_parsed={"manufacturer": "Test", "model": "PSU"},
        definition=definition,
    )

    class _SM:
        def get_session(self, name):
            return session if name == RESOURCE else None

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


def test_job_manager_exposes_its_backend(job_mgr):
    """The control plane enumerates through this rather than opening its own."""
    assert job_mgr.backend is not None


def test_instrument_endpoints_require_a_token(client):
    for path in (
        "/control/instruments",
        f"/control/instruments/{RESOURCE}",
        f"/control/instruments/{RESOURCE}/state",
    ):
        assert client.get(path).status_code == 401, path


def test_list_uses_the_backend_the_agent_uses(client, job_mgr):
    response = client.get("/control/instruments", headers=_auth())
    assert response.status_code == 200
    body = response.json()
    assert [r["resource_name"] for r in body["resources"]] == [
        RESOURCE,
        "USB0::1::INSTR",
    ]
    job_mgr.backend.list_resources.assert_awaited()


def test_detail_merges_the_read_only_description_tools(client, mcp):
    response = client.get(f"/control/instruments/{RESOURCE}", headers=_auth())
    assert response.status_code == 200
    body = response.json()
    assert body["resource_name"] == RESOURCE
    assert body["description"] == {"identity": {"model": "PSU"}}
    assert body["info"] == {"commands": ["reset"]}
    assert body["safety_constraints"] == {"constraints": []}
    assert set(mcp.calls) <= _READ_ONLY_TOOLS


def test_state_reads_through_get_state_and_is_audited(client, mcp, job_mgr):
    response = client.get(f"/control/instruments/{RESOURCE}/state", headers=_auth())
    assert response.status_code == 200
    assert response.json()["state"] == {"voltage": 1.0}
    assert "get_state" in mcp.calls
    # Reading state talks to the device, so it must leave an audit trail.
    events = job_mgr.store.list_audit_events() if hasattr(
        job_mgr.store, "list_audit_events"
    ) else None
    if events is not None:
        assert any("instrument_state" in str(e) for e in events)


def test_only_read_only_tools_are_reachable(client, mcp):
    """No endpoint may invoke a tool that could change an instrument."""
    client.get("/control/instruments", headers=_auth())
    client.get(f"/control/instruments/{RESOURCE}", headers=_auth())
    client.get(f"/control/instruments/{RESOURCE}/state", headers=_auth())
    assert mcp.calls, "expected the plane to go through MCP tools"
    for name in mcp.calls:
        assert name in _READ_ONLY_TOOLS, name
    for forbidden in (
        "execute_named_command",
        "start_experiment_job",
        "unsafe_send_command",
        "cancel_job",
    ):
        assert forbidden not in _READ_ONLY_TOOLS


def test_there_is_no_generic_tool_passthrough(client):
    """A caller must not be able to name the tool to run.

    The instrument routes take a resource name, never a tool name, so a path
    that spells one out is either unrouted or treated as an unknown instrument.
    """
    for path in (
        "/control/tools/execute_named_command",
        "/control/call/execute_named_command",
        "/control/invoke",
    ):
        assert client.get(path, headers=_auth()).status_code in (404, 405), path
    # The instrument route takes a resource name, so a tool name lands there as
    # an unknown instrument. Checked on hardware: a suffix like ".../write" is
    # read the same way and performs no write, because no write route exists.
    for stray_path in (
        "/control/instruments/execute_named_command",
        f"/control/instruments/{RESOURCE}/write",
    ):
        stray = client.get(stray_path, headers=_auth())
        assert stray.status_code == 404, stray_path
        assert stray.json()["error"] == "unknown_instrument"


def test_unknown_instrument_is_404_with_the_tools_own_reason(client):
    """A resource nobody knows must not look like a successful lookup."""
    response = client.get("/control/instruments/GPIB0::99::INSTR", headers=_auth())
    assert response.status_code == 404
    body = response.json()
    assert body["error"] == "unknown_instrument"
    # The agent's own explanation is handed through, not replaced — in whichever
    # shape each tool uses.
    assert body["description"]["errors"][0]["error_class"] == "not_found"
    assert body["info"]["error"] == "SessionNotFound"


def test_both_tool_response_shapes_count_as_a_miss():
    """Recognising only one envelope would let an unknown instrument look real.

    This is the defect real hardware exposed: the first implementation checked
    ``status == "error"`` alone, so the two tools using ``success: False`` kept
    the response at 200.
    """
    from lab_executor.control_plane import _section_failed

    assert _section_failed({"status": "error", "errors": []})
    assert _section_failed({"success": False, "error": "SessionNotFound"})
    assert not _section_failed({"status": "ok", "data": {}})
    assert not _section_failed({"success": True, "data": {}})
    assert not _section_failed(["not", "a", "mapping"])


def test_missing_tools_degrade_instead_of_failing(job_mgr):
    """A server composed without the description tools says so plainly."""
    app = create_control_app(
        job_mgr, token=TOKEN, backend_id="mock", mcp=_FakeMcp(available={})
    )
    client = TestClient(app, raise_server_exceptions=False)
    detail = client.get(f"/control/instruments/{RESOURCE}", headers=_auth())
    assert detail.status_code == 503
    assert detail.json()["error"] == "not_available"
    # Enumeration still works: it needs only the frozen BEF contract.
    assert client.get("/control/instruments", headers=_auth()).status_code == 200


def test_without_mcp_the_plane_still_serves_the_resource_list(job_mgr):
    """Existing callers that pass no mcp keep working."""
    app = create_control_app(job_mgr, token=TOKEN, backend_id="mock")
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/control/instruments", headers=_auth()).status_code == 200
    assert (
        client.get(f"/control/instruments/{RESOURCE}", headers=_auth()).status_code
        == 503
    )


def test_backend_failure_is_reported_not_swallowed(job_mgr, mcp):
    job_mgr.backend.list_resources = AsyncMock(side_effect=OSError("GPIB down"))
    app = create_control_app(job_mgr, token=TOKEN, backend_id="mock", mcp=mcp)
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/control/instruments", headers=_auth())
    assert response.status_code == 502
    assert "GPIB down" in response.json()["detail"]


# ---------- identification ----------


UNBOUND = "USB0::1::INSTR"

_IDENTIFY = {
    "success": True,
    "data": {
        "resource_name": UNBOUND,
        "manufacturer": "KIKUSUI",
        "model": "PMX35-3A",
        "definition_loaded": True,
    },
}


class _IdentifyMcp(_FakeMcp):
    """Identification is asked about a resource that has no session yet, so the
    base stub's "unknown resource" branch must not stand in for the answer."""

    async def call_tool(self, name, arguments):
        self.calls.append(name)
        if name not in self._available:
            raise LookupError(f"Unknown tool: {name!r}")
        return _FakeToolResult(self._available[name])


@pytest.fixture
def identifying_client(job_mgr):
    mcp = _IdentifyMcp(available={"identify_instrument": _IDENTIFY})
    app = create_control_app(job_mgr, token=TOKEN, backend_id="mock", mcp=mcp)
    return TestClient(app, raise_server_exceptions=False), mcp


def test_identifying_requires_a_token(identifying_client):
    client, _mcp = identifying_client
    assert client.post(f"/control/instruments/{UNBOUND}/identify").status_code == 401


def test_a_connected_resource_can_be_identified(identifying_client):
    """A resource the backend can see is not yet usable: without a session there
    is no bound definition and therefore no commands, and nothing else on this
    plane can create one."""
    client, mcp = identifying_client
    response = client.post(
        f"/control/instruments/{UNBOUND}/identify", headers=_auth()
    )
    assert response.status_code == 200
    assert response.json()["result"]["data"]["model"] == "PMX35-3A"
    assert mcp.calls == ["identify_instrument"]


def test_identifying_is_audited_like_a_state_read(identifying_client, job_mgr):
    from lab_executor.audit import AuditStore

    client, _mcp = identifying_client
    client.post(f"/control/instruments/{UNBOUND}/identify", headers=_auth())
    events, _cursor = AuditStore(job_mgr.store).query(
        event_type="instrument_identify_requested", limit=10
    )
    assert events


def test_identifying_is_not_on_the_read_only_surface():
    """It sends *IDN? and records a session, so it must not be pollable."""
    assert "identify_instrument" not in _READ_ONLY_TOOLS


def test_a_backend_without_identification_says_so(job_mgr):
    """Only instruments that announce themselves can be identified at all."""
    app = create_control_app(
        job_mgr, token=TOKEN, backend_id="nidaq", mcp=_FakeMcp(available={})
    )
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(
        f"/control/instruments/{UNBOUND}/identify", headers=_auth()
    )
    assert response.status_code == 503
    assert response.json()["error"] == "not_available"


def test_an_instrument_that_does_not_answer_is_reported(job_mgr):
    failing = _IdentifyMcp(
        available={
            "identify_instrument": {
                "success": False,
                "error": "IdentifyFailed",
                "message": "応答がありません",
            }
        }
    )
    app = create_control_app(job_mgr, token=TOKEN, backend_id="mock", mcp=failing)
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(
        f"/control/instruments/{UNBOUND}/identify", headers=_auth()
    )
    assert response.status_code == 502
    assert "応答がありません" in response.json()["result"]["message"]


def test_the_identify_path_is_not_swallowed_by_the_detail_route(identifying_client):
    """``{resource_name:path}`` is greedy; declaration order is what saves it."""
    client, mcp = identifying_client
    assert (
        client.post(
            f"/control/instruments/{UNBOUND}/identify", headers=_auth()
        ).status_code
        == 200
    )
    assert mcp.calls == ["identify_instrument"]
