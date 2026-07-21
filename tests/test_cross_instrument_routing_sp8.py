"""SP-8 cross-instrument routing and fail-closed safety regressions."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from lab_executor.models.instrument_def import InstrumentDefinition
from lab_executor.recipe_executor import execute_recipe, recipe_to_plan
from lab_executor.ui.views import dryrun_view
from lab_executor.utils.seq_expression import SeqExpressionError
from lab_visa_mcp.session_manager import InstrumentSession


PRIMARY = "TEST::PRIMARY"
OTHER = "TEST::OTHER"


def _definition(*, instrument: str | None) -> InstrumentDefinition:
    step = {"command": "measure"}
    if instrument is not None:
        step["instrument"] = instrument
    return InstrumentDefinition.model_validate({
        "metadata": {"manufacturer": "Test", "model": "SP8"},
        "commands": {
            "measure": {
                "scpi": "MEAS?",
                "type": "query",
            },
        },
        "recipes": {"run": {"steps": [step]}},
    })


def _session(definition: InstrumentDefinition) -> InstrumentSession:
    return _target_session(PRIMARY, definition)


def _target_session(
    resource: str, definition: InstrumentDefinition,
) -> InstrumentSession:
    return InstrumentSession(
        resource_name=resource,
        idn_response="<test>",
        idn_parsed={"manufacturer": "Test", "model": "SP8"},
        definition=definition,
    )


def _visa():
    visa = MagicMock()
    visa.query = AsyncMock(return_value="1.0")
    visa.write = AsyncMock(return_value=None)
    return visa


@pytest.mark.asyncio
async def test_cross_instrument_without_resolver_fails_without_io():
    """A target-resolution failure must never fall back to the primary."""
    visa = _visa()
    result = await execute_recipe(
        visa, _session(_definition(instrument=OTHER)), "run", {},
    )

    assert result["success"] is False
    failed = result["steps_executed"][0]
    assert failed["error"] == "InstrumentNotAvailable"
    assert failed["instrument"] == OTHER
    assert PRIMARY in failed["message"] and OTHER in failed["message"]
    visa.query.assert_not_awaited()
    visa.write.assert_not_awaited()


@pytest.mark.parametrize("instrument", [None, PRIMARY])
@pytest.mark.asyncio
async def test_primary_or_omitted_instrument_remains_compatible(instrument):
    visa = _visa()
    result = await execute_recipe(
        visa, _session(_definition(instrument=instrument)), "run", {},
    )

    assert result["success"] is True, result
    assert result["steps_executed"][0]["instrument"] == PRIMARY
    visa.query.assert_awaited_once()
    assert visa.query.await_args.args[0] == PRIMARY


def _resolver(*sessions: InstrumentSession):
    table = {session.resource_name: session for session in sessions}
    return table.get


@pytest.mark.asyncio
async def test_routes_with_target_definition_and_reports_effective_resource():
    primary_def = InstrumentDefinition.model_validate({
        "metadata": {"manufacturer": "Test", "model": "Controller"},
        "commands": {},
        "recipes": {"run": {"steps": [{
            "command": "target_only", "instrument": OTHER,
        }]}},
    })
    target_def = InstrumentDefinition.model_validate({
        "metadata": {"manufacturer": "Test", "model": "Meter"},
        "commands": {"target_only": {"scpi": "TARGET?", "type": "query"}},
    })
    primary = _session(primary_def)
    target = _target_session(OTHER, target_def)
    visa = _visa()

    result = await execute_recipe(
        visa, primary, "run", {}, session_resolver=_resolver(primary, target),
    )

    assert result["success"] is True, result
    assert result["steps_executed"][0]["instrument"] == OTHER
    visa.query.assert_awaited_once()
    assert visa.query.await_args.args[:2] == (OTHER, "TARGET?")


@pytest.mark.asyncio
async def test_target_definition_missing_command_does_not_use_primary_definition():
    primary_def = InstrumentDefinition.model_validate({
        "metadata": {"manufacturer": "Test", "model": "Controller"},
        "commands": {"primary_only": {"scpi": "PRIMARY?", "type": "query"}},
        "recipes": {"run": {"steps": [{
            "command": "primary_only", "instrument": OTHER,
        }]}},
    })
    target_def = InstrumentDefinition.model_validate({
        "metadata": {"manufacturer": "Test", "model": "Meter"},
        "commands": {"measure": {"scpi": "MEAS?", "type": "query"}},
    })
    primary = _session(primary_def)
    target = _target_session(OTHER, target_def)
    visa = _visa()

    result = await execute_recipe(
        visa, primary, "run", {}, session_resolver=_resolver(primary, target),
    )

    failed = result["steps_executed"][0]
    assert failed["error"] == "CommandNotFound"
    assert OTHER in failed["message"]
    visa.query.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolved_target_without_definition_fails_closed():
    primary = _session(_definition(instrument=OTHER))
    target = InstrumentSession(
        resource_name=OTHER,
        idn_response="<unidentified>",
        idn_parsed={},
        definition=None,
    )
    visa = _visa()

    result = await execute_recipe(
        visa, primary, "run", {}, session_resolver=_resolver(primary, target),
    )

    failed = result["steps_executed"][0]
    assert failed["error"] == "NoDefinitionFound"
    assert OTHER in failed["message"]
    visa.query.assert_not_awaited()
    visa.write.assert_not_awaited()


@pytest.mark.asyncio
async def test_deferred_range_uses_target_definition_not_primary():
    def command(maximum):
        return {
            "scpi": "VOLT {value}", "type": "write",
            "parameters": [{"name": "value", "type": "float", "range": [0, maximum]}],
        }

    primary_def = InstrumentDefinition.model_validate({
        "metadata": {"manufacturer": "Test", "model": "Wide"},
        "commands": {"set_voltage": command(100)},
        "recipes": {"run": {
            "parameters": [{"name": "value", "type": "float", "required": True}],
            "steps": [{
                "command": "set_voltage", "instrument": OTHER,
                "args": {"value": "${params.value}"},
            }],
        }},
    })
    target_def = InstrumentDefinition.model_validate({
        "metadata": {"manufacturer": "Test", "model": "Narrow"},
        "commands": {"set_voltage": command(5)},
    })
    primary = _session(primary_def)
    target = _target_session(OTHER, target_def)
    visa = _visa()

    result = await execute_recipe(
        visa, primary, "run", {"value": 10},
        session_resolver=_resolver(primary, target),
    )

    failed = result["steps_executed"][0]
    assert failed["error"] == "range_violation"
    assert failed["instrument"] == OTHER
    visa.write.assert_not_awaited()


def test_call_capability_is_checked_against_bound_target():
    primary_def = InstrumentDefinition.model_validate({
        "metadata": {"manufacturer": "Test", "model": "Controller"},
        "commands": {},
        "sequences": {"read": {
            "roles": [{"name": "meter", "requires": {"commands": ["measure"]}}],
            "steps": [{"command": "measure", "instrument": "@meter"}],
        }},
        "recipes": {"run": {"steps": [{"call": {
            "sequence": "read", "bind": {"meter": OTHER},
        }}]}},
    })
    target_def = InstrumentDefinition.model_validate({
        "metadata": {"manufacturer": "Test", "model": "WrongMeter"},
        "commands": {"other": {"scpi": "OTHER?", "type": "query"}},
    })
    primary = _session(primary_def)
    target = _target_session(OTHER, target_def)

    with pytest.raises(SeqExpressionError, match="capability"):
        recipe_to_plan(
            primary_def.recipes["run"], {}, primary_resource=PRIMARY,
            definition=primary_def,
            session_resolver=_resolver(primary, target),
        )


@pytest.mark.asyncio
async def test_safe_shutdown_attempts_each_written_resource_independently():
    def device(model, set_scpi, off_scpi, *, recipe=None):
        data = {
            "metadata": {"manufacturer": "Test", "model": model},
            "commands": {
                "set": {"scpi": set_scpi, "type": "write"},
                "off": {"scpi": off_scpi, "type": "write"},
            },
            "safe_shutdown": [{"command": "off"}],
        }
        if recipe is not None:
            data["recipes"] = {"run": recipe}
        return InstrumentDefinition.model_validate(data)

    recipe = {"steps": [
        {"command": "set", "instrument": PRIMARY},
        {"command": "set", "instrument": OTHER},
        {"guard": {"expr": "False", "on_fail": "safe_shutdown"}},
    ]}
    primary = _target_session(PRIMARY, device("PSU-A", "SET A", "OFF A", recipe=recipe))
    target = _target_session(OTHER, device("PSU-B", "SET B", "OFF B"))
    visa = _visa()

    async def write(resource, scpi, **_kwargs):
        if resource == OTHER and scpi == "OFF B":
            raise RuntimeError("shutdown B failed")

    visa.write.side_effect = write
    result = await execute_recipe(
        visa, primary, "run", {}, session_resolver=_resolver(primary, target),
    )

    shutdown = result["steps_executed"][2]["safe_shutdown"]
    assert set(shutdown["resources"]) == {PRIMARY, OTHER}
    assert shutdown["resources"][OTHER]["success"] is False
    assert shutdown["resources"][PRIMARY]["success"] is True
    calls = [(call.args[0], call.args[1]) for call in visa.write.await_args_list]
    assert (PRIMARY, "OFF A") in calls
    assert (OTHER, "OFF B") in calls


@pytest.mark.asyncio
async def test_safe_shutdown_excludes_read_only_cross_instruments():
    recipe = {"steps": [
        {"command": "read", "instrument": PRIMARY},
        {"command": "read", "instrument": OTHER},
        {"guard": {"expr": "False", "on_fail": "safe_shutdown"}},
    ]}

    def definition(model, *, with_recipe=False):
        data = {
            "metadata": {"manufacturer": "Test", "model": model},
            "commands": {
                "read": {"scpi": "READ?", "type": "query"},
                "off": {"scpi": "OFF", "type": "write"},
            },
            "safe_shutdown": [{"command": "off"}],
        }
        if with_recipe:
            data["recipes"] = {"run": recipe}
        return InstrumentDefinition.model_validate(data)

    primary = _target_session(PRIMARY, definition("A", with_recipe=True))
    target = _target_session(OTHER, definition("B"))
    visa = _visa()

    result = await execute_recipe(
        visa, primary, "run", {}, session_resolver=_resolver(primary, target),
    )

    shutdown = result["steps_executed"][2]["safe_shutdown"]
    assert shutdown == {"attempted": False, "success": True, "resources": {}}
    visa.write.assert_not_awaited()


@pytest.mark.asyncio
async def test_closed_loop_routes_power_and_meter_and_dryrun_shows_targets():
    primary_def = InstrumentDefinition.model_validate({
        "metadata": {"manufacturer": "Test", "model": "PSU"},
        "commands": {"set_voltage": {
            "scpi": "VOLT {value}", "type": "write",
            "parameters": [{"name": "value", "type": "float", "range": [0, 10]}],
        }},
        "recipes": {"loop": {"steps": [
            {"command": "set_voltage", "args": {"value": 1.0}},
            {"command": "measure", "instrument": OTHER, "result_as": "measured"},
            {"compute": {"set": "next_value", "expr": "steps.measured + 1"}},
            {"branch": [
                {"when": "vars.next_value > 1.5", "steps": [{
                    "command": "set_voltage",
                    "args": {"value": "${vars.next_value}"},
                }]},
                {"else": True, "steps": [
                    {"compute": {"set": "unchanged", "expr": "0"}},
                ]},
            ]},
        ]}},
    })
    meter_def = InstrumentDefinition.model_validate({
        "metadata": {"manufacturer": "Test", "model": "Meter"},
        "response_formats": {"num": {"fallback": "numeric_extract"}},
        "commands": {"measure": {
            "scpi": "MEAS?", "type": "query",
            "returns": {"type": "float", "format": "num"},
        }},
    })
    primary = _session(primary_def)
    meter = _target_session(OTHER, meter_def)
    visa = _visa()
    plan = recipe_to_plan(
        primary_def.recipes["loop"], {}, primary_resource=PRIMARY,
        definition=primary_def, session_resolver=_resolver(primary, meter),
    )
    assert plan.required_resources == [OTHER, PRIMARY]

    view = dryrun_view(plan)
    assert view["steps"][0]["instrument"] == PRIMARY
    assert view["steps"][1]["instrument"] == OTHER

    result = await execute_recipe(
        visa, primary, "loop", {}, session_resolver=_resolver(primary, meter),
    )

    assert result["success"] is True, result
    assert result["steps_executed"][1]["instrument"] == OTHER
    visa.query.assert_awaited_once()
    assert visa.query.await_args.args[0] == OTHER
    writes = [(call.args[0], call.args[1]) for call in visa.write.await_args_list]
    assert writes == [(PRIMARY, "VOLT 1.0"), (PRIMARY, "VOLT 2.0")]


@pytest.mark.asyncio
async def test_recipe_job_routes_and_locks_cross_instrument(tmp_path):
    from lab_executor.job import JobManager, JobStore
    from lab_executor.job.state_machine import JobStatus

    primary_def = InstrumentDefinition.model_validate({
        "metadata": {"manufacturer": "Test", "model": "Controller"},
        "commands": {},
        "recipes": {"run": {"steps": [{
            "command": "measure", "instrument": OTHER,
        }]}},
    })
    meter_def = InstrumentDefinition.model_validate({
        "metadata": {"manufacturer": "Test", "model": "Meter"},
        "commands": {"measure": {"scpi": "MEAS?", "type": "query"}},
    })
    primary = _session(primary_def)
    meter = _target_session(OTHER, meter_def)
    resolve = _resolver(primary, meter)

    class Sessions:
        get_session = staticmethod(resolve)

    visa = _visa()
    store = JobStore(db_path=tmp_path / "sp8.sqlite")
    manager = JobManager(visa, Sessions(), store=store)
    try:
        rec = await manager.start_recipe_job(PRIMARY, "run", {})
        for _ in range(100):
            current = manager.get(rec.job_id)
            if current.status in (JobStatus.COMPLETED, JobStatus.FAILED):
                break
            await asyncio.sleep(0.02)
        final = manager.get(rec.job_id)
        assert final.status == JobStatus.COMPLETED, final.result
        assert final.result["steps_executed"][0]["instrument"] == OTHER
        visa.query.assert_awaited_once()
        assert visa.query.await_args.args[0] == OTHER
    finally:
        store.close()
