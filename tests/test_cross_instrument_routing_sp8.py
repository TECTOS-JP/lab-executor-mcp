"""SP-8 cross-instrument routing and fail-closed safety regressions."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from lab_executor.models.instrument_def import InstrumentDefinition
from lab_executor.recipe_executor import execute_recipe
from visa_mcp.session_manager import InstrumentSession


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
    return InstrumentSession(
        resource_name=PRIMARY,
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
