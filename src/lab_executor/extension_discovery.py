"""v2.4.0: Dual-path extension discovery + duplicate conflict detection.

`ExtensionPaths.active_read_paths` を走査し、install 済 pack を列挙する
唯一の source of truth。`catalog` / `check` / 将来の `migration-plan`
が同じ結果を共有する。

基本ポリシー (v2.4):

- 読み取り順序: `active_read_paths` の順 (v2.4 は new_path → legacy_path)
- duplicate `extension_id` が複数 path にあった場合、**自動採用しない**。
  `ExtensionDiscoveryResult.duplicates` に列挙し、warning として報告する。
- ディレクトリ名ではなく `extension.yaml` の `extension_id` で判定。
- YAML が読めない場合は `invalid_extension_metadata` /
  `missing_extension_yaml` として errors / warnings に分けて報告。

PyVISA / `visa_mcp` への依存は一切持たない (CI gate あり)。
"""
from __future__ import annotations
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from lab_executor.extension_paths import (
    ExtensionPaths,
    get_extension_paths,
    DUPLICATE_POLICY,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InstalledExtension:
    """1 件の install 済 pack を表す不変 record"""
    extension_id: str
    path: Path
    source_path: Path  # 親 path (active_read_paths のいずれか)
    metadata: dict
    install_meta: dict | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "extension_id": self.extension_id,
            "path": str(self.path),
            "source_path": str(self.source_path),
            "version": self.metadata.get("version", ""),
            "support_level": (
                (self.metadata.get("stability") or {}).get(
                    "support_level", "")
            ),
            "installed_at": (
                (self.install_meta or {}).get("installed_at", "")
            ),
        }


@dataclass
class ExtensionDiscoveryResult:
    """discover_installed_extensions() の戻り値"""
    extensions: list[InstalledExtension] = field(default_factory=list)
    duplicates: dict[str, list[InstalledExtension]] = field(
        default_factory=dict)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    duplicate_policy: str = DUPLICATE_POLICY

    def has_duplicates(self) -> bool:
        return bool(self.duplicates)

    def to_dict(self) -> dict[str, Any]:
        return {
            "extensions": [e.to_dict() for e in self.extensions],
            "duplicates": [
                {
                    "extension_id": eid,
                    "locations": [str(e.path) for e in entries],
                    "error_class": "duplicate_extension_id",
                }
                for eid, entries in self.duplicates.items()
            ],
            "warnings": list(self.warnings),
            "errors": list(self.errors),
            "duplicate_policy": self.duplicate_policy,
            "count": len(self.extensions),
            "duplicate_count": len(self.duplicates),
        }


def _read_install_meta(pack_dir: Path) -> dict | None:
    """`.install_meta.json` を読む。無ければ None。"""
    p = pack_dir / ".install_meta.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("install_meta.json parse failed: %s", e)
        return None


def _scan_path(
    parent: Path,
    *,
    errors: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> list[InstalledExtension]:
    """1 つの parent path 配下を走査し、install 済 pack を列挙する。

    parent 自体が存在しない場合は空 list を返す (error にしない:
    v2.4 では new_path はまだ存在しないのが普通)。
    """
    result: list[InstalledExtension] = []
    if not parent.exists() or not parent.is_dir():
        return result

    for child in sorted(parent.iterdir()):
        if not child.is_dir():
            continue
        # ドットで始まる internal dir (backup 等) は除外
        if child.name.startswith("."):
            continue
        manifest = child / "extension.yaml"
        if not manifest.exists():
            warnings.append({
                "warning_class": "missing_extension_yaml",
                "message": (
                    f"extension.yaml が見つかりません: {child}"
                ),
                "path": str(child),
            })
            continue
        try:
            metadata = yaml.safe_load(
                manifest.read_text(encoding="utf-8")
            ) or {}
        except Exception as e:
            errors.append({
                "error_class": "invalid_extension_metadata",
                "message": (
                    f"extension.yaml parse failed: {e}"
                ),
                "path": str(manifest),
            })
            continue
        ext_id = metadata.get("extension_id")
        if not ext_id or not isinstance(ext_id, str):
            errors.append({
                "error_class": "invalid_extension_metadata",
                "message": (
                    f"extension_id が無い / 文字列でない: {manifest}"
                ),
                "path": str(manifest),
            })
            continue
        result.append(InstalledExtension(
            extension_id=ext_id,
            path=child,
            source_path=parent,
            metadata=metadata,
            install_meta=_read_install_meta(child),
        ))
    return result


def discover_installed_extensions(
    paths: ExtensionPaths | None = None,
) -> ExtensionDiscoveryResult:
    """v2.4.0: `active_read_paths` を走査して install 済 pack を列挙し、
    duplicate `extension_id` を検出する。

    duplicate 時は **自動採用しない**。`duplicates` に locations を
    全件列挙し、`extensions` には先頭の (active_read_paths 順での)
    record だけを残す (catalog 表示用)。後段の `catalog` / `check`
    で warning / error 化する。

    Args:
        paths: 上書き用 ExtensionPaths (test 用)。None なら
            `get_extension_paths()` を使う。

    Returns:
        ExtensionDiscoveryResult
    """
    paths = paths or get_extension_paths()
    result = ExtensionDiscoveryResult(
        duplicate_policy=paths.duplicate_policy)

    # path ごとに scan
    by_id: dict[str, list[InstalledExtension]] = {}
    for parent in paths.active_read_paths:
        for ext in _scan_path(
            parent, errors=result.errors, warnings=result.warnings,
        ):
            by_id.setdefault(ext.extension_id, []).append(ext)

    # 単一 / duplicate を仕分け
    for eid, entries in by_id.items():
        if len(entries) == 1:
            result.extensions.append(entries[0])
            continue
        # duplicate: 全件を duplicates に、先頭のみ extensions に
        result.duplicates[eid] = list(entries)
        result.extensions.append(entries[0])
        result.warnings.append({
            "warning_class": "duplicate_extension_id",
            "message": (
                f"duplicate extension_id={eid!r} (locations={len(entries)}). "
                f"v2.4 policy: {paths.duplicate_policy}. "
                f"Resolve by removing one copy or run migration plan."
            ),
            "extension_id": eid,
            "locations": [str(e.path) for e in entries],
            "recommended_actions": [
                {"action": "remove_one_copy"},
                {"action": "run_migration_plan"},
            ],
        })

    return result
