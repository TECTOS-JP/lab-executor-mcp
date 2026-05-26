"""v2.4.0: Extension install path resolver (dual-path source of truth).

v1.x からの互換のため、書き込み default は `~/.visa-mcp/extensions/` を
継続。v2.4.0 で `~/.lab-executor/extensions/` を読み取り候補に追加し、
duplicate `extension_id` を**自動解決しない** (報告のみ) ポリシーを
導入する (案 B: report_conflict_no_implicit_precedence)。

役割分担:

- **read**  : `active_read_paths` (new_path, legacy_path の順)
- **write** : `write_default` (legacy_path のまま、v2.5+ で切替判断)
- **display**: `current_default` / `future_default_candidate`

このモジュールは「どの path を読み書きするか」を 1 箇所で決め、
CLI / install / catalog / check が参照する source of truth。
"""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path


#: v2.4 で導入された duplicate 解決ポリシー識別子。
#: 「読み取り順序はあっても、duplicate 時は黙って片方を採用しない。
#: check / catalog で warning または --strict で error として報告する」
DUPLICATE_POLICY = "report_conflict_no_implicit_precedence"


@dataclass
class ExtensionPaths:
    """v2.4.0: install path 一覧と書き込み default。

    Attributes:
        current_default: 表示用の現 default install path。v2.4 では
            `legacy_path` と同じ (`~/.visa-mcp/extensions/`)
        future_default_candidate: v2.5+ で default 化を検討する候補
            (`new_path` と同じ、`~/.lab-executor/extensions/`)
        legacy_path: 既存互換の install path (`~/.visa-mcp/extensions/`)
        new_path: v2.4 で読み取り候補に追加した new path
            (`~/.lab-executor/extensions/`)
        write_default: install 時の書き込み先 default (v2.4 では
            `legacy_path` のまま)
        active_read_paths: catalog / check が読み込む path 一覧。
            v2.4 では `[new_path, legacy_path]` の順 (内部順序であり、
            duplicate 時は `DUPLICATE_POLICY` に従い**自動採用しない**)
        duplicate_policy: duplicate `extension_id` 検出時の解決方針。
            v2.4 では常に `"report_conflict_no_implicit_precedence"`
        migration_required: legacy/new 両方に install がある場合に
            True (情報提供のみ、自動移動は行わない)
    """
    current_default: Path
    future_default_candidate: Path
    legacy_path: Path
    new_path: Path
    write_default: Path
    active_read_paths: list[Path] = field(default_factory=list)
    duplicate_policy: str = DUPLICATE_POLICY
    migration_required: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "current_default": str(self.current_default),
            "future_default_candidate": str(
                self.future_default_candidate),
            "legacy_path": str(self.legacy_path),
            "new_path": str(self.new_path),
            "write_default": str(self.write_default),
            "active_read_paths": [
                str(p) for p in self.active_read_paths
            ],
            "duplicate_policy": self.duplicate_policy,
            "migration_required": self.migration_required,
            "schema_version": "v2.4",
        }


def get_extension_paths() -> ExtensionPaths:
    """v2.4.0: 現在の install path 構成を取得する公開 API (source of
    truth)。

    v2.4 では:

    - 読み取り: ``~/.lab-executor/extensions/`` (new) → ``~/.visa-mcp
      /extensions/`` (legacy) の dual-path
    - 書き込み default: ``~/.visa-mcp/extensions/`` のまま
    - duplicate `extension_id` 時: 自動解決せず、report のみ
      (`duplicate_policy = "report_conflict_no_implicit_precedence"`)

    v2.5+ で default 切替を判断する予定。

    Returns:
        ExtensionPaths instance
    """
    legacy = Path.home() / ".visa-mcp" / "extensions"
    new = Path.home() / ".lab-executor" / "extensions"
    return ExtensionPaths(
        current_default=legacy,
        future_default_candidate=new,
        legacy_path=legacy,
        new_path=new,
        write_default=legacy,
        active_read_paths=[new, legacy],
        duplicate_policy=DUPLICATE_POLICY,
        migration_required=False,
    )
