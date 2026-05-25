import pytest
from lab_executor.utils.param_validator import validate_and_build_scpi, ParameterValidationError
from lab_executor.models.instrument_def import CommandDefinition, ParameterDefinition


def make_cmd(scpi: str, params: list[dict]) -> CommandDefinition:
    return CommandDefinition(
        scpi=scpi,
        type="query",
        description="test",
        parameters=[ParameterDefinition(**p) for p in params],
    )


def test_integer_valid():
    cmd = make_cmd("MEAS:VOLT? CH{channel}", [{"name": "channel", "type": "integer", "range": [1, 4]}])
    result = validate_and_build_scpi(cmd, {"channel": 2})
    assert result == "MEAS:VOLT? CH2"


def test_integer_out_of_range():
    cmd = make_cmd("MEAS:VOLT? CH{channel}", [{"name": "channel", "type": "integer", "range": [1, 4]}])
    with pytest.raises(ParameterValidationError, match="channel"):
        validate_and_build_scpi(cmd, {"channel": 5})


def test_float_valid():
    cmd = make_cmd("HORI:SCAL {scale}", [{"name": "scale", "type": "float", "range": [1e-9, 5.0]}])
    result = validate_and_build_scpi(cmd, {"scale": 0.001})
    assert result == "HORI:SCAL 0.001"


def test_float_out_of_range():
    cmd = make_cmd("HORI:SCAL {scale}", [{"name": "scale", "type": "float", "range": [1e-9, 5.0]}])
    with pytest.raises(ParameterValidationError):
        validate_and_build_scpi(cmd, {"scale": 10.0})


def test_enum_valid():
    cmd = make_cmd("COUP {coupling}", [{"name": "coupling", "type": "enum", "choices": ["AC", "DC", "GND"]}])
    result = validate_and_build_scpi(cmd, {"coupling": "DC"})
    assert result == "COUP DC"


def test_enum_invalid():
    cmd = make_cmd("COUP {coupling}", [{"name": "coupling", "type": "enum", "choices": ["AC", "DC"]}])
    with pytest.raises(ParameterValidationError, match="AC.*DC"):
        validate_and_build_scpi(cmd, {"coupling": "INVALID"})


def test_missing_required_param():
    cmd = make_cmd("MEAS:VOLT? CH{channel}", [{"name": "channel", "type": "integer", "range": [1, 4]}])
    with pytest.raises(ParameterValidationError, match="channel"):
        validate_and_build_scpi(cmd, {})


def test_optional_param_with_default():
    cmd = make_cmd(
        "MEAS:VOLT? CH{channel}",
        [{"name": "channel", "type": "integer", "range": [1, 4], "required": False, "default": 1}],
    )
    result = validate_and_build_scpi(cmd, {})
    assert result == "MEAS:VOLT? CH1"


def test_no_params():
    cmd = make_cmd("*IDN?", [])
    result = validate_and_build_scpi(cmd, {})
    assert result == "*IDN?"
