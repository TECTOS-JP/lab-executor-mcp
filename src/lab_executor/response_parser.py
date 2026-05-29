"""
機器応答の構造化パーサ (v0.3.0 → v2.14.0)

YAML の response_formats セクションに定義された正規表現パターンと
フィールドマッピングを用いて、生応答を辞書に構造化する。

v2.14.0 で以下を拡張:
- 複数代替 patterns (例: NTTC / NTKC / OTTC / FTTC / ...) に対応
- 未マッチ時の `fallback: "numeric_extract"` (raw から
  `[+-]?\\d+(\\.\\d+)?(E[+-]?\\d+)?` を抽出して
  `value_numeric` を populate)
- 後方互換: 旧 `pattern` (単一) も引き続き利用可
- マッチした pattern index を `matched_pattern_index` で返す

例: Yokogawa 7563 の "NTKC+00027.2E+0" を
    {"matched": True, "fields": {"status": "Normal", ...},
     "raw": "NTKC+00027.2E+0", "matched_pattern_index": 1}
    のようにパースする。
未知形式 "JPPC+0029*1A+0" を
    {"matched": False, "fields": {},
     "raw": "JPPC+0029*1A+0",
     "value_numeric": 29.0,         # fallback で抽出
     "fallback_used": "numeric_extract"}
"""
from __future__ import annotations
import logging
import re
from typing import Any

from .models.instrument_def import InstrumentDefinition, ResponseFormat

logger = logging.getLogger(__name__)

# v2.14.0: numeric_extract fallback で使う数値抽出 regex
# `+0033.0E+0` / `1.23e-5` / `9999` 等を捕捉
_NUMERIC_RE = re.compile(
    r"[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?"
)


def _parse_value_permissive(raw_val: str, fallback_value: Any) -> Any:
    """v2.14.1: value 文字列を寛容に float 変換する。

    まず通常の `float()` を試し、失敗したら下記の文字置換を適用:
    - `*` → `.` (小数点相当)
    - `A`/`a` → `E`/`e` (指数 marker 相当)
    Yokogawa 7563 で `NTTC+0033.0E+0` ↔ `JPPC+0033*0A+0` のように
    特定文字が ASCII bit 反転で破損するケースに対応。
    両方とも float 化できなければ `fallback_value` をそのまま返す。
    """
    try:
        return float(raw_val)
    except (TypeError, ValueError):
        pass
    try:
        cleaned = (
            str(raw_val).replace("*", ".").replace("A", "E").replace("a", "e")
        )
        return float(cleaned)
    except (TypeError, ValueError):
        return fallback_value


def _try_numeric_extract(raw: str) -> float | None:
    """raw 文字列から最初に出現する数値を抽出して float に変換。
    取れない場合は None。"""
    m = _NUMERIC_RE.search(raw)
    if not m:
        return None
    try:
        return float(m.group(0))
    except (TypeError, ValueError):
        return None


def parse_response(
    raw: str,
    response_format: ResponseFormat,
) -> dict[str, Any]:
    """
    raw 応答を ResponseFormat に基づきパースする。

    返り値の最低保証:
    - 常に `raw` を含む (parser 例外でも失われない)
    - `matched: bool` を含む
    - マッチ時: `fields`, `matched_pattern_index`
    - 未マッチ + fallback=numeric_extract 時: `value_numeric`,
      `fallback_used`
    """
    raw = raw.strip() if isinstance(raw, str) else str(raw)
    patterns = response_format.effective_patterns()
    if not patterns:
        # 何も定義されていなければ fallback だけ試す
        return _fallback_only(raw, response_format)

    compiled: list[re.Pattern] = []
    for p in patterns:
        try:
            compiled.append(re.compile(p))
        except re.error as e:
            logger.warning("response_format の正規表現が不正: %r -> %s", p, e)

    for idx, pat in enumerate(compiled):
        m = pat.match(raw)
        if m is None:
            continue
        captured = m.groupdict()
        fields: dict[str, Any] = {}
        for name, raw_val in captured.items():
            mapping = response_format.fields.get(name, {})
            value: Any = mapping.get(raw_val, raw_val) if mapping else raw_val
            if name == "value" and raw_val is not None:
                value = _parse_value_permissive(raw_val, value)
            fields[name] = value
        out: dict[str, Any] = {
            "matched": True,
            "fields": fields,
            "raw": raw,
            "matched_pattern_index": idx,
        }
        return out

    # どの pattern にもマッチしなかった
    return _fallback_only(raw, response_format)


def _fallback_only(
    raw: str, response_format: ResponseFormat,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "matched": False,
        "fields": {},
        "raw": raw,
    }
    fb = (response_format.fallback or "").strip().lower()
    if fb == "numeric_extract":
        v = _try_numeric_extract(raw)
        if v is not None:
            out["value_numeric"] = v
            out["fallback_used"] = "numeric_extract"
    return out


def parse_with_definition(
    raw: str,
    definition: InstrumentDefinition,
    format_name: str,
) -> dict[str, Any]:
    """機器定義から format_name を引いてパース"""
    rf = definition.response_formats.get(format_name)
    if rf is None:
        return {
            "matched": False,
            "fields": {},
            "raw": raw,
            "error": f"response_format '{format_name}' が定義されていません",
        }
    return parse_response(raw, rf)
