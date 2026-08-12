"""``list_commands`` provided by the runtime itself, not only by visa-mcp.

The stability matrix has always listed this tool under "Core / instrument", but
only ``visa-mcp`` registered it. Every other backend was therefore unable to say
which of an instrument's commands operate it and which only read — a console
showing them all as operations hides the genuinely dangerous ones among the safe
ones. Nothing in the tool needs a transport: it reads the bound definition.
"""

from __future__ import annotations

import textwrap

import pytest
import yaml

from lab_executor.models.instrument_def import InstrumentDefinition
from lab_executor.server import compose_server
from lab_executor.stability import STABLE_TOOLS, flatten

SAMPLE_YAML = """
metadata:
  manufacturer: National Instruments
  model: USB-6009
  category: daq
  support_level: experimental
  definition_version: "0.1.0"
commands:
  read_ai0:
    scpi: "READ AI ai0"
    type: query
    description: 単点のアナログ入力を読む
    returns: {type: float, unit: V}
  write_ao0:
    scpi: "WRITE AO ao0 {value}"
    type: write
    description: アナログ出力を設定する
    parameters:
      - {name: value, type: float, required: true, description: 出力電圧}
"""


class _Session:
    def __init__(self, definition):
        self.definition = definition
        self.command_history: list = []


def _definition() -> InstrumentDefinition:
    return InstrumentDefinition(**yaml.safe_load(textwrap.dedent(SAMPLE_YAML)))


async def _call(mcp, name, arguments):
    result = await mcp.call_tool(name, arguments)
    data = getattr(result, "structured_content", None)
    if isinstance(data, dict) and set(data) == {"result"}:
        return data["result"]
    return data


def test_list_commands_is_registered_by_the_runtime():
    """It is promised in the stability matrix, so the runtime must provide it."""
    assert "list_commands" in flatten(STABLE_TOOLS)


@pytest.mark.asyncio
async def test_registered_tool_surface_includes_list_commands():
    mcp, _ = compose_server(None, name="test")
    assert "list_commands" in {tool.name for tool in await mcp.list_tools()}


@pytest.mark.asyncio
async def test_command_types_distinguish_reads_from_operations():
    """The console needs this to warn about commands that change the device."""
    mcp, job_mgr = compose_server(None, name="test")
    job_mgr.session_manager.register_session("DAQ::Dev2", _Session(_definition()))

    body = await _call(mcp, "list_commands", {"resource_name": "DAQ::Dev2"})
    assert body["success"] is True
    commands = body["data"]["commands"]
    assert commands["read_ai0"]["type"] == "query"
    assert commands["write_ao0"]["type"] == "write"
    assert commands["read_ai0"]["returns"]["unit"] == "V"
    assert commands["write_ao0"]["parameters"][0]["name"] == "value"
    assert body["data"]["instrument"] == "National Instruments USB-6009"


@pytest.mark.asyncio
async def test_an_unbound_resource_reports_that_plainly():
    mcp, _ = compose_server(None, name="test")
    body = await _call(mcp, "list_commands", {"resource_name": "DAQ::Nope"})
    assert body["success"] is False
    assert body["error"] == "SessionNotFound"


@pytest.mark.asyncio
async def test_a_session_without_a_definition_is_distinguished():
    """Bound but undefined is a different problem from not bound at all."""
    mcp, job_mgr = compose_server(None, name="test")
    job_mgr.session_manager.register_session("DAQ::Bare", _Session(None))
    body = await _call(mcp, "list_commands", {"resource_name": "DAQ::Bare"})
    assert body["success"] is False
    assert body["error"] == "NoDefinitionFound"
