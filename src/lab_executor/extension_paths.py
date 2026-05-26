"""v2.3.0: Extension install path resolver (central API)

v1.x からの互換のため、現在の default は `~/.visa-mcp/extensions/` を
継続使用する。v2.4 以降で `~/.lab-executor/extensions/` への dual-read
を検討し、v2.5+ で default 切替を判断する段階的 migration を計画。

このモジュールは「どの path を読み書きするか」を 1 箇所で決め、
CLI / install 系 helper / `extension catalog / paths` などが
参照する。
"""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ExtensionPaths:
    """v2.3.0: install path 一覧。

    Attributes:
        current_default: 現在 install 先として使う path
            (v2.3 時点では `~/.visa-mcp/extensions/`)
        future_default_candidate: v2.5+ で default 化を検討する候補
            (`~/.lab-executor/extensions/`)
        active_read_paths: catalog / check が読み込む path 一覧
            (v2.3: current_default 単独、v2.4+: dual-read 拡張)
        migration_required: v2.4+ で current_default 以外に既存
            install がある場合に True (v2.3 では常に False)
    """
    current_default: Path
    future_default_candidate: Path
    active_read_paths: list[Path] = field(default_factory=list)
    migration_required: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "current_default": str(self.current_default),
            "future_default_candidate": str(
                self.future_default_candidate),
            "active_read_paths": [
                str(p) for p in self.active_read_paths
            ],
            "migration_required": self.migration_required,
            "schema_version": "v2.3",
        }


def get_extension_paths() -> ExtensionPaths:
    """v2.3.0: 現在の install path 構成を取得する公開 API。

    v2.3 では `~/.visa-mcp/extensions/` のみが active read path。
    v2.4 で `~/.lab-executor/extensions/` を加え、v2.5+ で default
    切替を判断する予定。

    Returns:
        ExtensionPaths instance
    """
    legacy = Path.home() / ".visa-mcp" / "extensions"
    future = Path.home() / ".lab-executor" / "extensions"
    return ExtensionPaths(
        current_default=legacy,
        future_default_candidate=future,
        active_read_paths=[legacy],
        migration_required=False,
    )
