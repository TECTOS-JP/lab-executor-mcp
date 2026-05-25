"""
Polling wait executor (v0.5.1)

wait_for_condition / wait_for_stable / wait_until を実行する。

設計原則 (visa_mcp_v0.5.1の実装方針.md 反映):
- polling 間 sleep は POLL_SLEEP_SLICE_S 単位スライス → cancel/timeout に即応
- 開始直後 (t=0) に 1 回目の polling を実行 (エージェントがすぐ進捗を知れる)
- 連続失敗が max_consecutive_errors を超えたら step failed
- 1 polling 失敗は retry_on_error 回まで即時 retry
- 値抽出順序: value_path → parsed "value" → 単一数値フィールド → raw float → parse error
- wait_for_stable: max(window samples) - min(window samples) <= tolerance で stable 判定
- progress は callback (on_progress) で公開
"""
from __future__ import annotations
import asyncio
import logging
import math
import time
from datetime import datetime, timezone
from typing import Any, Callable, Awaitable, TYPE_CHECKING

from .experiment_ir import (
    WaitUntilStep, WaitForConditionStep, WaitForStableStep,
)
from .response_parser import parse_response
from .utils.condition import safe_eval_condition, ConditionError


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


# polling sleep を slice する単位 (s)
POLL_SLEEP_SLICE_S = 0.2


ProgressCallback = Callable[[dict], None]
"""progress dict を runtime へ書き戻すコールバック (同期呼び出し、軽量想定)"""

CancelCheckCallback = Callable[[], str | None]
"""キャンセル/タイムアウト要求を確認するコールバック。
返り値が None でなければ理由 (cancel|timeout) を返す。"""


# ============================================================
# 値抽出
# ============================================================


def extract_value(
    raw: str,
    parsed_fields: dict | None,
    value_path: str | None,
) -> float | None:
    """
    polling wait 用に測定値を float として取り出す。

    抽出順序:
      1. value_path が指定されていれば parsed_fields[value_path] を採用
      2. parsed_fields に "value" があれば採用
      3. parsed_fields に数値フィールドが 1 つだけならそれを採用
      4. raw が単独数値なら float 化
      5. 上記すべて失敗なら None
    """
    # 1. value_path
    if value_path and parsed_fields and value_path in parsed_fields:
        try:
            return float(parsed_fields[value_path])
        except (TypeError, ValueError):
            pass

    # 2. "value" キー
    if parsed_fields and "value" in parsed_fields:
        try:
            return float(parsed_fields["value"])
        except (TypeError, ValueError):
            pass

    # 3. 数値フィールドが 1 個だけ
    if parsed_fields:
        numeric_keys: list[str] = []
        for k, v in parsed_fields.items():
            try:
                float(v)
                numeric_keys.append(k)
            except (TypeError, ValueError):
                pass
        if len(numeric_keys) == 1:
            try:
                return float(parsed_fields[numeric_keys[0]])
            except (TypeError, ValueError):
                pass

    # 4. raw 単独数値
    if raw is not None:
        try:
            return float(str(raw).strip())
        except (TypeError, ValueError):
            pass

    return None


# ============================================================
# polling 1 回 (query + parse + value 抽出)
# ============================================================


async def _do_one_poll(
    visa: VisaManager,
    session: InstrumentSession,
    command_name: str,
    args: dict[str, Any],
    command_timeout_s: float | None,
    value_path: str | None,
) -> tuple[float | None, str, dict | None, str | None]:
    """
    1 polling を実行。
    返り値: (value, raw, parsed_fields, error_kind)
      - 成功時: value=数値, error_kind=None
      - 失敗時: value=None, error_kind in {"command_not_found","visa","parse"}
    """
    from .utils.param_validator import validate_and_build_scpi, ParameterValidationError

    cmd_def = session.definition.commands.get(command_name) if session.definition else None
    if cmd_def is None:
        return None, "", None, "command_not_found"

    if cmd_def.type != "query":
        return None, "", None, "command_not_query"

    try:
        scpi = validate_and_build_scpi(cmd_def, args)
    except ParameterValidationError as e:
        logger.warning("polling param validation: %s", e)
        return None, "", None, "validation"

    conn = session.definition.connection
    timeout_ms = (
        int(command_timeout_s * 1000) if command_timeout_s is not None
        else (cmd_def.timeout_ms or conn.default_timeout_ms)
    )
    try:
        raw = await visa.query(
            session.resource_name, scpi, timeout_ms=timeout_ms,
            read_termination=conn.read_termination,
            write_termination=conn.write_termination,
        )
    except VisaError as e:
        logger.debug("polling visa error: %s", e)
        return None, "", None, "visa"
    except asyncio.TimeoutError:
        return None, "", None, "visa"

    # response_format パース (任意)
    parsed: dict | None = None
    if cmd_def.returns and cmd_def.returns.format:
        fmt = session.definition.response_formats.get(cmd_def.returns.format)
        if fmt is not None:
            p = parse_response(raw, fmt)
            if p.get("matched"):
                parsed = p.get("fields") or {}

    value = extract_value(raw, parsed, value_path)
    if value is None:
        return None, raw, parsed, "parse"
    return value, raw, parsed, None


# ============================================================
# polling 共通ループ
# ============================================================


async def _sleep_sliced_until_next_poll(
    interval_s: float,
    cancel_check: CancelCheckCallback,
    on_tick: Callable[[float], None] | None = None,
) -> str | None:
    """
    interval_s 秒を POLL_SLEEP_SLICE_S 単位でスライスして sleep する。
    途中で cancel_check が None 以外を返したらその理由文字列を返して中断。
    on_tick: 残り時間を runtime に書き戻すための callback (任意)
    """
    remaining = interval_s
    while remaining > 0:
        reason = cancel_check()
        if reason:
            return reason
        chunk = min(remaining, POLL_SLEEP_SLICE_S)
        if on_tick is not None:
            on_tick(remaining)
        await asyncio.sleep(chunk)
        remaining -= chunk
    return None


# ============================================================
# wait_until
# ============================================================


async def execute_wait_until(
    step: WaitUntilStep,
    cancel_check: CancelCheckCallback,
    on_progress: ProgressCallback | None = None,
) -> dict:
    """絶対 / 相対 deadline まで待機 (slice 方式)"""
    start = time.monotonic()
    if step.seconds_from_now is not None:
        deadline_mono = start + float(step.seconds_from_now)
    else:
        # timestamp ISO8601
        try:
            target_dt = datetime.fromisoformat(step.timestamp)  # type: ignore[arg-type]
        except Exception as e:
            return {
                "step_type": "wait_until", "success": False,
                "error": "InvalidTimestamp", "message": str(e),
            }
        # v0.5.1.1: naive timestamp は危険 (UTC として扱われると実験現場で時差ミス)
        # → 拒否し、timezone 付きで指定するよう誘導
        if target_dt.tzinfo is None:
            return {
                "step_type": "wait_until", "success": False,
                "error": "TimezoneRequired",
                "message": (
                    f"wait_until.timestamp は timezone 付き ISO8601 で指定してください。"
                    f"例: '2026-05-22T15:00:00+09:00' (日本時間) "
                    f"または '2026-05-22T06:00:00+00:00' / '...Z' (UTC)。"
                    f"naive timestamp ({step.timestamp!r}) は曖昧なため拒否します。"
                ),
            }
        now = datetime.now(timezone.utc)
        delta = (target_dt - now).total_seconds()
        if delta < 0:
            return {
                "step_type": "wait_until", "success": True,
                "waited_s": 0.0, "note": "deadline already passed",
            }
        deadline_mono = start + delta

    while True:
        now = time.monotonic()
        if now >= deadline_mono:
            break
        reason = cancel_check()
        if reason:
            return {
                "step_type": "wait_until",
                "success": False,
                ("interrupted_by_" + reason): True,
                "error": reason,
                "waited_s": now - start,
            }
        remaining = deadline_mono - now
        if on_progress is not None:
            on_progress({
                "step_type": "wait_until",
                "elapsed_s": now - start,
                "remaining_s": remaining,
            })
        chunk = min(remaining, POLL_SLEEP_SLICE_S)
        await asyncio.sleep(chunk)

    return {
        "step_type": "wait_until",
        "success": True,
        "waited_s": time.monotonic() - start,
    }


# ============================================================
# wait_for_condition
# ============================================================


async def execute_wait_for_condition(
    visa: VisaManager,
    session_resolver: Callable[[str], InstrumentSession | None],
    step: WaitForConditionStep,
    cancel_check: CancelCheckCallback,
    on_progress: ProgressCallback | None = None,
) -> dict:
    """
    条件式が True になるまで polling。
    step.timeout_s を超えたら timeout エラー。
    """
    target_session = session_resolver(step.instrument)
    if target_session is None or target_session.definition is None:
        return {
            "step_type": "wait_for_condition", "success": False,
            "error": "InstrumentNotFound",
            "message": f"instrument '{step.instrument}' is not identified",
        }

    # v0.5.1.1: polling_safe 警告 (wait_for_stable と同様に condition 側でも)
    cmd_def = target_session.definition.commands.get(step.command)
    polling_safe_warning: str | None = None
    if cmd_def is not None and not cmd_def.polling_safe:
        polling_safe_warning = (
            f"command '{step.command}' has polling_safe=False; "
            f"確認: 副作用のある READ?/MEAS? を polling する可能性"
        )

    start = time.monotonic()
    deadline = start + step.timeout_s
    consecutive_errors = 0
    last_value: float | None = None

    # v0.5.1.1: sample_count を poll_count / valid_sample_count / consecutive_errors に分離
    poll_count = 0           # poll 試行数 (エラー含む)
    valid_sample_count = 0   # 有効な数値を得た成功 poll 数

    while True:
        attempt = 0
        value: float | None = None
        error_kind: str | None = None
        while attempt <= step.retry_on_error:
            reason = cancel_check()
            if reason:
                return {
                    "step_type": "wait_for_condition",
                    "success": False,
                    ("interrupted_by_" + reason): True,
                    "error": reason,
                    "poll_count": poll_count,
                    "valid_sample_count": valid_sample_count,
                    "consecutive_errors": consecutive_errors,
                    "elapsed_s": time.monotonic() - start,
                    "last_value": last_value,
                    "polling_safe_warning": polling_safe_warning,
                }
            value, _raw, _parsed, error_kind = await _do_one_poll(
                visa, target_session, step.command, step.args,
                step.command_timeout_s, step.value_path,
            )
            if error_kind is None:
                break
            attempt += 1

        poll_count += 1
        if error_kind is not None:
            consecutive_errors += 1
            if consecutive_errors >= step.max_consecutive_errors:
                return {
                    "step_type": "wait_for_condition",
                    "success": False,
                    "error": "PollingErrorExceeded",
                    "message": (
                        f"連続 polling 失敗が {consecutive_errors} 回 "
                        f"(>= max_consecutive_errors={step.max_consecutive_errors})"
                    ),
                    "last_error_kind": error_kind,
                    "poll_count": poll_count,
                    "valid_sample_count": valid_sample_count,
                    "consecutive_errors": consecutive_errors,
                    "elapsed_s": time.monotonic() - start,
                    "polling_safe_warning": polling_safe_warning,
                }
        else:
            consecutive_errors = 0
            last_value = value
            valid_sample_count += 1

            try:
                ok = safe_eval_condition(step.condition_expr, {"value": value})
            except ConditionError as e:
                return {
                    "step_type": "wait_for_condition",
                    "success": False,
                    "error": "ConditionError",
                    "message": str(e),
                    "elapsed_s": time.monotonic() - start,
                    "polling_safe_warning": polling_safe_warning,
                }
            if on_progress is not None:
                on_progress({
                    "step_type": "wait_for_condition",
                    "command": step.command,
                    "instrument": step.instrument,
                    "elapsed_s": time.monotonic() - start,
                    "timeout_remaining_s": max(0.0, deadline - time.monotonic()),
                    "poll_count": poll_count,
                    "valid_sample_count": valid_sample_count,
                    "consecutive_errors": consecutive_errors,
                    "last_value": last_value,
                    "condition_met": ok,
                    "next_poll_in_s": step.interval_s,
                    "polling_safe_warning": polling_safe_warning,
                })
            if ok:
                return {
                    "step_type": "wait_for_condition",
                    "success": True,
                    "poll_count": poll_count,
                    "valid_sample_count": valid_sample_count,
                    "elapsed_s": time.monotonic() - start,
                    "last_value": last_value,
                    "condition_expr": step.condition_expr,
                    "polling_safe_warning": polling_safe_warning,
                }

        # timeout
        now = time.monotonic()
        if now >= deadline:
            return {
                "step_type": "wait_for_condition",
                "success": False,
                "error": "WaitConditionTimeout",
                "message": (
                    f"timeout_s={step.timeout_s} を超過、条件未達成 "
                    f"(last_value={last_value})"
                ),
                "poll_count": poll_count,
                "valid_sample_count": valid_sample_count,
                "elapsed_s": now - start,
                "last_value": last_value,
                "polling_safe_warning": polling_safe_warning,
            }

        def _tick(remaining: float) -> None:
            if on_progress is not None:
                on_progress({
                    "step_type": "wait_for_condition",
                    "command": step.command,
                    "instrument": step.instrument,
                    "elapsed_s": time.monotonic() - start,
                    "timeout_remaining_s": max(0.0, deadline - time.monotonic()),
                    "poll_count": poll_count,
                    "valid_sample_count": valid_sample_count,
                    "consecutive_errors": consecutive_errors,
                    "last_value": last_value,
                    "next_poll_in_s": remaining,
                    "polling_safe_warning": polling_safe_warning,
                })

        reason = await _sleep_sliced_until_next_poll(
            min(step.interval_s, max(0.0, deadline - time.monotonic())),
            cancel_check, on_tick=_tick,
        )
        if reason:
            return {
                "step_type": "wait_for_condition",
                "success": False,
                ("interrupted_by_" + reason): True,
                "error": reason,
                "poll_count": poll_count,
                "valid_sample_count": valid_sample_count,
                "elapsed_s": time.monotonic() - start,
                "last_value": last_value,
                "polling_safe_warning": polling_safe_warning,
            }


# ============================================================
# wait_for_stable
# ============================================================


def _is_stable(window_samples: list[tuple[float, float]], tolerance: float,
               min_samples: int, window_s: float) -> tuple[bool, float | None]:
    """
    window_samples: [(t, value), ...] 時系列順 (t は polling 開始からの経過秒)
    安定判定:
      1. window_s に渡って観測している (= 最古サンプルが latest_t - window_s 以下に達している)
      2. window 内サンプル数 >= min_samples
      3. max - min <= tolerance
    返り値: (is_stable, current_delta_or_None)

    重要: 単に「最新 window_s 内のサンプル数」だけで判定すると、
    開始から window_s に満たない時点でも min_samples を満たせば stable と返してしまう。
    実際には window_s に渡って観測した上で安定していることを保証したい。
    """
    if not window_samples:
        return False, None
    latest_t = window_samples[-1][0]
    earliest_t = window_samples[0][0]
    # まだ window_s 分の観測時間が経過していないなら、いかなる値分布でも stable とは見なさない
    if (latest_t - earliest_t) < window_s:
        # 参考値として現在の window 内 delta を返す
        in_window = [v for (t, v) in window_samples]
        delta = max(in_window) - min(in_window) if len(in_window) >= 2 else None
        return False, delta

    in_window = [v for (t, v) in window_samples if (latest_t - t) <= window_s]
    if len(in_window) < min_samples:
        return False, (max(in_window) - min(in_window) if len(in_window) >= 2 else None)
    delta = max(in_window) - min(in_window)
    return (delta <= tolerance), delta


async def execute_wait_for_stable(
    visa: VisaManager,
    session_resolver: Callable[[str], InstrumentSession | None],
    step: WaitForStableStep,
    cancel_check: CancelCheckCallback,
    on_progress: ProgressCallback | None = None,
) -> dict:
    """
    window_s 期間内の (max - min) が tolerance 以下になるまで polling。
    polling_safe=False の query を使っている場合は警告を含めて返す。
    """
    target_session = session_resolver(step.instrument)
    if target_session is None or target_session.definition is None:
        return {
            "step_type": "wait_for_stable", "success": False,
            "error": "InstrumentNotFound",
            "message": f"instrument '{step.instrument}' is not identified",
        }

    cmd_def = target_session.definition.commands.get(step.command)
    polling_safe_warning: str | None = None
    if cmd_def is not None and not cmd_def.polling_safe:
        polling_safe_warning = (
            f"command '{step.command}' has polling_safe=False; "
            f"確認: 副作用のある READ?/MEAS? を polling する可能性"
        )

    start = time.monotonic()
    deadline = start + step.timeout_s
    samples: list[tuple[float, float]] = []
    consecutive_errors = 0
    # v0.5.1.1: poll_count (試行数) と valid_sample_count (有効サンプル数) を分離
    poll_count = 0
    valid_sample_count = 0
    last_value: float | None = None
    last_delta: float | None = None

    while True:
        attempt = 0
        value: float | None = None
        error_kind: str | None = None
        while attempt <= step.retry_on_error:
            reason = cancel_check()
            if reason:
                return {
                    "step_type": "wait_for_stable",
                    "success": False,
                    ("interrupted_by_" + reason): True,
                    "error": reason,
                    "poll_count": poll_count,
                    "valid_sample_count": valid_sample_count,
                    "consecutive_errors": consecutive_errors,
                    "elapsed_s": time.monotonic() - start,
                    "last_value": last_value,
                    "last_delta": last_delta,
                    "polling_safe_warning": polling_safe_warning,
                }
            value, _raw, _parsed, error_kind = await _do_one_poll(
                visa, target_session, step.command, step.args,
                step.command_timeout_s, step.value_path,
            )
            if error_kind is None:
                break
            attempt += 1

        poll_count += 1
        if error_kind is not None:
            consecutive_errors += 1
            if consecutive_errors >= step.max_consecutive_errors:
                return {
                    "step_type": "wait_for_stable",
                    "success": False,
                    "error": "PollingErrorExceeded",
                    "message": (
                        f"連続 polling 失敗が {consecutive_errors} 回 "
                        f"(>= max_consecutive_errors={step.max_consecutive_errors})"
                    ),
                    "last_error_kind": error_kind,
                    "poll_count": poll_count,
                    "valid_sample_count": valid_sample_count,
                    "consecutive_errors": consecutive_errors,
                    "elapsed_s": time.monotonic() - start,
                    "polling_safe_warning": polling_safe_warning,
                }
        else:
            consecutive_errors = 0
            last_value = value
            valid_sample_count += 1
            t = time.monotonic() - start
            samples.append((t, value))  # type: ignore[arg-type]

            # メモリ膨張防止: window_s + 1 サンプル分より古い記録を破棄。
            # _is_stable は最新 window_s 内のみ参照するため、それより古いものは不要。
            # ただし「window_s 経過観測」判定のため、最古 1 個は残す必要あり。
            cutoff = t - step.window_s
            # 最古を 1 個だけ残して残りは window 内に絞る
            i = 0
            while i < len(samples) - 1 and samples[i + 1][0] < cutoff:
                i += 1
            if i > 0:
                samples[:i] = []

            stable, delta = _is_stable(
                samples, step.tolerance, step.min_samples, step.window_s,
            )
            last_delta = delta
            if on_progress is not None:
                on_progress({
                    "step_type": "wait_for_stable",
                    "command": step.command,
                    "instrument": step.instrument,
                    "elapsed_s": t,
                    "timeout_remaining_s": max(0.0, deadline - time.monotonic()),
                    "poll_count": poll_count,
                    "valid_sample_count": valid_sample_count,
                    "consecutive_errors": consecutive_errors,
                    "last_value": last_value,
                    "current_delta": delta,
                    "tolerance": step.tolerance,
                    "window_s": step.window_s,
                    "stable": stable,
                    "next_poll_in_s": step.interval_s,
                    "polling_safe_warning": polling_safe_warning,
                })
            if stable:
                return {
                    "step_type": "wait_for_stable",
                    "success": True,
                    "poll_count": poll_count,
                    "valid_sample_count": valid_sample_count,
                    "elapsed_s": t,
                    "last_value": last_value,
                    "final_delta": delta,
                    "tolerance": step.tolerance,
                    "window_s": step.window_s,
                    "polling_safe_warning": polling_safe_warning,
                }

        now = time.monotonic()
        if now >= deadline:
            return {
                "step_type": "wait_for_stable",
                "success": False,
                "error": "WaitStableTimeout",
                "message": (
                    f"timeout_s={step.timeout_s} を超過、安定条件未達成 "
                    f"(last_value={last_value}, last_delta={last_delta})"
                ),
                "poll_count": poll_count,
                "valid_sample_count": valid_sample_count,
                "elapsed_s": now - start,
                "last_value": last_value,
                "last_delta": last_delta,
                "polling_safe_warning": polling_safe_warning,
            }

        def _tick(remaining: float) -> None:
            if on_progress is not None:
                on_progress({
                    "step_type": "wait_for_stable",
                    "command": step.command,
                    "instrument": step.instrument,
                    "elapsed_s": time.monotonic() - start,
                    "timeout_remaining_s": max(0.0, deadline - time.monotonic()),
                    "poll_count": poll_count,
                    "valid_sample_count": valid_sample_count,
                    "consecutive_errors": consecutive_errors,
                    "last_value": last_value,
                    "current_delta": last_delta,
                    "next_poll_in_s": remaining,
                    "polling_safe_warning": polling_safe_warning,
                })

        reason = await _sleep_sliced_until_next_poll(
            min(step.interval_s, max(0.0, deadline - time.monotonic())),
            cancel_check, on_tick=_tick,
        )
        if reason:
            return {
                "step_type": "wait_for_stable",
                "success": False,
                ("interrupted_by_" + reason): True,
                "error": reason,
                "poll_count": poll_count,
                "valid_sample_count": valid_sample_count,
                "elapsed_s": time.monotonic() - start,
                "last_value": last_value,
                "last_delta": last_delta,
                "polling_safe_warning": polling_safe_warning,
            }
