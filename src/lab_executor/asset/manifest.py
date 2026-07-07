"""asset.yaml のスキーマ定義 (AssetManifest, v0.1 / v2.25.0)

docs/experiment_asset_schema_v0.md の定義に従う pydantic モデル。
build_asset が生成し、check_asset がスキーマ検証に使う。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


ASSET_VERSION = "0.1"


class ProvenanceInfo(BaseModel):
    producer: str = ""
    runtime: str = ""                      # "lab-executor-mcp <version>"
    git_commit: str | None = None


class ConditionsInfo(BaseModel):
    """L2 用。無い項目は "not_recorded" を明示 (キー欠落は不可)。"""
    calibration: str = "not_recorded"
    environment: str = "not_recorded"


class SampleInfo(BaseModel):
    """Phase B。MaiML <material> 相当の最小実装。"""
    uuid: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class HazardsInfo(BaseModel):
    """Phase B (L5)。危険性宣言。"""
    none_declared: bool = False
    voltage_max: float | None = None
    temperature_max: float | None = None
    chemicals: list[str] = Field(default_factory=list)
    notes: str = ""


class ExpectedResult(BaseModel):
    """Phase B (L5)。再実行時の期待結果 (許容範囲付き)。"""
    command: str
    value_min: float | None = None
    value_max: float | None = None


class DryRunInfo(BaseModel):
    """Phase B (L5)。dry-run 検証記録。

    v0.2 (v2.26.0): builder が ``--dry-run-now`` で export 時に
    梱包レシピをコンパイル検証し、その結果をここに記入できる。
    """
    performed_at: str | None = None
    ok: bool | None = None
    method: str | None = None            # 例: recipe_to_plan+validate@export
    runtime: str | None = None           # lab-executor-mcp <version>
    step_count: int | None = None
    error: str | None = None             # ok=False のときの一行要約


class ContentEntry(BaseModel):
    """同梱ファイル 1 件 (sha256 + 種別)。"""
    path: str
    sha256: str
    kind: Literal[
        "results", "run_metadata", "instrument", "recipe",
        "analysis", "log", "other",
    ] = "other"


class AssetManifest(BaseModel):
    """実験資産マニフェスト (asset.yaml) v0.1。"""
    asset_version: str = ASSET_VERSION
    asset_id: str
    level_declared: int = Field(ge=0, le=5)
    level_verified: int | None = None
    title: str = ""
    created_at: str = ""
    license: str = "UNLICENSED"
    provenance: ProvenanceInfo = Field(default_factory=ProvenanceInfo)
    conditions: ConditionsInfo = Field(default_factory=ConditionsInfo)
    sample: SampleInfo | None = None
    hazards: HazardsInfo | None = None
    expected_results: list[ExpectedResult] = Field(default_factory=list)
    dry_run: DryRunInfo | None = None
    contents: list[ContentEntry] = Field(default_factory=list)

    def to_yaml_dict(self) -> dict[str, Any]:
        """YAML 直列化用の dict (None を含む素の dump)。"""
        return self.model_dump(mode="json")
