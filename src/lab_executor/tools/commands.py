from __future__ import annotations
import logging
import os
import re
from typing import TYPE_CHECKING

from fastmcp import FastMCP
from lab_executor.utils.param_validator import validate_and_build_scpi, ParameterValidationError
from lab_executor import safety as sf
from lab_executor.response_parser import parse_with_definition


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

# v0.4.0: raw SCPI を許可するか (環境変数オプトイン、デフォルト無効)
RAW_COMMANDS_ENABLED = os.environ.get("VISA_MCP_ENABLE_RAW_COMMANDS", "0").strip() == "1"

# 危険キーワード（write 系: 機器状態を変更する可能性が高い）
# ロングフォームも検出するため (?:...) で短縮形と正式表記を両方カバーする
_DANGEROUS_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("*RST",   re.compile(r"(?<![A-Z0-9])\*RST(?![A-Z0-9])")),
    ("*CLS",   re.compile(r"(?<![A-Z0-9])\*CLS(?![A-Z0-9])")),
    ("*SAV",   re.compile(r"(?<![A-Z0-9])\*SAV(?![A-Z0-9])")),
    ("VOLT",   re.compile(r"(?<![A-Z0-9])VOLT(?:AGE)?(?![A-Z0-9])")),
    ("CURR",   re.compile(r"(?<![A-Z0-9])CURR(?:ENT)?(?![A-Z0-9])")),
    ("OUTP",   re.compile(r"(?<![A-Z0-9])OUTP(?:UT)?(?![A-Z0-9])")),
    ("SOUR",   re.compile(r"(?<![A-Z0-9])SOUR(?:CE)?(?![A-Z0-9])")),
    ("CONF",   re.compile(r"(?<![A-Z0-9])CONF(?:IGURE)?(?![A-Z0-9])")),
    ("FUNC",   re.compile(r"(?<![A-Z0-9])FUNC(?:TION)?(?![A-Z0-9])")),
    ("RANG",   re.compile(r"(?<![A-Z0-9])RANG(?:E)?(?![A-Z0-9])")),
    ("INIT",   re.compile(r"(?<![A-Z0-9])INIT(?:IATE)?(?![A-Z0-9])")),
    ("TRIG",   re.compile(r"(?<![A-Z0-9])TRIG(?:GER)?(?![A-Z0-9])")),
    ("MEM",    re.compile(r"(?<![A-Z0-9])MEM(?:ORY)?(?![A-Z0-9])")),
    ("STOR",   re.compile(r"(?<![A-Z0-9])STOR(?:E)?(?![A-Z0-9])")),
    ("RECALL", re.compile(r"(?<![A-Z0-9])RECALL(?![A-Z0-9])")),
]


def _detect_dangerous_keywords(command: str) -> list[str]:
    """SCPI 文字列に危険キーワードが含まれるか検出する。

    short form (VOLT) と long form (VOLTAGE) の両方を検出する。

    スキップ条件: '?' を含み ';' を含まない pure query はスキップ。
    ';' を含む複合コマンドは '?' があっても検査対象とする
    (例: CONF:VOLT;READ? や INIT;*OPC? は書き込みを含む)。
    """
    cmd_upper = command.upper().strip()
    if "?" in cmd_upper and ";" not in cmd_upper:
        return []
    hits = []
    for label, pattern in _DANGEROUS_PATTERNS:
        if pattern.search(cmd_upper):
            hits.append(label)
    return hits


def register_tools(mcp: FastMCP, session_mgr: SessionManager) -> None:

    mode = sf.get_safety_mode()

    # ===== Raw (任意 SCPI) ツールは strict モードでは登録しない =====
    if RAW_COMMANDS_ENABLED and mode != "strict":
        logger.warning(
            "VISA_MCP_ENABLE_RAW_COMMANDS=1 のため unsafe_send_command / unsafe_query_instrument "
            "を登録します。これらは YAML 安全制約を介しません。利用には十分注意してください。"
        )

        @mcp.tool()
        async def unsafe_query_instrument(
            resource_name: str,
            command: str,
            timeout_ms: int = 5000,
            override_safety: bool = False,
            override_reason: str = "",
        ) -> dict:
            """
            [DANGEROUS] 任意の SCPI クエリを送信し応答を返す。
            YAML 定義の検証・安全制約・パラメータ範囲チェックを介さない。
            VISA_MCP_ENABLE_RAW_COMMANDS=1 のときのみ利用可能。

            危険キーワード (VOLT/CURR/OUTP/*RST 等) を含むコマンドは override_safety=True
            と override_reason の指定が必要。

            resource_name: VISA リソース文字列
            command: 任意の SCPI 文字列
            timeout_ms: タイムアウト (ms)
            override_safety: 危険キーワードを含む場合の override (advisory モードのみ有効)
            override_reason: override 理由 (override_safety=True 時に必須)
            """
            session = session_mgr.get_session(resource_name)
            conn = session.definition.connection if (session and session.definition) else None
            hits = _detect_dangerous_keywords(command)

            # 危険キーワードがある場合は safety と同じ判定ロジック
            if hits:
                violations = [sf.SafetyViolation(
                    "raw_dangerous_keyword",
                    f"raw コマンド '{command}' に危険キーワード {hits} を検出",
                    severity="high",
                    recommendation="execute_named_command を使うか、override_safety=True + override_reason で再実行",
                )]
                action, msg = sf.decide_action(violations, mode, override_safety, override_reason or None)
                sf.write_audit(
                    resource_name, "unsafe_query_instrument",
                    {"command": command}, violations,
                    action=action, mode=mode,
                    override_safety=override_safety, override_reason=override_reason or None,
                )
                if action in ("block_advisory", "block_strict"):
                    resp = sf.make_warning_response(violations, action, mode)
                    resp["message"] = msg
                    return resp

            # 監査ログ (危険キーワードなしでも記録)
            if not hits:
                sf.write_audit(
                    resource_name, "unsafe_query_instrument",
                    {"command": command}, [],
                    action="proceed", mode=mode,
                    override_safety=False, override_reason=None,
                )

            try:
                response = await session_mgr._visa.query(
                    resource_name, command, timeout_ms=timeout_ms,
                    read_termination=conn.read_termination if conn else "\n",
                    write_termination=conn.write_termination if conn else "\n",
                )
                return {
                    "success": True,
                    "data": {"response": response, "command": command},
                    "dangerous_keywords_detected": hits,
                }
            except VisaError as e:
                return {"success": False, "error": type(e).__name__, "message": str(e), "resource_name": resource_name}

        @mcp.tool()
        async def unsafe_send_command(
            resource_name: str,
            command: str,
            timeout_ms: int = 5000,
            override_safety: bool = False,
            override_reason: str = "",
        ) -> dict:
            """
            [DANGEROUS] 応答を読まない任意の SCPI コマンドを送信する (write)。
            YAML 定義の検証・安全制約を介さない。
            VISA_MCP_ENABLE_RAW_COMMANDS=1 のときのみ利用可能。

            危険キーワード (VOLT/CURR/OUTP/*RST 等) を含むコマンドは override_safety=True
            と override_reason の指定が必要。
            """
            session = session_mgr.get_session(resource_name)
            conn = session.definition.connection if (session and session.definition) else None
            hits = _detect_dangerous_keywords(command)

            if hits:
                violations = [sf.SafetyViolation(
                    "raw_dangerous_keyword",
                    f"raw コマンド '{command}' に危険キーワード {hits} を検出",
                    severity="high",
                    recommendation="execute_named_command を使うか、override_safety=True + override_reason で再実行",
                )]
                action, msg = sf.decide_action(violations, mode, override_safety, override_reason or None)
                sf.write_audit(
                    resource_name, "unsafe_send_command",
                    {"command": command}, violations,
                    action=action, mode=mode,
                    override_safety=override_safety, override_reason=override_reason or None,
                )
                if action in ("block_advisory", "block_strict"):
                    resp = sf.make_warning_response(violations, action, mode)
                    resp["message"] = msg
                    return resp

            if not hits:
                sf.write_audit(
                    resource_name, "unsafe_send_command",
                    {"command": command}, [],
                    action="proceed", mode=mode,
                    override_safety=False, override_reason=None,
                )

            try:
                await session_mgr._visa.write(
                    resource_name, command, timeout_ms=timeout_ms,
                    read_termination=conn.read_termination if conn else "\n",
                    write_termination=conn.write_termination if conn else "\n",
                )
                return {
                    "success": True,
                    "data": {"command": command, "resource_name": resource_name},
                    "dangerous_keywords_detected": hits,
                }
            except VisaError as e:
                return {"success": False, "error": type(e).__name__, "message": str(e), "resource_name": resource_name}

    # ===== execute_named_command (YAML 定義経由、常に登録) =====

    @mcp.tool()
    async def execute_named_command(
        resource_name: str,
        command_name: str,
        parameters: dict = {},
        override_safety: bool = False,
        override_reason: str = "",
    ) -> dict:
        """
        YAML 定義に登録された名前付きコマンドを実行する。
        パラメータを型・範囲検証してから SCPI 文字列を組み立てて送信する。
        さらに YAML の safety セクションに基づく安全制約検証を行う。

        resource_name: VISA リソース文字列
        command_name: YAML 定義のコマンドキー（例: "measure_voltage"）
        parameters: コマンドパラメータ辞書（例: {"channel": 1}）
        override_safety: 安全警告を無視して実行する場合 True（advisory モード時のみ有効）
        override_reason: override する理由（override_safety=True の場合は必須）
        """
        session = session_mgr.get_session(resource_name)
        if session is None:
            return {
                "success": False,
                "error": "SessionNotFound",
                "message": f"{resource_name} はまだ識別されていません。identify_instrument を先に実行してください。",
            }
        if session.definition is None:
            return {
                "success": False,
                "error": "NoDefinitionFound",
                "message": f"{resource_name} の YAML 定義が見つかりません。",
            }

        cmd_def = session.definition.commands.get(command_name)
        if cmd_def is None:
            available = list(session.definition.commands.keys())
            return {
                "success": False,
                "error": "CommandNotFound",
                "message": f"コマンド '{command_name}' は定義されていません。利用可能: {available}",
            }

        # 安全制約チェック (v0.2.0)
        current_mode = sf.get_safety_mode()
        violations = sf.validate(
            session.definition,
            command_name,
            parameters,
            session_history=session.command_history,
        )
        action, msg = sf.decide_action(violations, current_mode, override_safety, override_reason or None)

        if action in ("block_advisory", "block_strict"):
            sf.write_audit(
                resource_name, command_name, parameters, violations,
                action=action, mode=current_mode,
                override_safety=override_safety, override_reason=override_reason or None,
            )
            resp = sf.make_warning_response(violations, action, current_mode)
            resp["message"] = msg
            return resp

        if violations:
            sf.write_audit(
                resource_name, command_name, parameters, violations,
                action="proceed_with_override" if override_safety else "proceed_permissive",
                mode=current_mode,
                override_safety=override_safety, override_reason=override_reason or None,
            )

        try:
            scpi = validate_and_build_scpi(cmd_def, parameters)
        except ParameterValidationError as e:
            return {"success": False, "error": "ParameterValidationError", "message": str(e)}

        conn = session.definition.connection
        timeout_ms = cmd_def.timeout_ms or conn.default_timeout_ms

        try:
            if cmd_def.type == "query":
                raw = await session_mgr._visa.query(
                    resource_name, scpi, timeout_ms=timeout_ms,
                    read_termination=conn.read_termination,
                    write_termination=conn.write_termination,
                )
                result = _cast_response(raw, cmd_def.returns.type)
                session.record_command(command_name)
                data = {
                    "command_name": command_name,
                    "scpi_sent": scpi,
                    "raw_response": raw,
                    "value": result,
                    "unit": cmd_def.returns.unit,
                }
                if cmd_def.returns.format:
                    parsed = parse_with_definition(raw, session.definition, cmd_def.returns.format)
                    data["parsed"] = parsed
                return {
                    "success": True,
                    "data": data,
                    "safety_violations_overridden": list(violations) if violations else [],
                }
            else:
                await session_mgr._visa.write(
                    resource_name, scpi, timeout_ms=timeout_ms,
                    read_termination=conn.read_termination,
                    write_termination=conn.write_termination,
                )
                session.record_command(command_name)
                return {
                    "success": True,
                    "data": {"command_name": command_name, "scpi_sent": scpi},
                    "safety_violations_overridden": list(violations) if violations else [],
                }
        except VisaError as e:
            return {"success": False, "error": type(e).__name__, "message": str(e), "resource_name": resource_name}


def _cast_response(raw: str, return_type: str) -> object:
    raw = raw.strip()
    try:
        if return_type == "integer":
            return int(float(raw))
        elif return_type == "float":
            return float(raw)
        elif return_type == "boolean":
            return raw not in ("0", "OFF", "false", "FALSE")
    except (ValueError, TypeError):
        pass
    return raw
