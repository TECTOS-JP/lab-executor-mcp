"""v0.4.x raw command 安全対策のテスト"""
import pytest
from lab_executor.tools.commands import _detect_dangerous_keywords


def test_query_form_is_safe():
    """pure query (? あり、; なし) は危険キーワード扱いされない"""
    assert _detect_dangerous_keywords("*IDN?") == []
    assert _detect_dangerous_keywords("MEAS:VOLT?") == []
    assert _detect_dangerous_keywords("SYST:ERR?") == []
    assert _detect_dangerous_keywords("VOLT?") == []
    assert _detect_dangerous_keywords("CURR?") == []
    assert _detect_dangerous_keywords("OUTP?") == []
    assert _detect_dangerous_keywords("SYST:VERS?") == []


def test_dangerous_write_detected():
    """状態変更コマンドは検出される (short form)"""
    assert "VOLT" in _detect_dangerous_keywords("VOLT 5.0")
    assert "*RST" in _detect_dangerous_keywords("*RST")
    assert "OUTP" in _detect_dangerous_keywords("OUTP ON")
    assert "CURR" in _detect_dangerous_keywords("CURR 1.5")


@pytest.mark.parametrize("cmd,label", [
    ("VOLTAGE 10",            "VOLT"),
    ("CURRENT 1",             "CURR"),
    ("OUTPUT ON",             "OUTP"),
    ("SOURCE:VOLTAGE 10",     "VOLT"),
    ("SOURce:CURRent 1",      "CURR"),
    ("CONFIGURE:VOLTAGE",     "CONF"),
    ('FUNCTION "VOLTAGE"',    "FUNC"),
    ("RANGE 10",              "RANG"),
    ("INITIATE",              "INIT"),
    ("TRIGGER",               "TRIG"),
    ("MEMORY:SAVE 1",         "MEM"),
    ("STORE 1",               "STOR"),
])
def test_longform_dangerous_detected(cmd, label):
    """SCPI ロングフォームが短縮形と同様に検出される (v0.4.1)"""
    hits = _detect_dangerous_keywords(cmd)
    assert label in hits, f"'{cmd}' から '{label}' が検出されるべき。実際: {hits}"


@pytest.mark.parametrize("cmd", [
    "CONF:VOLT;READ?",   # 書き込みを含む複合コマンド
    "INIT;*OPC?",        # INITIATE + OPC query
    "VOLT 10;*OPC?",     # write + sync query
    "SOURCE:VOLTAGE 5;OUTPUT ON",
])
def test_compound_command_with_query_detected(cmd):
    """; を含む複合コマンドは ? があっても危険キーワードを検出する (v0.4.1)"""
    assert _detect_dangerous_keywords(cmd) != [], f"'{cmd}' は危険と判定されるべき"


def test_multiple_keywords_detected():
    hits = _detect_dangerous_keywords("VOLT 5; OUTP ON")
    assert "VOLT" in hits
    assert "OUTP" in hits


def test_benign_commands_not_detected():
    """無害なコマンドは検出されない"""
    assert _detect_dangerous_keywords("") == []
    assert _detect_dangerous_keywords("SYST:VERS?") == []


def test_dangerous_keywords_module_import():
    """モジュールが import できる (環境変数で raw 機能の有効/無効が切り替わる)"""
    from lab_executor.tools import commands
    assert hasattr(commands, "RAW_COMMANDS_ENABLED")
    assert hasattr(commands, "_DANGEROUS_PATTERNS")
    labels = [label for label, _ in commands._DANGEROUS_PATTERNS]
    assert "VOLT" in labels
    assert "*RST" in labels
