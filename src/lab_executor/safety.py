"""
安全制約の検証層 (v0.2.0)

設計方針:
- YAML の `safety` セクションに宣言された制約をチェック
- モード: strict / advisory (デフォルト) / permissive
- LLM が override_safety=True + override_reason="..." で警告を無視可能 (strict 除く)
- 全 override は監査ログに記録
"""
from __future__ import annotations
import json
import logging
import os
import re
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from .models.instrument_def import InstrumentDefinition, PreconditionCheck

logger = logging.getLogger(__name__)


SafetyMode = Literal["strict", "advisory", "permissive"]


_MODE_WARNING_EMITTED = False


def get_safety_mode() -> SafetyMode:
    """
    環境変数 VISA_MCP_SAFETY_MODE からモードを取得 (デフォルト: strict, v0.4.0 から)。

    v0.3.0 までは advisory がデフォルトだったが、LLM が操作主体になる
    MCP では保守的な初期値が望ましいため、v0.4.0 から strict に変更した。
    """
    global _MODE_WARNING_EMITTED
    raw_env = os.environ.get("VISA_MCP_SAFETY_MODE")
    if raw_env is None:
        if not _MODE_WARNING_EMITTED:
            logger.warning(
                "VISA_MCP_SAFETY_MODE が未設定です。デフォルトの 'strict' を使用します。"
                " 研究開発で override を使いたい場合は VISA_MCP_SAFETY_MODE=advisory を指定してください。"
            )
            _MODE_WARNING_EMITTED = True
        return "strict"
    raw = raw_env.strip().lower()
    if raw in ("strict", "advisory", "permissive"):
        return raw  # type: ignore[return-value]
    logger.warning(
        "不明な VISA_MCP_SAFETY_MODE='%s'。strict にフォールバックします。", raw
    )
    return "strict"


def get_audit_log_path() -> Path:
    """監査ログの保存先 (環境変数 VISA_MCP_AUDIT_LOG または既定パス)"""
    p = os.environ.get(
        "VISA_MCP_AUDIT_LOG",
        str(Path.home() / ".visa-mcp" / "audit.log"),
    )
    return Path(p)


class SafetyViolation(dict):
    """1 件の違反情報 (dict 派生でそのまま JSON 化可能)"""

    def __init__(
        self,
        violation_type: str,
        details: str,
        severity: Literal["low", "medium", "high"] = "medium",
        recommendation: str = "",
    ):
        super().__init__(
            violation_type=violation_type,
            details=details,
            severity=severity,
            recommendation=recommendation,
        )


def _check_range_violations(
    definition: InstrumentDefinition,
    command_name: str,
    parameters: dict[str, Any],
) -> list[SafetyViolation]:
    """ratings に基づく値制約チェック"""
    violations: list[SafetyViolation] = []
    cmd = definition.commands.get(command_name)
    if cmd is None:
        return violations

    # コマンドのパラメータ名 → 値 マップ
    for param_def in cmd.parameters:
        if param_def.name not in parameters:
            continue
        raw = parameters[param_def.name]
        # 数値判定
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue

        # rating キーをパラメータ名で照合 (例: voltage / current)
        # 命名規則: パラメータ名と ratings キーが一致するか部分一致
        for rating_key, rating in definition.safety.ratings.items():
            if rating_key.lower() not in param_def.name.lower():
                continue

            # 絶対最大定格チェック
            if rating.absolute_max is not None and value > rating.absolute_max:
                violations.append(
                    SafetyViolation(
                        violation_type="absolute_max_exceeded",
                        details=(
                            f"{param_def.name}={value} {rating.unit} は絶対最大定格 "
                            f"{rating.absolute_max} {rating.unit} を超えています"
                        ),
                        severity="high",
                        recommendation=(
                            f"値を {rating.absolute_max} {rating.unit} 以下に設定するか、"
                            "override_safety=True と override_reason を指定して実行してください"
                        ),
                    )
                )
            if rating.absolute_min is not None and value < rating.absolute_min:
                violations.append(
                    SafetyViolation(
                        violation_type="absolute_min_undershot",
                        details=(
                            f"{param_def.name}={value} {rating.unit} は絶対最小定格 "
                            f"{rating.absolute_min} {rating.unit} を下回ります"
                        ),
                        severity="high",
                    )
                )

            # 推奨上限チェック (低重要度)
            if (
                rating.recommended_max is not None
                and value > rating.recommended_max
                and (rating.absolute_max is None or value <= rating.absolute_max)
            ):
                violations.append(
                    SafetyViolation(
                        violation_type="recommended_max_exceeded",
                        details=(
                            f"{param_def.name}={value} {rating.unit} は推奨上限 "
                            f"{rating.recommended_max} {rating.unit} を超えています "
                            "(動作可能ですが定格寿命に影響する可能性)"
                        ),
                        severity="low",
                    )
                )

    return violations


def _check_preconditions(
    definition: InstrumentDefinition,
    command_name: str,
    parameters: dict[str, Any],
    session_history: list[str],
) -> list[SafetyViolation]:
    """前提条件 (preconditions) チェック"""
    violations: list[SafetyViolation] = []

    for pc in definition.safety.preconditions:
        if pc.command != command_name:
            continue

        # when 条件マッチ判定
        when_match = True
        for k, expected in pc.when.items():
            actual = parameters.get(k)
            if isinstance(expected, list):
                if str(actual) not in [str(e) for e in expected]:
                    when_match = False
                    break
            else:
                if str(actual) != str(expected):
                    when_match = False
                    break

        if not when_match:
            continue

        # requires チェック
        for req in pc.requires:
            for kind, target in req.items():
                if kind == "has_been_called" and target not in session_history:
                    violations.append(
                        SafetyViolation(
                            violation_type="precondition_unmet",
                            details=(
                                f"コマンド '{command_name}' 実行前に '{target}' を呼ぶ必要があります"
                            ),
                            severity=pc.severity,
                            recommendation=pc.reason,
                        )
                    )

    return violations


def validate(
    definition: InstrumentDefinition,
    command_name: str,
    parameters: dict[str, Any] | None,
    session_history: list[str] | None = None,
) -> list[SafetyViolation]:
    """
    安全制約を検証する。違反がなければ空リストを返す。
    """
    parameters = parameters or {}
    session_history = session_history or []
    violations: list[SafetyViolation] = []
    violations.extend(_check_range_violations(definition, command_name, parameters))
    violations.extend(_check_preconditions(definition, command_name, parameters, session_history))
    return violations


def decide_action(
    violations: list[SafetyViolation],
    mode: SafetyMode,
    override_safety: bool,
    override_reason: str | None,
) -> tuple[Literal["proceed", "block_advisory", "block_strict"], str | None]:
    """
    違反リスト・モード・override 引数から、実行可否を決定する。

    返り値:
      ("proceed", None) → そのまま実行
      ("block_advisory", warning_msg) → 警告して実行しない (override 可)
      ("block_strict", error_msg) → エラーで実行しない (override 不可)
    """
    if not violations:
        return ("proceed", None)

    # strict モード: 違反があれば常にブロック、override 不可
    if mode == "strict":
        return (
            "block_strict",
            "strict モードでは安全制約違反のあるコマンドは実行できません",
        )

    # permissive モード: 違反は記録のみ、常に proceed
    if mode == "permissive":
        return ("proceed", None)

    # advisory モード
    if override_safety:
        if not override_reason or not override_reason.strip():
            return (
                "block_advisory",
                "override_safety=True を指定する場合は override_reason を必ず記述してください",
            )
        return ("proceed", None)

    return ("block_advisory", "安全制約違反のため実行を保留しました。値を見直すか override してください")


def write_audit(
    resource_name: str,
    command_name: str,
    parameters: dict[str, Any] | None,
    violations: list[SafetyViolation],
    action: str,
    mode: SafetyMode,
    override_safety: bool,
    override_reason: str | None,
) -> None:
    """監査ログに 1 行 JSON で追記"""
    try:
        log_path = get_audit_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "resource": resource_name,
            "command": command_name,
            "parameters": parameters or {},
            "violations": list(violations),
            "action": action,
            "mode": mode,
            "override_safety": override_safety,
            "override_reason": override_reason,
        }
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        # 監査ログ失敗はメイン動作を止めない
        logger.warning("監査ログ書込失敗: %s", e)


def make_warning_response(violations: list[SafetyViolation], action: str, mode: SafetyMode) -> dict:
    """LLM 向けの警告レスポンスを構築"""
    token = "wrn_" + secrets.token_hex(4)
    return {
        "success": False,
        "blocked_by_safety": True,
        "safety_mode": mode,
        "action": action,
        "violations": list(violations),
        "override_token": token if action == "block_advisory" else None,
        "override_help": (
            "execute_named_command(..., override_safety=True, override_reason='...') で再実行可能"
            if action == "block_advisory"
            else "strict モードのため override 不可"
        ),
    }
