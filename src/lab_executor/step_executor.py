"""
個別 Step の実行ロジック (v0.5.0.1 で recipe_executor.py から切り出し)

v0.7.0: write 系 command に対し verify (read-back) を組み込む。
"""
from __future__ import annotations
import asyncio
import logging
from typing import Any, TYPE_CHECKING

from . import safety as sf
from .experiment_ir import CommandStep, WaitStep
from .models.instrument_def import VerifyConfig
from .utils.param_validator import validate_and_build_scpi, ParameterValidationError


# v2.0: backend layer は visa-mcp / shim 経由。型ヒント目的
# は TYPE_CHECKING、VisaError は ImportError fallback。
if TYPE_CHECKING:
    from lab_visa_mcp.session_manager import InstrumentSession
    from lab_visa_mcp.visa_manager import VisaManager

try:
    from lab_visa_mcp.visa_manager import VisaError
except ImportError:
    class VisaError(Exception):  # type: ignore[no-redef]
        """visa-mcp 不在時の VisaError 代替"""
        pass

logger = logging.getLogger(__name__)


async def execute_wait_step(step: WaitStep) -> dict:
    """
    単純な秒待機ステップ実行。

    この関数は cancel/timeout 検出機能を持たない (素朴な asyncio.sleep)。
    Job 経由で cancel/timeout に応答する必要がある場合は
    lab_executor.job.manager._JobRuntime と組み合わせた専用パスを使う。
    """
    await asyncio.sleep(step.seconds)
    return {
        "step_type": "wait",
        "seconds": step.seconds,
        "success": True,
    }


async def execute_command_step(
    visa: VisaManager,
    session: InstrumentSession,
    step: CommandStep,
    override_safety: bool,
    override_reason: str,
) -> dict:
    """機器コマンドを 1 回実行。安全制約 + パラメータ検証 + SCPI 送信。"""
    cmd_def = session.definition.commands.get(step.command)
    if cmd_def is None:
        return {
            "command": step.command,
            "success": False,
            "error": "CommandNotFound",
            "message": f"コマンド '{step.command}' が定義されていません",
        }

    resolved_args = step.args  # recipe_to_plan で解決済み
    mode = sf.get_safety_mode()

    # 安全制約検証
    violations = sf.validate(
        session.definition, step.command, resolved_args,
        session_history=session.command_history,
    )
    action, msg = sf.decide_action(violations, mode, override_safety, override_reason or None)

    if action in ("block_advisory", "block_strict"):
        sf.write_audit(
            session.resource_name, step.command, resolved_args, violations,
            action=action, mode=mode,
            override_safety=override_safety, override_reason=override_reason or None,
        )
        return {
            "command": step.command,
            "success": False,
            "blocked_by_safety": True,
            "violations": list(violations),
            "action": action,
            "message": msg,
        }

    if violations:
        sf.write_audit(
            session.resource_name, step.command, resolved_args, violations,
            action="proceed_with_override" if override_safety else "proceed_permissive",
            mode=mode,
            override_safety=override_safety, override_reason=override_reason or None,
        )

    # パラメータ検証 + SCPI 組み立て
    try:
        scpi = validate_and_build_scpi(cmd_def, resolved_args)
    except ParameterValidationError as e:
        return {
            "command": step.command,
            "success": False,
            "error": "ParameterValidationError",
            "message": str(e),
        }

    conn = session.definition.connection
    timeout_ms = cmd_def.timeout_ms or conn.default_timeout_ms

    try:
        if cmd_def.type == "query":
            raw = await visa.query(
                session.resource_name, scpi, timeout_ms=timeout_ms,
                read_termination=conn.read_termination,
                write_termination=conn.write_termination,
            )
            session.record_command(step.command)
            result: dict[str, Any] = {
                "command": step.command,
                "args": resolved_args,
                "scpi_sent": scpi,
                "raw_response": raw,
                "success": True,
            }
            # v2.14.0: returns.format が定義されていれば自動で
            # response_formats を引いて parse し、`parsed` を同梱する。
            # raw は常に保持 (parser 失敗時も `raw_response` は残る)。
            try:
                if cmd_def.returns and cmd_def.returns.format:
                    rf = session.definition.response_formats.get(
                        cmd_def.returns.format)
                    if rf is not None:
                        from .response_parser import parse_response as _pr
                        result["parsed"] = _pr(raw, rf)
            except Exception as _e:
                logger.debug("auto-parse failed for %s: %s",
                             step.command, _e)
            return result
        else:
            await visa.write(
                session.resource_name, scpi, timeout_ms=timeout_ms,
                read_termination=conn.read_termination,
                write_termination=conn.write_termination,
            )
            session.record_command(step.command)
            # v0.7.0: verify (write 後 read-back)
            verify_info: dict[str, Any] | None = None
            verified_ok = True
            if cmd_def.verify is not None:
                verify_info = await _do_verify(
                    visa, session, step, cmd_def.verify, resolved_args,
                )
                verified_ok = verify_info.get("verified", False)
            base = {
                "command": step.command,
                "args": resolved_args,
                "scpi_sent": scpi,
                "success": True,
            }
            if verify_info is not None:
                base["verified"] = verified_ok
                base["verify"] = verify_info
                if not verified_ok and mode == "strict":
                    # strict mode: verify 失敗を step failed として扱う
                    base["success"] = False
                    base["error"] = "VerifyMismatch"
                    base["message"] = (
                        f"verify 失敗 (strict): expected={verify_info.get('expected')}, "
                        f"actual={verify_info.get('actual')}, "
                        f"tolerance={verify_info.get('tolerance')}"
                    )
            return base
    except VisaError as e:
        return {
            "command": step.command,
            "success": False,
            "error": type(e).__name__,
            "message": str(e),
        }


# ============================================================
# v0.7.0: verify (read-back)
# ============================================================


def _extract_numeric_for_verify(
    raw: str, parsed: dict | None, value_path: str,
) -> float | None:
    """verify 用に read-back response から数値を抽出。

    polling_executor.extract_value と同じロジックを採用 (簡易版)。
    """
    if value_path and parsed and value_path in parsed:
        try:
            return float(parsed[value_path])
        except (TypeError, ValueError):
            pass
    if parsed and "value" in parsed:
        try:
            return float(parsed["value"])
        except (TypeError, ValueError):
            pass
    if parsed:
        numeric = []
        for k, v in parsed.items():
            try:
                float(v)
                numeric.append(k)
            except (TypeError, ValueError):
                pass
        if len(numeric) == 1:
            try:
                return float(parsed[numeric[0]])
            except (TypeError, ValueError):
                pass
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return None


async def _do_verify(
    visa: VisaManager,
    session: InstrumentSession,
    write_step: CommandStep,
    verify_cfg: VerifyConfig,
    resolved_args: dict[str, Any],
) -> dict[str, Any]:
    """write 直後の read-back 比較を実施。

    返り値:
      {
        "verified": bool,
        "readback_command": str,
        "expected": float | None,
        "actual": float | None,
        "tolerance": float,
        "attempts": int,
        "status": "ok" | "mismatch" | "readback_failed",
        "message"?: str,
      }
    """
    from .response_parser import parse_response

    # v0.7.0.1: 期待値推定を厳密化
    # 数値 args が複数ある場合は arg_key を必須にする (set_limit(voltage, current)
    # のような曖昧パターンを reject)。
    numeric_args: list[tuple[str, float]] = []
    for k, v in resolved_args.items():
        try:
            numeric_args.append((k, float(v)))
        except (TypeError, ValueError):
            continue

    expected: float | None = None
    if verify_cfg.arg_key:
        # 明示指定があればそれを採用
        if verify_cfg.arg_key in resolved_args:
            try:
                expected = float(resolved_args[verify_cfg.arg_key])
            except (TypeError, ValueError):
                return {
                    "verified": False,
                    "readback_command": verify_cfg.readback_command,
                    "expected": None, "actual": None,
                    "tolerance": verify_cfg.tolerance,
                    "attempts": 0, "status": "readback_failed",
                    "message": (
                        f"verify.arg_key='{verify_cfg.arg_key}' の値が "
                        f"数値ではありません: {resolved_args[verify_cfg.arg_key]!r}"
                    ),
                }
        else:
            return {
                "verified": False,
                "readback_command": verify_cfg.readback_command,
                "expected": None, "actual": None,
                "tolerance": verify_cfg.tolerance,
                "attempts": 0, "status": "readback_failed",
                "message": (
                    f"verify.arg_key='{verify_cfg.arg_key}' が "
                    f"resolved_args={list(resolved_args)} に存在しません"
                ),
            }
    else:
        # arg_key 未指定 → 数値 args の数に応じて分岐
        if len(numeric_args) == 1:
            expected = numeric_args[0][1]
        elif len(numeric_args) >= 2:
            return {
                "verified": False,
                "readback_command": verify_cfg.readback_command,
                "expected": None, "actual": None,
                "tolerance": verify_cfg.tolerance,
                "attempts": 0, "status": "readback_failed",
                "message": (
                    f"verify: write '{write_step.command}' に数値 parameter が "
                    f"複数あります ({[k for k, _ in numeric_args]})。"
                    f"verify.arg_key を明示してください"
                ),
            }
        # len == 0 の場合は expected=None のまま (info のみ verify になる)

    rb_cmd_def = session.definition.commands.get(verify_cfg.readback_command)
    if rb_cmd_def is None:
        return {
            "verified": False, "readback_command": verify_cfg.readback_command,
            "expected": expected, "actual": None, "tolerance": verify_cfg.tolerance,
            "attempts": 0, "status": "readback_failed",
            "message": (
                f"verify.readback_command '{verify_cfg.readback_command}' が "
                f"機器定義に存在しません"
            ),
        }
    if rb_cmd_def.type != "query":
        return {
            "verified": False, "readback_command": verify_cfg.readback_command,
            "expected": expected, "actual": None, "tolerance": verify_cfg.tolerance,
            "attempts": 0, "status": "readback_failed",
            "message": (
                f"readback_command '{verify_cfg.readback_command}' は query 型である必要があります"
            ),
        }

    max_retry = max(0, int(verify_cfg.retry))
    delay_s = max(0.0, float(verify_cfg.delay_s))
    conn = session.definition.connection
    rb_timeout_ms = rb_cmd_def.timeout_ms or conn.default_timeout_ms

    actual: float | None = None
    last_raw = ""
    attempts = 0
    while attempts <= max_retry:
        attempts += 1
        try:
            rb_scpi = rb_cmd_def.scpi
            last_raw = await visa.query(
                session.resource_name, rb_scpi, timeout_ms=rb_timeout_ms,
                read_termination=conn.read_termination,
                write_termination=conn.write_termination,
            )
        except VisaError as e:
            if attempts > max_retry:
                return {
                    "verified": False,
                    "readback_command": verify_cfg.readback_command,
                    "expected": expected, "actual": None,
                    "tolerance": verify_cfg.tolerance,
                    "attempts": attempts, "status": "readback_failed",
                    "message": f"VisaError on read-back: {e}",
                }
            if delay_s > 0:
                await asyncio.sleep(delay_s)
            continue

        parsed: dict | None = None
        if rb_cmd_def.returns and rb_cmd_def.returns.format:
            fmt = session.definition.response_formats.get(rb_cmd_def.returns.format)
            if fmt is not None:
                p = parse_response(last_raw, fmt)
                if p.get("matched"):
                    parsed = p.get("fields") or {}

        actual = _extract_numeric_for_verify(last_raw, parsed, verify_cfg.value_path)
        if actual is None:
            if attempts > max_retry:
                return {
                    "verified": False,
                    "readback_command": verify_cfg.readback_command,
                    "expected": expected, "actual": None,
                    "tolerance": verify_cfg.tolerance,
                    "attempts": attempts, "status": "readback_failed",
                    "message": f"read-back から数値を抽出できません (raw={last_raw!r})",
                }
            if delay_s > 0:
                await asyncio.sleep(delay_s)
            continue

        # 比較
        if expected is None:
            # 比較不可 (write が数値 args を持たない等) → "ok" 扱い (info のみ)
            return {
                "verified": True,
                "readback_command": verify_cfg.readback_command,
                "expected": None, "actual": actual,
                "tolerance": verify_cfg.tolerance,
                "attempts": attempts, "status": "ok",
                "message": "expected が抽出できないため比較スキップ",
            }
        if abs(actual - expected) <= verify_cfg.tolerance:
            return {
                "verified": True,
                "readback_command": verify_cfg.readback_command,
                "expected": expected, "actual": actual,
                "tolerance": verify_cfg.tolerance,
                "attempts": attempts, "status": "ok",
            }
        # mismatch → retry
        if attempts > max_retry:
            return {
                "verified": False,
                "readback_command": verify_cfg.readback_command,
                "expected": expected, "actual": actual,
                "tolerance": verify_cfg.tolerance,
                "attempts": attempts, "status": "mismatch",
            }
        if delay_s > 0:
            await asyncio.sleep(delay_s)

    # ここに来ることは無いが安全側 fallback
    return {
        "verified": False,
        "readback_command": verify_cfg.readback_command,
        "expected": expected, "actual": actual,
        "tolerance": verify_cfg.tolerance,
        "attempts": attempts, "status": "mismatch",
    }
