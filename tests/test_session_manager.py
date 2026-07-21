from unittest.mock import MagicMock
from lab_visa_mcp.session_manager import SessionManager
from lab_executor.instrument_registry import InstrumentRegistry


def test_bind_manually_success(tmp_path):
    yaml_content = """
metadata:
  manufacturer: "Yokogawa"
  model: "7563"
  description: "test"
commands:
  trigger:
    scpi: "E"
    type: "write"
    description: "trigger"
"""
    (tmp_path / "yokogawa_7563.yaml").write_text(yaml_content, encoding="utf-8")
    registry = InstrumentRegistry(tmp_path)
    visa_mgr = MagicMock()
    mgr = SessionManager(visa_mgr, registry)

    session = mgr.bind_manually("GPIB0::1::INSTR", "Yokogawa", "7563")
    assert session is not None
    assert session.definition.display_name == "Yokogawa 7563"
    assert session.idn_response == "<manual binding>"
    assert mgr.get_session("GPIB0::1::INSTR") is session


def test_bind_manually_not_found(tmp_path):
    registry = InstrumentRegistry(tmp_path)
    visa_mgr = MagicMock()
    mgr = SessionManager(visa_mgr, registry)

    session = mgr.bind_manually("GPIB0::1::INSTR", "Unknown", "Model")
    assert session is None
    assert mgr.get_session("GPIB0::1::INSTR") is None
