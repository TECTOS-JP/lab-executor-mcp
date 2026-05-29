"""v2.14.0 response_parser 拡張テスト.

実機で観測した形式を全部 fixture 化して、parser が:
- 厳密 pattern にマッチ → matched=True + fields 構造化
- 緩い pattern にマッチ → matched=True + prefix 文字列保持
- どちらも未マッチ + fallback=numeric_extract → matched=False +
  value_numeric 抽出
- raw は常に保持
となることを確認する。
"""
from __future__ import annotations
import pytest

from lab_executor.models.instrument_def import ResponseFormat
from lab_executor.response_parser import (
    parse_response, _try_numeric_extract,
)


# ===== 7563 measurement_data fixture =====

YOKOGAWA_7563_FORMAT = ResponseFormat(
    patterns=[
        # 厳密形 (status + func + tc_type + unit + 値)
        # tc_type は実機で K/T/R/J/S 等が観測されるため [A-Z] で受ける。
        r'^(?P<status>[NFOTBC])(?P<func>[NTRKEJSB])(?P<tc_type>[A-Z])(?P<unit>[CFKVNA])(?P<value>[+-]\d+\.\d+E[+-]\d+)\s*$',
        # 緩い 4 文字 prefix
        r'^(?P<prefix>[A-Z]{4})(?P<value>[+-]\d+\.\d+E[+-]\d+)\s*$',
    ],
    fallback="numeric_extract",
    fields={
        "status": {
            "N": "Normal", "O": "Over range", "F": "Failure",
            "T": "Trigger waiting", "B": "Burnout detected",
        },
        "unit": {
            "C": "celsius", "F": "fahrenheit", "K": "kelvin",
            "V": "volt", "N": "none / unitless",
        },
    },
)


# ===== 厳密 pattern (Normal) =====

@pytest.mark.parametrize("raw,expected_value,expected_status", [
    ("NTKC+00027.2E+0", 27.2, "Normal"),
    ("NTTC+0033.0E+0", 33.0, "Normal"),
    ("NTTC+0028.8E+0", 28.8, "Normal"),
])
def test_strict_pattern_normal(raw, expected_value, expected_status):
    r = parse_response(raw, YOKOGAWA_7563_FORMAT)
    assert r["matched"] is True
    assert r["raw"] == raw
    assert r["matched_pattern_index"] == 0
    assert r["fields"]["status"] == expected_status
    assert r["fields"]["value"] == pytest.approx(expected_value)


# ===== 厳密 pattern (Over / Failure) =====

def test_strict_pattern_over_range():
    """OTTC は熱電対オープン時の Over range レスポンス (実機観測)"""
    r = parse_response("OTTC+9999.9E+0", YOKOGAWA_7563_FORMAT)
    assert r["matched"] is True
    assert r["fields"]["status"] == "Over range"
    assert r["fields"]["value"] == pytest.approx(9999.9)


# ===== 緩い pattern (未確認 prefix だが値は浮く) =====

def test_loose_pattern_unknown_prefix():
    """JPPC のような prefix も 緩い pattern なら value を取れる
    (ASCII corruption の可能性を残しつつ救済)"""
    # JPPC+0029.0E+0 は緩い pattern にマッチする
    r = parse_response("JPPC+0029.0E+0", YOKOGAWA_7563_FORMAT)
    assert r["matched"] is True
    assert r["matched_pattern_index"] == 1, (
        f"緩い patten (index 1) にマッチすべき: {r}")
    assert r["fields"]["prefix"] == "JPPC"
    assert r["fields"]["value"] == pytest.approx(29.0)


# ===== fallback (どちらの pattern にも当たらない) =====

def test_fallback_numeric_extract_corrupted_value():
    """JPPC+0029*1A+0 のように小数点/指数部が崩れた raw でも
    fallback で先頭の数値だけ救う。matched=False を維持。"""
    r = parse_response("JPPC+0029*1A+0", YOKOGAWA_7563_FORMAT)
    assert r["matched"] is False
    assert r["raw"] == "JPPC+0029*1A+0"
    assert r.get("fallback_used") == "numeric_extract"
    # 最初に抽出される数値は +0029 = 29.0
    assert r["value_numeric"] == pytest.approx(29.0)


def test_fallback_numeric_extract_arbitrary_text():
    r = parse_response("garbage text", YOKOGAWA_7563_FORMAT)
    assert r["matched"] is False
    assert r["raw"] == "garbage text"
    # 数値も無い場合 fallback_used / value_numeric は無い
    assert "value_numeric" not in r


# ===== raw 保持の保証 =====

@pytest.mark.parametrize("raw", [
    "NTKC+00027.2E+0",
    "JPPC+0029*1A+0",
    "completely unparseable garbage",
    "",
])
def test_raw_always_preserved(raw):
    r = parse_response(raw, YOKOGAWA_7563_FORMAT)
    assert r["raw"] == raw.strip()


# ===== 後方互換: 旧 pattern (単一) も動く =====

def test_legacy_single_pattern_still_works():
    legacy = ResponseFormat(
        pattern=r"^(?P<value>\d+\.\d+)$",
        fallback="",
    )
    r = parse_response("123.45", legacy)
    assert r["matched"] is True
    assert r["fields"]["value"] == pytest.approx(123.45)


# ===== effective_patterns: patterns 優先 =====

def test_effective_patterns_priority():
    rf = ResponseFormat(
        pattern=r"^old$",
        patterns=[r"^new1$", r"^new2$"],
    )
    assert rf.effective_patterns() == [r"^new1$", r"^new2$"]


def test_effective_patterns_falls_back_to_pattern():
    rf = ResponseFormat(pattern=r"^only$")
    assert rf.effective_patterns() == [r"^only$"]


def test_effective_patterns_empty():
    rf = ResponseFormat()
    assert rf.effective_patterns() == []


# ===== _try_numeric_extract 内部関数 =====

@pytest.mark.parametrize("raw,expected", [
    ("+0033.0E+0", 33.0),
    ("-1.23e-5", -1.23e-5),
    ("9999", 9999.0),
    ("NTKC+00027.2E+0", 27.2),  # 文字列中の最初の数値
    ("no number here", None),
    ("", None),
])
def test_try_numeric_extract(raw, expected):
    got = _try_numeric_extract(raw)
    if expected is None:
        assert got is None
    else:
        assert got == pytest.approx(expected)


# ===== version sentinel =====

def test_v2_14_0_version():
    import lab_executor
    parts = lab_executor.__version__.split(".")
    assert tuple(int(p) for p in parts[:3]) >= (2, 14, 0)
