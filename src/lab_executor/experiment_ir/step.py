"""
Experiment IR の Step 型定義 (v0.5.0 / v0.5.1)

discriminator フィールド `type` による Pydantic discriminated union。

v0.5.0:
- CommandStep
- WaitStep (単純秒待機)

v0.5.1:
- WaitUntilStep              ── 絶対 / 相対の deadline まで待つ
- WaitForConditionStep       ── 条件式が True になるまで polling
- WaitForStableStep          ── window 内の (max - min) が tolerance 以下になるまで polling

今後のバージョンで以下を追加予定:
- GroupStep / BarrierStep / StaggerStep (v0.6.x)
- SweepStep / ParallelStep / LoopStep / BranchStep (v0.8.0 DSL)
- SafeShutdownStep (v0.8.0)
"""
from __future__ import annotations
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, field_validator, model_validator


class CommandStep(BaseModel):
    """
    YAML 機器定義の名前付きコマンドを 1 回実行するステップ。

    `command` は機器定義の commands.<name> を参照するキー。
    `args` の値は文字列で "$" 始まりなら式評価 (recipe parameter を変数として参照)。
    `result_as` を指定すると後続ステップから ${steps.<result_as>} で参照可能 (v0.6.0+)。

    v0.6.0 再導入:
    `instrument` は logical role 参照 ("$psu" 形式) または alias / resource 名。
    map_recipe の target 内で bindings 経由で実 resource に解決される。
    省略時は Job の主 resource (start_recipe_job の resource_name) を使う。
    """
    type: Literal["command"] = "command"
    command: str
    args: dict[str, Any] = Field(default_factory=dict)
    result_as: str | None = None
    description: str = ""
    # v0.6.0: logical instrument ref. None なら Job 主 resource を使用
    instrument: str | None = None
    # v0.6.1: step 開始の意図的な遅延 (ms)
    # Map Job の各 target が同じ command step を実行する際、target_index に応じた
    # 遅延 (target_index * stagger_ms / 1000) を入れて突入電流等を避ける。
    # 単一 Job / 通常 recipe では効果なし (target_index=0 のみ)。
    # None なら遅延なし。
    stagger_ms: int | None = None

    @field_validator("stagger_ms")
    @classmethod
    def _stagger_nonneg(cls, v: int | None) -> int | None:
        if v is not None and v < 0:
            raise ValueError(f"stagger_ms は 0 以上である必要があります: {v}")
        if v is not None and v > 600_000:  # 10 分上限 (誤入力防止)
            raise ValueError(
                f"stagger_ms は最大 600000 (10 分) です: {v}"
            )
        return v


class WaitStep(BaseModel):
    """
    指定秒数だけ待機するステップ (v0.5.0-rc1)。
    """
    type: Literal["wait"] = "wait"
    seconds: float
    description: str = ""

    @field_validator("seconds")
    @classmethod
    def _validate_non_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError(f"WaitStep.seconds は 0 以上である必要があります: {v}")
        return v


# ============================================================
# v0.5.1: Polling wait Step
# ============================================================


class WaitUntilStep(BaseModel):
    """
    指定された絶対時刻 (ISO8601) または相対秒数の deadline まで待つ (v0.5.1)。

    timestamp: ISO8601 文字列 (例: "2026-05-22T15:00:00+09:00")
    seconds_from_now: 開始時刻からの相対秒数 (timestamp と排他、どちらか一方を指定)

    cancel / job_timeout への即応は manager 側で slice ループにより実現。
    """
    type: Literal["wait_until"] = "wait_until"
    timestamp: str | None = None
    seconds_from_now: float | None = None
    description: str = ""

    @model_validator(mode="after")
    def _exactly_one(self) -> "WaitUntilStep":
        has_ts = self.timestamp is not None and self.timestamp != ""
        has_sec = self.seconds_from_now is not None
        if has_ts and has_sec:
            raise ValueError("wait_until: timestamp と seconds_from_now は排他です")
        if not has_ts and not has_sec:
            raise ValueError("wait_until: timestamp または seconds_from_now のいずれかが必須です")
        if has_sec and self.seconds_from_now < 0:  # type: ignore[operator]
            raise ValueError("wait_until.seconds_from_now は 0 以上である必要があります")
        return self


class _PollingCommon(BaseModel):
    """polling 系 Step の共通フィールドと validation"""
    instrument: str
    command: str
    args: dict[str, Any] = Field(default_factory=dict)
    interval_s: float = 1.0
    timeout_s: float = 60.0
    command_timeout_s: float | None = None  # 1 回の query に対する VISA timeout (None = command 定義値)
    value_path: str | None = None           # parsed response 内の数値フィールド名
    retry_on_error: int = 1                 # 1 polling 失敗時の即時 retry 回数
    max_consecutive_errors: int = 3         # 連続失敗許容数。超えたら step failed
    description: str = ""

    @field_validator("interval_s")
    @classmethod
    def _interval_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"interval_s は正の値である必要があります: {v}")
        return v

    @field_validator("timeout_s")
    @classmethod
    def _timeout_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"timeout_s は正の値である必要があります: {v}")
        return v

    @field_validator("retry_on_error")
    @classmethod
    def _retry_nonneg(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"retry_on_error は 0 以上である必要があります: {v}")
        return v

    @field_validator("max_consecutive_errors")
    @classmethod
    def _mce_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"max_consecutive_errors は 1 以上である必要があります: {v}")
        return v


class WaitForConditionStep(_PollingCommon):
    """
    条件式が True を返すまで定期測定するステップ (v0.5.1)。

    condition_expr: 許可される構文 (safe_eval_condition):
      - 変数 `value` (最新の measurement)
      - 数値リテラル
      - 比較演算子: < <= > >= == !=
      - 論理演算: and / or
      - abs(value - target) のような単項関数 abs()
    禁止: 属性 / 関数呼び出し全般 / import / indexing / 文字列操作 / 代入 / 内包表記

    例:
      condition_expr: "value > 80"
      condition_expr: "abs(value - 25) < 0.2"
    """
    type: Literal["wait_for_condition"] = "wait_for_condition"
    condition_expr: str

    @field_validator("condition_expr")
    @classmethod
    def _cond_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("condition_expr は空にできません")
        return v


class WaitForStableStep(_PollingCommon):
    """
    window_s 期間内の測定値が安定 (max - min <= tolerance) するまで polling (v0.5.1)。

    定義:
      max(samples_in_window) - min(samples_in_window) <= tolerance
    で stable と判定。
    最低 min_samples 点 (デフォルト 3) のサンプルが必要。

    method は v0.5.1 では "range" のみ対応。
    将来 "stddev" / "slope" / "median_range" を追加可能。
    """
    type: Literal["wait_for_stable"] = "wait_for_stable"
    tolerance: float
    window_s: float
    min_samples: int = 3
    method: Literal["range"] = "range"

    @field_validator("tolerance")
    @classmethod
    def _tol_nonneg(cls, v: float) -> float:
        if v < 0:
            raise ValueError(f"tolerance は 0 以上である必要があります: {v}")
        return v

    @field_validator("window_s")
    @classmethod
    def _window_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"window_s は正の値である必要があります: {v}")
        return v

    @field_validator("min_samples")
    @classmethod
    def _ms_positive(cls, v: int) -> int:
        if v < 2:
            raise ValueError(f"min_samples は 2 以上である必要があります: {v}")
        return v

    @model_validator(mode="after")
    def _cross_check(self) -> "WaitForStableStep":
        # window <= timeout
        if self.window_s > self.timeout_s:
            raise ValueError(
                f"window_s ({self.window_s}) は timeout_s ({self.timeout_s}) 以下である必要があります"
            )
        # interval <= window
        if self.interval_s > self.window_s:
            raise ValueError(
                f"interval_s ({self.interval_s}) は window_s ({self.window_s}) 以下である必要があります"
            )
        # 測定点数下限
        # ceil(window / interval) + 1 >= min_samples
        import math
        possible = math.ceil(self.window_s / self.interval_s) + 1
        if possible < self.min_samples:
            raise ValueError(
                f"window_s/interval_s から得られる最大サンプル数 ({possible}) が "
                f"min_samples ({self.min_samples}) に満たないため安定判定不可能です"
            )
        return self


# ============================================================
# v0.6.1: Barrier (Group/Map 同期点)
# ============================================================


class BarrierStep(BaseModel):
    """v0.6.1: Group/Map Job 内の target 間同期点。

    複数 target が同じ name の BarrierStep に到達するまで待機し、
    全 target 到達 (または failure_policy で除外された target を除いて) で
    次 step へ進む。

    重要 (実装方針):
      - **barrier 待ち中は target-level resource lock を解放する**
        (deadlock 回避: 親 Job lock があるので外部からは触られない)
      - **失敗 target は barrier 対象から自動除外** (failure_policy=continue 時)
      - barrier_key = (name, step_index) ── 同一 name でも step_index が違えば別物
      - timeout_s 必須 (無限待ち禁止)

    対応範囲 (v0.6.1 MVP):
      - same Map/Group Job 内 target 間 barrier のみ
      - quorum / nested / target-local Plan 内 barrier は未対応

    Field notes (v0.6.1.1 補足):
      - `timeout_s` は **必ず有限値を持つ**。省略時 default=60s で無限待ちは禁止。
        (旧 docstring の「必須」表現を訂正: 省略可能だが必ず有限値)
    """
    type: Literal["barrier"] = "barrier"
    name: str
    timeout_s: float = 60.0   # 省略可能だが必ず有限値 (無限待ち禁止)
    description: str = ""

    @field_validator("name")
    @classmethod
    def _name_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("BarrierStep.name は空にできません")
        return v

    @field_validator("timeout_s")
    @classmethod
    def _timeout_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"BarrierStep.timeout_s は正の値: {v}")
        return v


# discriminated union: type フィールドで自動的に正しいモデルが選ばれる
Step = Annotated[
    Union[
        CommandStep, WaitStep, WaitUntilStep,
        WaitForConditionStep, WaitForStableStep,
        BarrierStep,
    ],
    Field(discriminator="type"),
]
