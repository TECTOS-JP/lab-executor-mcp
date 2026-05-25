"""
v0.8.0: Experiment DSL schema

LLM が生成する JSON 実験計画を Pydantic で受け取り、validation する。

設計原則:
- JSON として閉じた構造 (Python 式 / 関数呼び出し禁止)
- discriminated union by `type`
- 既存 IR (CommandStep / WaitStep / ...) に compile される
- `dsl_version` で将来の schema migration に備える
"""
from __future__ import annotations
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, field_validator, model_validator


# 上限値 (誤入力防止) ─ レビュー方針より
MAX_SWEEP_POINTS = 200
MAX_PARALLEL_CONCURRENCY = 10
MAX_PARALLEL_BRANCHES = 100
MAX_PLAN_STEPS = 500           # 1 plan あたりの top-level step 上限
CURRENT_DSL_VERSION = "0.8"


# ============================================================
# 基本 step 型
# ============================================================


class DSLCommandStep(BaseModel):
    """command 実行 (write/query 両対応)"""
    type: Literal["command"] = "command"
    instrument: str                                       # alias / "$role" / resource
    command: str
    args: dict[str, Any] = Field(default_factory=dict)
    save_as: str | None = None                           # 後続参照用 (v0.8.0 では reserve)
    description: str = ""


class DSLQueryStep(BaseModel):
    """query 専用 step (意味的には command と同じだが LLM 可読性のため分離)"""
    type: Literal["query"] = "query"
    instrument: str
    command: str
    args: dict[str, Any] = Field(default_factory=dict)
    save_as: str | None = None
    description: str = ""


class DSLWaitStep(BaseModel):
    """単純秒待機"""
    type: Literal["wait"] = "wait"
    seconds: float
    description: str = ""

    @field_validator("seconds")
    @classmethod
    def _nonneg(cls, v: float) -> float:
        if v < 0:
            raise ValueError(f"wait.seconds は 0 以上: {v}")
        return v


class DSLWaitUntilStep(BaseModel):
    type: Literal["wait_until"] = "wait_until"
    timestamp: str | None = None
    seconds_from_now: float | None = None
    description: str = ""

    @model_validator(mode="after")
    def _exactly_one(self) -> "DSLWaitUntilStep":
        has_ts = self.timestamp not in (None, "")
        has_sec = self.seconds_from_now is not None
        if has_ts == has_sec:
            raise ValueError(
                "wait_until: timestamp または seconds_from_now のいずれか一方が必須"
            )
        return self


class DSLWaitForConditionStep(BaseModel):
    type: Literal["wait_for_condition"] = "wait_for_condition"
    instrument: str
    command: str
    condition_expr: str
    args: dict[str, Any] = Field(default_factory=dict)
    interval_s: float = 1.0
    timeout_s: float = 60.0
    command_timeout_s: float | None = None
    value_path: str | None = None
    retry_on_error: int = 1
    max_consecutive_errors: int = 3
    description: str = ""

    @field_validator("interval_s", "timeout_s")
    @classmethod
    def _positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"interval_s / timeout_s は正の値: {v}")
        return v


class DSLWaitForStableStep(BaseModel):
    type: Literal["wait_for_stable"] = "wait_for_stable"
    instrument: str
    command: str
    tolerance: float
    window_s: float
    args: dict[str, Any] = Field(default_factory=dict)
    interval_s: float = 1.0
    timeout_s: float = 60.0
    command_timeout_s: float | None = None
    value_path: str | None = None
    min_samples: int = 3
    retry_on_error: int = 1
    max_consecutive_errors: int = 3
    description: str = ""


class DSLBarrierStep(BaseModel):
    """Group/Map 内 target 間同期 (parallel branch 間でも使用可)"""
    type: Literal["barrier"] = "barrier"
    name: str
    timeout_s: float = 60.0
    description: str = ""

    @field_validator("name")
    @classmethod
    def _name_nonempty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("barrier.name は空にできません")
        return v


class DSLSafeShutdownStep(BaseModel):
    """このプランで使用した resource (または明示指定 targets) を安全停止。

    v0.8.0 では「Plan で使用した全 resource」へ best_effort_safe_shutdown を適用。
    `targets` 明示指定で対象を絞り込み可能。
    """
    type: Literal["safe_shutdown"] = "safe_shutdown"
    targets: list[str] | None = None    # None or [] なら "使用した全 resource"
    description: str = ""


# ============================================================
# Sweep / Parallel (compile 時展開される複合 step)
# ============================================================


class SweepValues(BaseModel):
    """sweep の値指定 (start/stop/step または values list)"""
    start: float | None = None
    stop: float | None = None
    step: float | None = None
    values: list[Any] | None = None    # 明示リスト指定 (推奨)

    @model_validator(mode="after")
    def _exactly_one_form(self) -> "SweepValues":
        has_values = self.values is not None and len(self.values) > 0
        has_range = all(v is not None for v in (self.start, self.stop, self.step))
        if has_values and has_range:
            raise ValueError(
                "sweep.values と start/stop/step は排他です"
            )
        if not (has_values or has_range):
            raise ValueError(
                "sweep には values または start/stop/step (全 3 つ) のどちらかが必要"
            )
        if has_range:
            if self.step == 0:
                raise ValueError("sweep.step=0 は許可されません")
            # 方向チェック
            if self.step > 0 and self.start > self.stop:
                raise ValueError(
                    f"sweep: step>0 なら start <= stop が必要: "
                    f"start={self.start} stop={self.stop} step={self.step}"
                )
            if self.step < 0 and self.start < self.stop:
                raise ValueError(
                    f"sweep: step<0 なら start >= stop が必要"
                )
        return self

    def expand(self) -> list[Any]:
        """展開後の値リスト (compile / validate で使う)"""
        if self.values is not None:
            return list(self.values)
        # start/stop/step (浮動小数累積誤差を避けるため index ベース)
        import math
        n = math.floor((self.stop - self.start) / self.step + 1e-9) + 1
        return [self.start + i * self.step for i in range(max(0, n))]


# 注: SweepStep / ParallelStep は再帰参照のため後段で Step union を確立してから定義


# ============================================================
# Plan root
# ============================================================


class ExperimentPlan(BaseModel):
    """LLM が生成する実験計画のルート"""
    dsl_version: str = CURRENT_DSL_VERSION
    name: str = ""
    description: str = ""
    # v0.8.3: experiment_unit 直接参照 (任意)
    # system_config.experiment_units[unit] の bindings を effective_bindings へ
    # 展開する。explicit `bindings` は同 role を上書きする (override 優先)。
    unit: str | None = None
    # bindings: $role 形式の DSL ref を解決するための alias マップ
    # 例: {"psu": "psu001", "dmm": "dmm001"}
    # unit 指定時はこれが unit_bindings への override として働く。
    bindings: dict[str, str] = Field(default_factory=dict)
    # 全 step 共通の variables (sweep 変数とは別、テンプレート展開用)
    variables: dict[str, Any] = Field(default_factory=dict)
    steps: list["Step"] = Field(default_factory=list)

    @field_validator("dsl_version")
    @classmethod
    def _dsl_ver_known(cls, v: str) -> str:
        # 互換性: 現状 "0.8" のみ
        if v not in ("0.8",):
            raise ValueError(f"未対応の dsl_version: {v} (current: 0.8)")
        return v

    @field_validator("steps")
    @classmethod
    def _step_count_limit(cls, v: list) -> list:
        if len(v) > MAX_PLAN_STEPS:
            raise ValueError(
                f"steps が {MAX_PLAN_STEPS} を超えています ({len(v)})"
            )
        return v


# Sweep / Parallel は body が他の Step を持つ再帰構造のため、
# Step union を定義した後に rebuild する。


class DSLSweepStep(BaseModel):
    """変数 sweep。compile 時に body を value 数だけ複製して展開する"""
    type: Literal["sweep"] = "sweep"
    parameter: str               # 変数名 ({voltage} 等で参照される)
    values: SweepValues
    body: list["Step"] = Field(default_factory=list)
    description: str = ""

    def expanded_count(self) -> int:
        return len(self.values.expand())

    @model_validator(mode="after")
    def _within_limit(self) -> "DSLSweepStep":
        n = self.expanded_count()
        if n > MAX_SWEEP_POINTS:
            raise ValueError(
                f"sweep の展開点数 {n} が上限 {MAX_SWEEP_POINTS} を超過。"
                f"start/stop/step を見直すか values を分割してください"
            )
        if n == 0:
            raise ValueError("sweep の展開結果が 0 点になります")
        return self


class DSLParallelStep(BaseModel):
    """branches を並列実行。compile 時に GroupExecutor の target に転送される"""
    type: Literal["parallel"] = "parallel"
    concurrency: int = 1
    branches: list[list["Step"]] = Field(default_factory=list)
    description: str = ""

    @model_validator(mode="after")
    def _within_limits(self) -> "DSLParallelStep":
        if self.concurrency < 1:
            raise ValueError(f"parallel.concurrency は 1 以上: {self.concurrency}")
        if self.concurrency > MAX_PARALLEL_CONCURRENCY:
            raise ValueError(
                f"parallel.concurrency が上限 {MAX_PARALLEL_CONCURRENCY} 超過: "
                f"{self.concurrency}"
            )
        if len(self.branches) == 0:
            raise ValueError("parallel.branches が空です")
        if len(self.branches) > MAX_PARALLEL_BRANCHES:
            raise ValueError(
                f"parallel.branches が上限 {MAX_PARALLEL_BRANCHES} 超過: "
                f"{len(self.branches)}"
            )
        return self


# ============================================================
# Step union (discriminated by type)
# ============================================================


Step = Annotated[
    Union[
        DSLCommandStep, DSLQueryStep,
        DSLWaitStep, DSLWaitUntilStep,
        DSLWaitForConditionStep, DSLWaitForStableStep,
        DSLBarrierStep, DSLSafeShutdownStep,
        DSLSweepStep, DSLParallelStep,
    ],
    Field(discriminator="type"),
]


# 再帰参照 rebuild
ExperimentPlan.model_rebuild()
DSLSweepStep.model_rebuild()
DSLParallelStep.model_rebuild()
