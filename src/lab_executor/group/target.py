"""
v0.6.0 TargetExecution IR

group / map executor が共通で扱う「1 つの target を実行する」単位。

query_group:
  各 instrument に対し単一 CommandStep の Plan を作って TargetExecution にラップ
map_recipe:
  各 target に対し Recipe 由来 Plan + bindings を持つ TargetExecution
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any

from lab_executor.experiment_ir import Plan


@dataclass
class TargetExecution:
    """1 つの target の実行単位"""
    target_id: str
    plan: Plan
    required_resources: list[str]  # canonical sorted
    bindings: dict[str, str] = field(default_factory=dict)   # logical role → alias
    parameters: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    # v0.6.1: stagger 順序保証用 (入力 target 順、0-indexed)。
    # CommandStep.stagger_ms が指定されている場合、target_index * stagger_ms / 1000 だけ
    # step 開始を遅延させる。
    target_index: int = 0


@dataclass
class FailurePolicy:
    """Group/Map Job の失敗時挙動

    mode:
      - "continue":                    失敗 target を記録し、他 target は継続
      - "stop_on_first_error":         最初の失敗で未開始 target を skipped に
      - "stop_if_failure_rate_exceeds":失敗率が閾値超過で未開始を skipped

    retry: 同一 target を全体再実行する最大回数 (target 単位リトライ、step 部分リトライではない)
    stop_if_failure_rate_exceeds_threshold: 0.0-1.0
    cancel_running_on_policy_stop:
      **v0.6.0.1 では予約フィールド (未実装)**。
      stop_on_first_error / stop_if_failure_rate_exceeds 時に既に実行中の
      target に cancel を要求する設計だが、v0.6.0.1 では stop_requested フラグは
      未開始 target を skipped にするのみに使用。実行中 target は現在 step を
      完了するまで継続する。v0.6.1 以降で policy_cancel_requested 経路を追加予定。
    retry_safe_shutdown_before_retry:
      **v0.6.0.1 では予約フィールド (未実装)**。
      retry の前に target の safe_shutdown を試行する設計。v0.7.0+ の SQLite
      永続化強化と合わせて実装予定。
    """
    mode: str = "continue"
    retry: int = 0
    stop_if_failure_rate_exceeds_threshold: float = 0.5
    cancel_running_on_policy_stop: bool = False
    retry_safe_shutdown_before_retry: bool = False

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "FailurePolicy":
        if not d:
            return cls()
        return cls(
            mode=d.get("mode", "continue"),
            retry=int(d.get("retry", 0)),
            stop_if_failure_rate_exceeds_threshold=float(
                d.get("stop_if_failure_rate_exceeds", 0.5)
            ),
            cancel_running_on_policy_stop=bool(d.get("cancel_running_on_policy_stop", False)),
            retry_safe_shutdown_before_retry=bool(
                d.get("retry_safe_shutdown_before_retry", False)
            ),
        )

    VALID_MODES = ("continue", "stop_on_first_error", "stop_if_failure_rate_exceeds")

    def validate(self) -> None:
        if self.mode not in self.VALID_MODES:
            raise ValueError(
                f"failure_policy.mode は {self.VALID_MODES} のいずれか: {self.mode!r}"
            )
        if self.retry < 0:
            raise ValueError(f"failure_policy.retry は 0 以上: {self.retry}")
        if not (0.0 <= self.stop_if_failure_rate_exceeds_threshold <= 1.0):
            raise ValueError(
                f"stop_if_failure_rate_exceeds 閾値は 0..1: "
                f"{self.stop_if_failure_rate_exceeds_threshold}"
            )
