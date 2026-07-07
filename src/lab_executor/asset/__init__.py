"""実験資産 (experiment asset) v0.1 (v2.25.0)

MaiML (JIS K 0200:2024) の独立可用性概念に基づく実験資産の生成・検査。
CLI ``lab-executor asset export`` / ``asset check`` のバックエンド。

- ``manifest``: asset.yaml のスキーマ (AssetManifest)
- ``levels``: L0〜L5 判定の純関数群 (I/O しない)
- ``capability``: L4 用 capability 照合
- ``builder``: 完了 Job から asset zip を生成
- ``checker``: asset zip を読み、独立可用性レベルを機械判定

仕様の正本: docs/experiment_asset_schema_v0.md / docs/asset_v01_plan.md
"""
from __future__ import annotations

from lab_executor.asset.manifest import (
    ASSET_VERSION,
    AssetManifest,
    ContentEntry,
)
from lab_executor.asset.capability import match_capabilities
from lab_executor.asset.builder import build_asset
from lab_executor.asset.checker import CheckReport, check_asset
from lab_executor.asset.registry import (
    AssetRegistryError,
    catalog,
    init_registry,
    load_index,
    publish_asset,
)

__all__ = [
    "ASSET_VERSION",
    "AssetManifest",
    "ContentEntry",
    "match_capabilities",
    "build_asset",
    "CheckReport",
    "check_asset",
    "AssetRegistryError",
    "init_registry",
    "load_index",
    "publish_asset",
    "catalog",
]
