"""response_parser のテスト"""
from lab_executor.models.instrument_def import ResponseFormat
from lab_executor.response_parser import parse_response


def test_parse_yokogawa_7563_data():
    """7563 の独自フォーマット応答をパース"""
    rf = ResponseFormat(
        pattern=r'^(?P<status>[NFOTBC])(?P<func>[NTRKEJSB])(?P<tc_type>[KCFVA])(?P<unit>[CFKVNA])(?P<value>[+-]\d+\.\d+E[+-]\d+)\s*$',
        description="7563 measurement data",
        fields={
            "status": {"N": "Normal", "O": "Over range"},
            "unit": {"C": "celsius", "F": "fahrenheit", "K": "kelvin"},
        },
    )
    result = parse_response("NTKC+00027.2E+0", rf)
    assert result["matched"] is True
    assert result["fields"]["status"] == "Normal"
    assert result["fields"]["unit"] == "celsius"
    assert result["fields"]["value"] == 27.2


def test_parse_no_match():
    rf = ResponseFormat(
        pattern=r'^(?P<value>\d+)$',
        fields={},
    )
    result = parse_response("not a number", rf)
    assert result["matched"] is False
    assert result["fields"] == {}


def test_parse_value_converted_to_float():
    """value という名前のグループは float に変換される"""
    rf = ResponseFormat(
        pattern=r'^(?P<value>[+-]\d+\.\d+E[+-]\d+)$',
    )
    result = parse_response("+1.50000E+01", rf)
    assert result["matched"] is True
    assert result["fields"]["value"] == 15.0


def test_parse_with_trailing_whitespace():
    rf = ResponseFormat(
        pattern=r'^(?P<value>\d+)$',
    )
    result = parse_response("  42  ", rf)
    # 内部で strip() するので一致
    assert result["matched"] is True


def test_parse_unmapped_field_passes_through():
    """fields に変換マップがない名前は生のまま"""
    rf = ResponseFormat(
        pattern=r'^(?P<header>[A-Z]+)(?P<value>\d+)$',
    )
    result = parse_response("ABC123", rf)
    assert result["fields"]["header"] == "ABC"
    assert result["fields"]["value"] == 123.0
