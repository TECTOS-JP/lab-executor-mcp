"""SP-9: command failure on_error safe_shutdown policy regressions."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from lab_executor.models.instrument_def import InstrumentDefinition
from lab_executor.recipe_executor import execute_recipe, recipe_to_plan
from visa_mcp.session_manager import InstrumentSession


PRIMARY = "TEST::PSU"
METER = "TEST::DMM"


def _definition(recipes: dict) -> InstrumentDefinition:
    return InstrumentDefinition.model_validate({
        "metadata": {"manufacturer": "Test", "model": "SP9"},
        "commands": {
            "set_voltage": {
                "scpi": "VOLT {value}",
                "type": "write",
                "parameters": [{
                    "name": "value", "type": "float", "range": [0, 5],
                }],
            },
            "output_off": {"scpi": "OUTP OFF", "type": "write"},
        },
        "safe_shutdown": [{"command": "output_off"}],
        "recipes": recipes,
    })


def _session(definition: InstrumentDefinition, resource: str = PRIMARY):
    return InstrumentSession(
        resource_name=resource,
        idn_response="<test>",
        idn_parsed={"manufacturer": "Test", "model": "SP9"},
        definition=definition,
    )


def _visa(query_result="not-numeric"):
    visa = MagicMock()
    visa.write = AsyncMock(return_value=None)
    visa.query = AsyncMock(return_value=query_result)
    return visa


def _resolver(*sessions):
    table = {session.resource_name: session for session in sessions}
    return table.get


@pytest.mark.asyncio
async def test_step_explicit_safe_shutdown_on_parameter_validation_failure():
    definition = _definition({"run": {"steps": [{
        "command": "set_voltage", "args": {"value": 99},
        "on_error": "safe_shutdown",
    }]}})
    visa = _visa()

    result = await execute_recipe(visa, _session(definition), "run", {})

    failed = result["steps_executed"][0]
    assert failed["error"] == "ParameterValidationError"
    assert failed["on_error"] == "safe_shutdown"
    assert failed["safe_shutdown"]["source"] == "yaml"
    visa.write.assert_awaited_once()
    assert visa.write.await_args.args[:2] == (PRIMARY, "OUTP OFF")


@pytest.mark.asyncio
async def test_step_default_abort_does_not_shutdown():
    definition = _definition({"run": {"steps": [{
        "command": "set_voltage", "args": {"value": 99},
    }]}})
    visa = _visa()

    result = await execute_recipe(visa, _session(definition), "run", {})

    failed = result["steps_executed"][0]
    assert failed["error"] == "ParameterValidationError"
    assert failed["on_error"] == "abort"
    assert failed["safe_shutdown"] is None
    visa.write.assert_not_awaited()


@pytest.mark.asyncio
async def test_recipe_default_safe_shutdown_is_inherited():
    definition = _definition({"run": {
        "on_error": "safe_shutdown",
        "steps": [{"command": "set_voltage", "args": {"value": 99}}],
    }})
    plan = recipe_to_plan(
        definition.recipes["run"], {}, primary_resource=PRIMARY,
        definition=definition,
    )
    assert plan.steps[0].on_error == "safe_shutdown"
    visa = _visa()

    result = await execute_recipe(visa, _session(definition), "run", {})

    assert result["steps_executed"][0]["safe_shutdown"]["source"] == "yaml"
    visa.write.assert_awaited_once()


@pytest.mark.asyncio
async def test_step_abort_overrides_recipe_safe_shutdown_default():
    definition = _definition({"run": {
        "on_error": "safe_shutdown",
        "steps": [{
            "command": "set_voltage", "args": {"value": 99},
            "on_error": "abort",
        }],
    }})
    plan = recipe_to_plan(
        definition.recipes["run"], {}, primary_resource=PRIMARY,
        definition=definition,
    )
    assert plan.steps[0].on_error == "abort"
    visa = _visa()

    result = await execute_recipe(visa, _session(definition), "run", {})

    assert result["steps_executed"][0]["safe_shutdown"] is None
    visa.write.assert_not_awaited()


@pytest.mark.asyncio
async def test_cross_instrument_failure_shuts_down_written_psu():
    psu_definition = _definition({"run": {"steps": [
        {"command": "set_voltage", "args": {"value": 1}},
        {
            "command": "bad_measure", "instrument": METER,
            "args": {"channel": 99}, "on_error": "safe_shutdown",
        },
    ]}})
    meter_definition = InstrumentDefinition.model_validate({
        "metadata": {"manufacturer": "Test", "model": "DMM"},
        "commands": {"bad_measure": {
            "scpi": "MEAS? {channel}", "type": "query",
            "parameters": [{
                "name": "channel", "type": "integer", "range": [1, 4],
            }],
        }},
    })
    psu = _session(psu_definition)
    meter = _session(meter_definition, METER)
    visa = _visa()

    result = await execute_recipe(
        visa, psu, "run", {}, session_resolver=_resolver(psu, meter),
    )

    failed = result["steps_executed"][1]
    assert failed["error"] == "ParameterValidationError"
    shutdown = failed["safe_shutdown"]
    assert shutdown["source"] == "yaml"
    calls = [(call.args[0], call.args[1]) for call in visa.write.await_args_list]
    assert calls == [(PRIMARY, "VOLT 1.0"), (PRIMARY, "OUTP OFF")]
    visa.query.assert_not_awaited()


@pytest.mark.asyncio
async def test_deferred_range_violation_still_shutdowns_with_abort_policy():
    definition = _definition({"run": {
        "parameters": [{"name": "value", "type": "float", "required": True}],
        "steps": [{
            "command": "set_voltage",
            "args": {"value": "${params.value}"},
            "on_error": "abort",
        }],
    }})
    visa = _visa()

    result = await execute_recipe(
        visa, _session(definition), "run", {"value": 99},
    )

    failed = result["steps_executed"][0]
    assert failed["error"] == "range_violation"
    assert "on_error" not in failed
    assert failed["safe_shutdown"]["source"] == "yaml"
    visa.write.assert_awaited_once()
    assert visa.write.await_args.args[:2] == (PRIMARY, "OUTP OFF")


@pytest.mark.asyncio
async def test_capture_failure_also_obeys_safe_shutdown_policy():
    definition = InstrumentDefinition.model_validate({
        "metadata": {"manufacturer": "Test", "model": "Capture"},
        "commands": {
            "read": {"scpi": "READ?", "type": "query"},
            "off": {"scpi": "OFF", "type": "write"},
        },
        "safe_shutdown": [{"command": "off"}],
        "recipes": {"run": {"steps": [{
            "command": "read", "result_as": "value",
            "on_error": "safe_shutdown",
        }]}},
    })
    visa = _visa("not-numeric")

    result = await execute_recipe(visa, _session(definition), "run", {})

    failed = result["steps_executed"][0]
    assert failed["error"] == "capture_failed"
    assert failed["safe_shutdown"]["source"] == "yaml"
    visa.query.assert_awaited_once()
    visa.write.assert_awaited_once()


def test_recipe_default_is_inherited_by_called_subsequence():
    definition = InstrumentDefinition.model_validate({
        "metadata": {"manufacturer": "Test", "model": "Call"},
        "commands": {"read": {"scpi": "READ?", "type": "query"}},
        "sequences": {"child": {"steps": [{"command": "read"}]}},
        "recipes": {"run": {
            "on_error": "safe_shutdown",
            "steps": [{"call": {"sequence": "child"}}],
        }},
    })

    plan = recipe_to_plan(
        definition.recipes["run"], {}, primary_resource=PRIMARY,
        definition=definition,
    )

    assert plan.steps[0].sub_steps[0].on_error == "safe_shutdown"


@pytest.mark.asyncio
async def test_job_path_matches_sync_for_safe_shutdown_and_abort(tmp_path):
    from lab_executor.job import JobManager, JobStore
    from lab_executor.job.state_machine import JobStatus

    definition = _definition({
        "safe": {"steps": [{
            "command": "set_voltage", "args": {"value": 99},
            "on_error": "safe_shutdown",
        }]},
        "abort": {"steps": [{
            "command": "set_voltage", "args": {"value": 99},
        }]},
    })
    session = _session(definition)

    class Sessions:
        @staticmethod
        def get_session(resource):
            return session if resource == PRIMARY else None

    visa = _visa()
    store = JobStore(db_path=tmp_path / "sp9.sqlite")
    manager = JobManager(visa, Sessions(), store=store)
    try:
        safe_rec = await manager.start_recipe_job(PRIMARY, "safe", {})
        for _ in range(100):
            safe_final = manager.get(safe_rec.job_id)
            if safe_final.status in (JobStatus.COMPLETED, JobStatus.FAILED):
                break
            await asyncio.sleep(0.02)
        assert safe_final.status == JobStatus.FAILED
        safe_failed = safe_final.result["steps_executed"][0]
        assert safe_failed["on_error"] == "safe_shutdown"
        assert safe_failed["safe_shutdown"]["source"] == "yaml"
        visa.write.assert_awaited_once()

        visa.write.reset_mock()
        abort_rec = await manager.start_recipe_job(PRIMARY, "abort", {})
        for _ in range(100):
            abort_final = manager.get(abort_rec.job_id)
            if abort_final.status in (JobStatus.COMPLETED, JobStatus.FAILED):
                break
            await asyncio.sleep(0.02)
        assert abort_final.status == JobStatus.FAILED
        abort_failed = abort_final.result["steps_executed"][0]
        assert abort_failed["on_error"] == "abort"
        assert abort_failed["safe_shutdown"] is None
        visa.write.assert_not_awaited()
    finally:
        store.close()
