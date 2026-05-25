"""
v0.7.0: state_query 実行ロジック

機器定義の state_query セクションに従って状態を取得し、構造化辞書を返す。
get_state / describe_instrument / get_last_measurement で共有される。
"""
from __future__ import annotations
import asyncio
import logging
from typing import Any, TYPE_CHECKING

from .models.instrument_def import StateQueryItem
from .response_parser import parse_response


# v2.0: backend layer は visa-mcp / shim 経由。型ヒント目的
# は TYPE_CHECKING、VisaError は ImportError fallback。
if TYPE_CHECKING:
    from visa_mcp.session_manager import InstrumentSession
    from visa_mcp.visa_manager import VisaManager

try:
    from visa_mcp.visa_manager import VisaError
except ImportError:
    class VisaError(Exception):  # type: ignore[no-redef]
        """visa-mcp 不在時の VisaError 代替"""
        pass

logger = logging.getLogger(__name__)


def _extract_value(raw: str, parsed: dict | None, value_path: str) -> Any:
    """state_query 用の値抽出。

    value_path 指定があれば parsed[value_path]、なければ parsed["value"]、
    それも無ければ raw を float / 文字列の順で返す。
    """
    if value_path and parsed and value_path in parsed:
        return parsed[value_path]
    if parsed and "value" in parsed:
        return parsed["value"]
    if parsed:
        numeric = []
        for k, v in parsed.items():
            try:
                float(v)
                numeric.append(k)
            except (TypeError, ValueError):
                pass
        if len(numeric) == 1:
            return parsed[numeric[0]]
    s = str(raw).strip()
    try:
        return float(s)
    except (TypeError, ValueError):
        return s


async def query_state_item(
    visa: VisaManager,
    session: InstrumentSession,
    key: str,
    item: StateQueryItem,
) -> dict[str, Any]:
    """state_query の 1 項目を取得して構造化辞書を返す"""
    cmd_def = session.definition.commands.get(item.command)
    if cmd_def is None:
        return {
            "key": key, "value": None, "unit": item.unit,
            "error": "command_not_found",
            "message": f"state_query.{key}.command='{item.command}' は未定義",
        }
    if cmd_def.type != "query":
        return {
            "key": key, "value": None, "unit": item.unit,
            "error": "not_query_type",
            "message": (
                f"state_query.{key}.command='{item.command}' は query 型である必要があります"
            ),
        }
    conn = session.definition.connection
    timeout_ms = cmd_def.timeout_ms or conn.default_timeout_ms
    try:
        raw = await visa.query(
            session.resource_name, cmd_def.scpi, timeout_ms=timeout_ms,
            read_termination=conn.read_termination,
            write_termination=conn.write_termination,
        )
    except VisaError as e:
        return {
            "key": key, "value": None, "unit": item.unit,
            "error": type(e).__name__, "message": str(e),
        }

    parsed: dict | None = None
    if cmd_def.returns and cmd_def.returns.format:
        fmt = session.definition.response_formats.get(cmd_def.returns.format)
        if fmt is not None:
            p = parse_response(raw, fmt)
            if p.get("matched"):
                parsed = p.get("fields") or {}

    value = _extract_value(raw, parsed, item.value_path)
    # 表示マッピング (raw 文字列 → 表示値)
    if item.map and isinstance(value, str) and value in item.map:
        value = item.map[value]
    elif item.map and isinstance(value, (int, float)):
        # "1" / "0" のような数値文字列 keyed map に対応
        s = str(int(value)) if float(value).is_integer() else str(value)
        if s in item.map:
            value = item.map[s]

    return {
        "key": key,
        "value": value,
        "unit": item.unit,
        "raw_response": raw,
    }


async def query_all_state(
    visa: VisaManager,
    session: InstrumentSession,
    keys: list[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """state_query 全項目 (or 指定 keys) を取得"""
    if session.definition is None:
        return {}
    sq = session.definition.state_query
    if not sq:
        return {}
    if keys is None:
        target_keys = list(sq.keys())
    else:
        target_keys = [k for k in keys if k in sq]
    results: dict[str, dict[str, Any]] = {}
    # 直列実行 (同一機器への lock 競合を避ける)
    for k in target_keys:
        results[k] = await query_state_item(visa, session, k, sq[k])
    return results
