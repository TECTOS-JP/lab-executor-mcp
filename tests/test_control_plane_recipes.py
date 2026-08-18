"""Editing recipes through the serve process that will run them.

A recipe an operator wrote is a file, and the question these tests pin is who
is allowed to write it. The answer is the runtime: a save is parsed and
validated by the same process that will execute it, so a file that lands in the
library is one the runtime has already agreed it can run. Editing a recipe is
not instrument I/O; starting one is, and that stays on the job routes.
"""

from __future__ import annotations

import textwrap
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from starlette.testclient import TestClient

from lab_executor.control_plane import create_control_app
from lab_executor.job import JobManager, JobStore
from lab_executor.models.instrument_def import InstrumentDefinition
from lab_executor.recipe_library import RecipeLibrary

TOKEN = "t" * 64
RESOURCE = "USB0::1::INSTR"

RECIPE = textwrap.dedent("""
    description: 電圧をかけて測る
    parameters:
      - {name: target_v, type: float, required: true, range: [0, 30], unit: V}
    steps:
      - {command: set_voltage, args: {voltage: "${params.target_v}"}}
      - {command: measure_voltage, result_as: measured}
""")

DEFINITION_YAML = textwrap.dedent("""
    metadata:
      manufacturer: Kikusui
      model: PMX35-3A
      category: power_supply
      support_level: experimental
      definition_version: "0.1.0"
    commands:
      set_voltage:
        scpi: "VOLT {voltage}"
        type: write
        description: 電圧設定
        parameters:
          - {name: voltage, type: float, range: [0, 36.75]}
      measure_voltage:
        scpi: "MEAS:VOLT?"
        type: query
        description: 電圧測定
        returns: {type: float, unit: V}
    recipes:
      warm_up:
        description: 定義側のレシピ
        steps:
          - {command: measure_voltage}
""")


@pytest.fixture
def library(tmp_path):
    return RecipeLibrary(tmp_path / "recipes")


@pytest.fixture
def job_mgr(tmp_path, monkeypatch, library):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    backend = MagicMock()
    backend.list_resources = AsyncMock(return_value=[RESOURCE])

    definition = InstrumentDefinition(**yaml.safe_load(DEFINITION_YAML))

    class _Session:
        def __init__(self):
            self.definition = definition
            self.resource_name = RESOURCE

    class _SM:
        def get_session(self, name):
            return _Session() if name == RESOURCE else None

        def list_sessions(self):
            return [RESOURCE]

    store = JobStore(db_path=tmp_path / "state.sqlite")
    manager = JobManager(
        backend=backend, session_mgr=_SM(), store=store, recipe_library=library
    )
    yield manager
    store.close()


@pytest.fixture
def client(job_mgr):
    app = create_control_app(job_mgr, token=TOKEN, backend_id="pyvisa")
    return TestClient(app, raise_server_exceptions=False)


def _auth():
    return {"X-Control-Token": TOKEN}


def test_recipe_routes_require_a_token(client):
    assert client.get("/control/recipes").status_code == 401
    assert client.get("/control/recipes/x").status_code == 401
    assert client.put("/control/recipes/x", json={"text": RECIPE}).status_code == 401
    assert client.delete("/control/recipes/x").status_code == 401
    assert client.post("/control/recipes/check", json={"text": RECIPE}).status_code == 401


def test_a_recipe_can_be_saved_listed_read_and_deleted(client):
    assert client.put(
        "/control/recipes/iv_sweep", json={"text": RECIPE}, headers=_auth()
    ).status_code == 200

    listing = client.get("/control/recipes", headers=_auth()).json()
    entry = listing["recipes"][0]
    assert entry["name"] == "iv_sweep"
    assert entry["parameters"] == ["target_v"]
    assert entry["step_count"] == 2

    text = client.get("/control/recipes/iv_sweep", headers=_auth()).json()["text"]
    assert text == RECIPE

    assert client.delete(
        "/control/recipes/iv_sweep", headers=_auth()
    ).status_code == 200
    assert client.get("/control/recipes", headers=_auth()).json()["recipes"] == []


def test_an_invalid_recipe_is_refused_with_the_reason(client, library):
    response = client.put(
        "/control/recipes/broken", json={"text": "steps: [[["}, headers=_auth()
    )
    assert response.status_code == 422
    assert response.json()["error"] == "invalid_recipe"
    assert response.json()["detail"]
    assert library.list() == []


def test_a_recipe_may_not_take_a_definition_recipes_name(client):
    """Otherwise which recipe a name refers to depends on where we looked."""
    response = client.put(
        "/control/recipes/warm_up", json={"text": RECIPE}, headers=_auth()
    )
    assert response.status_code == 422
    assert "機器定義" in response.json()["detail"]


def test_checking_a_recipe_writes_nothing(client, library):
    body = client.post(
        "/control/recipes/check", json={"text": RECIPE}, headers=_auth()
    ).json()
    assert body["valid"] is True
    assert body["step_count"] == 2
    assert body["parameters"][0]["name"] == "target_v"
    assert body["parameters"][0]["unit"] == "V"
    assert library.list() == []


def test_checking_reports_a_problem_instead_of_failing(client):
    body = client.post(
        "/control/recipes/check", json={"text": "steps: 3"}, headers=_auth()
    ).json()
    assert body["valid"] is False
    assert body["detail"]


def test_a_missing_recipe_is_a_404(client):
    assert client.get("/control/recipes/absent", headers=_auth()).status_code == 404
    assert client.delete("/control/recipes/absent", headers=_auth()).status_code == 404


def test_a_name_that_is_a_path_is_refused(client, tmp_path):
    response = client.put(
        "/control/recipes/..%2Fescape", json={"text": RECIPE}, headers=_auth()
    )
    assert response.status_code in (404, 422)
    assert not list(tmp_path.glob("*.yaml"))


def test_a_serve_without_a_library_says_so(tmp_path, monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    backend = MagicMock()
    backend.list_resources = AsyncMock(return_value=[])

    class _SM:
        def get_session(self, name):
            return None

    store = JobStore(db_path=tmp_path / "s.sqlite")
    manager = JobManager(backend=backend, session_mgr=_SM(), store=store)
    client = TestClient(
        create_control_app(manager, token=TOKEN, backend_id="mock"),
        raise_server_exceptions=False,
    )
    assert client.get("/control/recipes", headers=_auth()).status_code == 503
    store.close()


def test_saving_and_deleting_are_audited(client, job_mgr):
    from lab_executor.audit import AuditStore

    client.put("/control/recipes/iv_sweep", json={"text": RECIPE}, headers=_auth())
    client.delete("/control/recipes/iv_sweep", headers=_auth())
    events, _cursor = AuditStore(job_mgr.store).query(limit=50)
    kinds = {e["event_type"] for e in events}
    assert "recipe_saved" in kinds
    assert "recipe_deleted" in kinds


def test_a_library_recipe_can_be_started_like_any_other(client, job_mgr):
    """The point of the library: a recipe written here is one that can run."""
    client.put("/control/recipes/iv_sweep", json={"text": RECIPE}, headers=_auth())
    response = client.post(
        "/control/jobs/start-recipe",
        json={
            "resource_name": RESOURCE,
            "recipe_name": "iv_sweep",
            "parameters": {"target_v": 5.0},
        },
        headers=_auth(),
    )
    assert response.status_code == 200
    body = response.json()
    assert body.get("job_id")
    # Registration must not have failed for "recipe not defined".
    assert "定義されていません" not in str(body)
