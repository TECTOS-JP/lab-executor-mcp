import pytest
from lab_executor.utils.idn_matcher import parse_idn, match_definition
from lab_executor.models.instrument_def import (
    InstrumentDefinition, MetadataConfig, IdentificationConfig
)


def make_def(manufacturer_match: str, model_regex: str) -> InstrumentDefinition:
    return InstrumentDefinition(
        metadata=MetadataConfig(manufacturer="Test", model="Model"),
        identification=IdentificationConfig(
            manufacturer_match=manufacturer_match,
            model_regex=model_regex,
        ),
    )


def test_parse_idn_full():
    result = parse_idn("TEKTRONIX,TDS 210,C012345,V1.00")
    assert result["manufacturer"] == "TEKTRONIX"
    assert result["model"] == "TDS 210"
    assert result["serial"] == "C012345"
    assert result["firmware"] == "V1.00"


def test_parse_idn_partial():
    result = parse_idn("AGILENT,34401A")
    assert result["manufacturer"] == "AGILENT"
    assert result["model"] == "34401A"
    assert result["serial"] == ""
    assert result["firmware"] == ""


def test_parse_idn_strips_whitespace():
    result = parse_idn("  KEITHLEY , 2400 , SN123 , 1.0  ")
    assert result["manufacturer"] == "KEITHLEY"
    assert result["model"] == "2400"


def test_match_definition_found():
    defn = make_def("TEKTRONIX", r"TDS.?210")
    result = match_definition("TEKTRONIX,TDS 210,C001,V1.00", [defn])
    assert result is defn


def test_match_definition_regex_no_space():
    defn = make_def("TEKTRONIX", r"TDS.?210")
    result = match_definition("TEKTRONIX,TDS210,C001,V1.00", [defn])
    assert result is defn


def test_match_definition_manufacturer_partial():
    defn = make_def("AGILENT", r"34401")
    result = match_definition("AGILENT TECHNOLOGIES,34401A,0,10.5.2", [defn])
    assert result is defn


def test_match_definition_not_found():
    defn = make_def("TEKTRONIX", r"TDS.?210")
    result = match_definition("KEITHLEY,2400,SN001,1.0", [defn])
    assert result is None


def test_match_definition_empty_idn():
    defn = make_def("TEKTRONIX", r"TDS.?210")
    result = match_definition("", [defn])
    assert result is None


def test_match_definition_first_wins():
    defn1 = make_def("TEKTRONIX", r"TDS")
    defn2 = make_def("TEKTRONIX", r"TDS 210")
    result = match_definition("TEKTRONIX,TDS 210,C001,V1.00", [defn1, defn2])
    assert result is defn1  # 最初にマッチした定義が返る
