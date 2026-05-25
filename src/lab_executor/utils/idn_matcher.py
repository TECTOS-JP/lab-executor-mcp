from __future__ import annotations
import re
from lab_executor.models.instrument_def import InstrumentDefinition


def parse_idn(idn_response: str) -> dict[str, str]:
    """*IDN? 応答をパースして辞書で返す。"""
    parts = [p.strip() for p in idn_response.strip().split(",")]
    return {
        "manufacturer": parts[0] if len(parts) > 0 else "",
        "model":        parts[1] if len(parts) > 1 else "",
        "serial":       parts[2] if len(parts) > 2 else "",
        "firmware":     parts[3] if len(parts) > 3 else "",
    }


def match_definition(
    idn_response: str,
    definitions: list[InstrumentDefinition],
) -> InstrumentDefinition | None:
    """
    *IDN? 応答に最もよく一致する InstrumentDefinition を返す。
    一致なしの場合は None。
    """
    parsed = parse_idn(idn_response)
    manufacturer = parsed["manufacturer"].upper()
    model = parsed["model"]

    for defn in definitions:
        ident = defn.identification

        # メーカー照合（大文字・部分一致）
        if ident.manufacturer_match:
            if ident.manufacturer_match.upper() not in manufacturer:
                continue

        # モデル照合（正規表現、大文字小文字無視）
        if ident.model_regex:
            if not re.search(ident.model_regex, model, re.IGNORECASE):
                continue

        return defn

    return None
