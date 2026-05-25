"""v0.9.0: Benchmark task schema + loader.

`benchmarks/tasks/*.yaml` を Pydantic で読み込む。
3 層 benchmark の全データソース:
  - input (instruction / available_units / plan / template_name + override)
  - expected (plan_features / required_tool_sequence / success_criteria)
  - fixtures (system_config / instruments yaml paths)
"""
from __future__ import annotations
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator


# ============================================================
# 期待動作
# ============================================================


class PlanFeatures(BaseModel):
    """Plan が満たすべき構造的特徴"""
    uses_unit: bool | None = None
    uses_sweep: bool | None = None
    uses_parallel: bool | None = None
    uses_wait_for_stable: bool | None = None
    uses_wait_for_condition: bool | None = None
    uses_safe_shutdown: bool | None = None
    uses_verify: bool | None = None
    uses_template_override: bool | None = None
    min_required_resources: int | None = None
    max_required_resources: int | None = None


class ExpectedToolSequence(BaseModel):
    """Tool 呼び出し列の期待: 厳密一致ではなく「含有 / 禁止」で判定"""
    required_order: list[str] = Field(default_factory=list)
    optional: list[str] = Field(default_factory=list)
    forbidden: list[str] = Field(default_factory=list)


class SuccessCriteria(BaseModel):
    """benchmark 成功条件"""
    validation_status: Literal["ok", "error", "any"] = "any"
    dry_run_has_no_errors: bool | None = None
    dry_run_max_warnings: int | None = None
    job_status: Literal["completed", "failed", "any"] = "any"
    expected_job_outcome: Literal[
        "success", "partial_failure", "failure",
        "cancelled", "interrupted", "any",
    ] = "any"
    expected_verify_failed_count: int | None = None
    expected_measurements: list[str] = Field(default_factory=list)


class ExpectedSpec(BaseModel):
    plan_features: PlanFeatures = Field(default_factory=PlanFeatures)
    required_tool_sequence: ExpectedToolSequence = Field(
        default_factory=ExpectedToolSequence,
    )
    success_criteria: SuccessCriteria = Field(default_factory=SuccessCriteria)


# ============================================================
# Input
# ============================================================


class InputSpec(BaseModel):
    """benchmark task の入力。

    LLM 評価モード (v0.9.1+) では instruction + available_units を渡して
    LLM に Plan を生成させる。v0.9.0 では plan / template_name + override
    どちらかを直接渡す。
    """
    instruction: str = ""
    available_units: list[str] = Field(default_factory=list)
    plan: dict[str, Any] | None = None
    template_name: str | None = None
    template_override: dict[str, Any] | None = None

    @field_validator("plan")
    @classmethod
    def _plan_dict(cls, v):
        if v is None:
            return v
        if not isinstance(v, dict):
            raise ValueError("input.plan は dict が必要")
        return v


class Fixtures(BaseModel):
    """fixture file paths (benchmarks/fixtures/... からの相対 or 絶対)"""
    system_config: str | None = None
    instruments: list[str] = Field(default_factory=list)
    mock_scenarios: dict[str, Any] | None = None
    templates: list[dict[str, Any]] = Field(default_factory=list)
    # v0.9.0.1: benchmark 再現性確保のための seed (mock の random / asyncio jitter
    # 等に影響)。指定しない場合は None で OS 既定 (再現性は保証されない)。
    random_seed: int | None = None
    # v0.9.0.1: benchmark 実行時の安全モード (mock との組み合わせで挙動が
    # 変わる箇所がある。例: strict なら verify 失敗で step_failed になり
    # job_status=failed に到達する)。None なら環境変数を尊重。
    safety_mode: str | None = None


# ============================================================
# Root
# ============================================================


class ExpectedFailure(BaseModel):
    """v0.9.1: repair task 用「broken_plan が満たすべき失敗仕様」"""
    phase: Literal["validate", "dry_run", "execute"] = "validate"
    error_class: str | None = None
    field_path: str | None = None
    step_path: str | None = None
    required_recommended_actions: list[str] = Field(default_factory=list)


class ExpectedRepair(BaseModel):
    """v0.9.1: repair task 用「repaired_plan が満たすべき仕様」"""
    repair_actions: list[str] = Field(default_factory=list)
    must_not: list[str] = Field(default_factory=list)
    layer: Literal["validate", "dry_run", "execute"] = "dry_run"


class BenchmarkTask(BaseModel):
    """benchmarks/tasks/*.yaml ルート"""
    id: str
    title: str = ""
    description: str = ""
    # v0.9.1: "repair" mode を layer enum に追加
    layer: Literal["validate", "dry_run", "execute", "repair"] = "execute"
    input: InputSpec = Field(default_factory=InputSpec)
    expected: ExpectedSpec = Field(default_factory=ExpectedSpec)
    fixtures: Fixtures = Field(default_factory=Fixtures)

    # ---- repair 専用 (layer="repair" のとき必須) ----
    # broken_plan: 失敗するはずの DSL plan (input.plan / template と排他)
    broken_plan: dict[str, Any] | None = None
    repaired_plan: dict[str, Any] | None = None
    expected_failure: ExpectedFailure | None = None
    expected_repair: ExpectedRepair | None = None

    @field_validator("id")
    @classmethod
    def _id_format(cls, v: str) -> str:
        if not v or " " in v:
            raise ValueError(f"task id 不正: {v!r}")
        return v


# ============================================================
# Loaders
# ============================================================


def load_benchmark_task(path: str | Path) -> BenchmarkTask:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(p)
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return BenchmarkTask(**raw)


def load_benchmark_tasks(directory: str | Path) -> list[BenchmarkTask]:
    base = Path(directory)
    tasks: list[BenchmarkTask] = []
    for p in sorted(base.glob("*.yaml")):
        tasks.append(load_benchmark_task(p))
    return tasks
