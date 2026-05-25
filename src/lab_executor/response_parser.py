"""
機器応答の構造化パーサ (v0.3.0)

YAML の response_formats セクションに定義された正規表現パターンと
フィールドマッピングを用いて、生応答を辞書に構造化する。

例: Yokogawa 7563 の "NTKC+00027.2E+0" を
    {"status": "Normal", "func": "T", "type": "K", "unit": "celsius", "value": 27.2}
    のようにパースする。
"""
from __future__ import annotations
import logging
import re
from typing import Any

from .models.instrument_def import InstrumentDefinition, ResponseFormat

logger = logging.getLogger(__name__)


def parse_response(
    raw: str,
    response_format: ResponseFormat,
) -> dict[str, Any]:
    """
    raw 応答を ResponseFormat に基づきパースする。

    返り値: {"matched": bool, "fields": {...}, "raw": "..."} 形式の辞書
    マッチしない場合は matched=False, fields は空辞書
    """
    raw = raw.strip()
    try:
        pattern = re.compile(response_format.pattern)
    except re.error as e:
        logger.warning("response_format の正規表現が不正: %s", e)
        return {"matched": False, "fields": {}, "raw": raw, "error": str(e)}

    m = pattern.match(raw)
    if m is None:
        return {"matched": False, "fields": {}, "raw": raw}

    captured = m.groupdict()
    fields: dict[str, Any] = {}

    for name, raw_val in captured.items():
        # フィールド変換マッピングを適用
        mapping = response_format.fields.get(name, {})
        # マッピングがあれば値を変換、なければ生のまま
        value = mapping.get(raw_val, raw_val) if mapping else raw_val

        # value という名前のグループは数値として解釈を試みる
        if name == "value":
            try:
                value = float(raw_val)
            except (TypeError, ValueError):
                pass

        fields[name] = value

    return {"matched": True, "fields": fields, "raw": raw}


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
