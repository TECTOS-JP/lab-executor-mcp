"""safe_eval (式評価) のテスト"""
import pytest
from lab_executor.utils.expression import safe_eval, resolve_arg, ExpressionError


def test_simple_number():
    assert safe_eval("42", {}) == 42
    assert safe_eval("3.14", {}) == 3.14


def test_variable_reference():
    assert safe_eval("x", {"x": 5.0}) == 5.0


def test_arithmetic():
    vars = {"v": 10.0}
    assert safe_eval("v + 1", vars) == 11.0
    assert safe_eval("v - 2", vars) == 8.0
    assert safe_eval("v * 1.1", vars) == 11.0
    assert safe_eval("v / 2", vars) == 5.0
    assert safe_eval("v * 1.1 + 0.5", vars) == 11.5
    assert safe_eval("(v + 2) * 3", vars) == 36.0


def test_unary_operators():
    assert safe_eval("-x", {"x": 5}) == -5
    assert safe_eval("+x", {"x": 5}) == 5


def test_undefined_variable_raises():
    with pytest.raises(ExpressionError):
        safe_eval("y", {"x": 1})


def test_forbidden_function_call_raises():
    """関数呼び出しは禁止"""
    with pytest.raises(ExpressionError):
        safe_eval("__import__('os')", {})


def test_forbidden_attribute_raises():
    with pytest.raises(ExpressionError):
        safe_eval("x.y", {"x": 1})


def test_forbidden_string_literal():
    with pytest.raises(ExpressionError):
        safe_eval("'evil'", {})


def test_resolve_arg_dollar_prefix():
    assert resolve_arg("$x", {"x": 5}) == 5
    assert resolve_arg("$x * 2", {"x": 5}) == 10


def test_resolve_arg_no_dollar_passes_through():
    """$ で始まらない値はそのまま返る"""
    assert resolve_arg("hello", {}) == "hello"
    assert resolve_arg(42, {}) == 42
    assert resolve_arg(None, {}) is None


def test_resolve_arg_numeric_string_passes_through():
    """数値文字列も $ がなければ文字列のまま"""
    assert resolve_arg("5.0", {}) == "5.0"
