"""v2.5.0 / v2.6.0: Extension Migration Plan + Copy Plan.

v2.4 で導入した dual-path discovery + duplicate detection に対し、
**実ファイルは変更せず**、現状と推奨 action を構造化して出す:

- legacy_only  : `~/.visa-mcp/extensions/` にのみ存在
- new_only     : `~/.lab-executor/extensions/` にのみ存在
- duplicate    : 両 path に同じ `extension_id` (案 B により自動採用なし)
- invalid      : metadata 不正 / YAML 不在

v2.5.0 では migration plan を、v2.6.0 では copy plan を追加する。
両方とも **plan のみ**。`--apply` / 実 copy / 実 move / 実 delete は
実装しない (v2.7+ で慎重に検討)。
"""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lab_executor.extension_paths import (
    ExtensionPaths,
    get_extension_paths,
)
from lab_executor.extension_discovery import (
    InstalledExtension,
    discover_installed_extensions,
)


@dataclass(frozen=True)
class ExtensionMigrationAction:
    """1 件の推奨 action。

    v2.5+ では action は **提案のみ**。v2.6 で `--copy-plan` を導入し
    copy candidate を出せるようになったが、`apply_available` は引き
    続き **常に False** で、実 copy / move / delete は行わない。
    controlled apply は v2.7+ で検討。
    """
    action: str
    extension_id: str | None
    severity: str  # "info" / "warning" / "error"
    locations: list[Path] = field(default_factory=list)
    recommendation: str = ""
    apply_available: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "extension_id": self.extension_id,
            "severity": self.severity,
            "locations": [str(p) for p in self.locations],
            "recommendation": self.recommendation,
            "apply_available": self.apply_available,
        }


@dataclass(frozen=True)
class ExtensionCopyCandidate:
    """v2.6.0: legacy_only から new path への copy 候補 1 件。

    `safe_to_copy=True` でも v2.6 では実 copy はしない。candidate は
    **将来 v2.7+ で apply される予定の reference** であり、本 release
    では情報提供のみ。
    """
    extension_id: str
    source: Path
    target: Path
    reason: str = "legacy_only -> new_path copy candidate"
    safe_to_copy: bool = True
    overwrite_required: bool = False  # v2.6 では常に False

    def to_dict(self) -> dict[str, Any]:
        return {
            "extension_id": self.extension_id,
            "source": str(self.source),
            "target": str(self.target),
            "reason": self.reason,
            "safe_to_copy": self.safe_to_copy,
            "overwrite_required": self.overwrite_required,
        }


@dataclass
class ExtensionCopyPlan:
    """v2.6.0: copy candidate 群と blocked 状態。

    - status="ready"   : candidate を提示する余地がある
    - status="empty"   : legacy_only がなく candidate もない
    - status="blocked" : duplicate / invalid / 既存 target などにより
                        copy plan 生成自体を止めた状態

    v2.6 では `apply_available=False` 固定。
    """
    status: str  # "ready" / "empty" / "blocked"
    candidates: list[ExtensionCopyCandidate] = field(default_factory=list)
    blocked_reasons: list[dict[str, Any]] = field(default_factory=list)
    apply_available: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "candidates": [c.to_dict() for c in self.candidates],
            "blocked_reasons": list(self.blocked_reasons),
            "apply_available": self.apply_available,
        }


@dataclass
class ExtensionMigrationPlan:
    """`plan_extension_migration()` の戻り値"""
    status: str  # "ok" / "warning" / "error"
    summary: dict[str, Any] = field(default_factory=dict)
    actions: list[ExtensionMigrationAction] = field(default_factory=list)
    paths: ExtensionPaths | None = None
    # v2.6.0: `copy_plan=True` で plan_extension_migration を呼ぶと
    # ここに ExtensionCopyPlan が入る。default は None。
    copy_plan: ExtensionCopyPlan | None = None

    def to_dict(self) -> dict[str, Any]:
        p = self.paths
        d = {
            "status": self.status,
            "legacy_path": str(p.legacy_path) if p else None,
            "new_path": str(p.new_path) if p else None,
            "write_default": str(p.write_default) if p else None,
            "active_read_paths": (
                [str(x) for x in p.active_read_paths] if p else []
            ),
            "duplicate_policy": p.duplicate_policy if p else None,
            "summary": dict(self.summary),
            "actions": [a.to_dict() for a in self.actions],
            "schema_version": "v2.6" if self.copy_plan else "v2.5",
        }
        if self.copy_plan is not None:
            d["copy_plan"] = self.copy_plan.to_dict()
        return d


def plan_extension_migration(
    *,
    paths: ExtensionPaths | None = None,
    copy_plan: bool = False,
) -> ExtensionMigrationPlan:
    """v2.5.0: dual-path 状態を分類し migration plan を返す。

    実ファイルは **変更しない**。

    分類:

    - legacy_only: legacy_path にのみ
    - new_only: new_path にのみ
    - duplicate: 両方 (`discovery.duplicates`)
    - invalid: metadata 不正 / YAML 不在 (`discovery.errors`/`warnings`)

    `migration_required`:

    - legacy_only > 0 → True (legacy から new への移行候補)
    - duplicate > 0 → True (解消が必要)
    - invalid > 0 → True (修正が必要)
    - new_only のみ → False
    """
    paths = paths or get_extension_paths()
    discovery = discover_installed_extensions(paths)

    legacy = paths.legacy_path
    new = paths.new_path

    legacy_only: list[InstalledExtension] = []
    new_only: list[InstalledExtension] = []
    for ext in discovery.extensions:
        # duplicates は別 bucket; ここでは extensions list を見るが、
        # discovery 仕様で duplicates の代表 1 件も extensions に
        # 入っているので、duplicates にもある id はスキップする
        if ext.extension_id in discovery.duplicates:
            continue
        if ext.source_path == legacy:
            legacy_only.append(ext)
        elif ext.source_path == new:
            new_only.append(ext)

    duplicates = discovery.duplicates
    invalid_metadata_count = sum(
        1 for e in discovery.errors
        if e.get("error_class") == "invalid_extension_metadata"
    )
    missing_yaml_count = sum(
        1 for w in discovery.warnings
        if w.get("warning_class") == "missing_extension_yaml"
    )
    invalid_count = invalid_metadata_count + missing_yaml_count

    actions: list[ExtensionMigrationAction] = []

    # error: duplicate (案 B により最優先で解消)
    for ext_id, entries in duplicates.items():
        actions.append(ExtensionMigrationAction(
            action="resolve_duplicate_extension_id",
            extension_id=ext_id,
            severity="error",
            locations=[e.path for e in entries],
            recommendation=(
                "Remove or rename one copy before migration. "
                "v2.4 policy: report_conflict_no_implicit_precedence."
            ),
            apply_available=False,
        ))

    # error: invalid metadata
    for err in discovery.errors:
        if err.get("error_class") != "invalid_extension_metadata":
            continue
        actions.append(ExtensionMigrationAction(
            action="fix_invalid_extension_metadata",
            extension_id=None,
            severity="error",
            locations=[Path(err.get("path", ""))],
            recommendation=(
                "Repair extension.yaml (missing extension_id, "
                "parse error, etc.) or remove the pack directory."
            ),
            apply_available=False,
        ))

    # warning: missing extension.yaml
    for warn in discovery.warnings:
        if warn.get("warning_class") != "missing_extension_yaml":
            continue
        actions.append(ExtensionMigrationAction(
            action="fix_missing_extension_yaml",
            extension_id=None,
            severity="warning",
            locations=[Path(warn.get("path", ""))],
            recommendation=(
                "Add extension.yaml to the pack directory, or "
                "remove the directory if it is not a valid pack."
            ),
            apply_available=False,
        ))

    # info: legacy_only → copy candidate (実行はしない)
    for ext in legacy_only:
        actions.append(ExtensionMigrationAction(
            action="candidate_copy_to_new_path",
            extension_id=ext.extension_id,
            severity="info",
            locations=[ext.path, new / ext.path.name],
            recommendation=(
                "Future v2.6+ may offer a copy command. v2.5 only "
                "reports candidates; legacy install stays in place."
            ),
            apply_available=False,
        ))

    # info: new_only (action 不要)
    # — no action emitted; reflected in summary only

    migration_required = bool(
        legacy_only or duplicates or invalid_count
    )

    summary = {
        "legacy_only": len(legacy_only),
        "new_only": len(new_only),
        "duplicates": len(duplicates),
        "invalid": invalid_count,
        # v2.5.1: invalid の内訳を分けて公開
        # (error vs warning の起点を明示)
        "invalid_metadata": invalid_metadata_count,
        "missing_extension_yaml": missing_yaml_count,
        "migration_required": migration_required,
    }

    if duplicates or any(e.get("error_class") ==
                          "invalid_extension_metadata"
                          for e in discovery.errors):
        status = "error"
    elif legacy_only or invalid_count or any(
        w.get("warning_class") == "missing_extension_yaml"
        for w in discovery.warnings
    ):
        status = "warning"
    else:
        status = "ok"

    cp: ExtensionCopyPlan | None = None
    if copy_plan:
        cp = _build_copy_plan(
            legacy_only=legacy_only,
            duplicates=duplicates,
            discovery_errors=discovery.errors,
            discovery_warnings=discovery.warnings,
            new_path=new,
        )
        # summary に copy-plan の集計を反映 (v2.6)
        summary["copy_candidates"] = len(cp.candidates)
        summary["copy_blocked"] = (cp.status == "blocked")

    return ExtensionMigrationPlan(
        status=status,
        summary=summary,
        actions=actions,
        paths=paths,
        copy_plan=cp,
    )


def _build_copy_plan(
    *,
    legacy_only: list[InstalledExtension],
    duplicates: dict[str, list[InstalledExtension]],
    discovery_errors: list[dict[str, Any]],
    discovery_warnings: list[dict[str, Any]],
    new_path: Path,
) -> ExtensionCopyPlan:
    """v2.6.0: legacy_only 群を copy candidate 化する。

    blocked になる条件 (実 ファイルは一切変更しない):

    - duplicate あり (case: 案 B に従い、まず解消が必要)
    - invalid_extension_metadata あり (case: 修正が必要)

    candidate ごとに skip する条件:

    - target (new_path/<name>) が既に存在する
    """
    blocked_reasons: list[dict[str, Any]] = []
    for ext_id, entries in duplicates.items():
        blocked_reasons.append({
            "reason_class": "duplicate_extension_id",
            "extension_id": ext_id,
            "locations": [str(e.path) for e in entries],
        })
    for err in discovery_errors:
        if err.get("error_class") == "invalid_extension_metadata":
            blocked_reasons.append({
                "reason_class": "invalid_extension_metadata",
                "path": err.get("path", ""),
            })

    if blocked_reasons:
        return ExtensionCopyPlan(
            status="blocked",
            candidates=[],
            blocked_reasons=blocked_reasons,
            apply_available=False,
        )

    candidates: list[ExtensionCopyCandidate] = []
    skipped: list[dict[str, Any]] = []
    for ext in legacy_only:
        target = new_path / ext.path.name
        if target.exists():
            skipped.append({
                "reason_class": "target_exists",
                "extension_id": ext.extension_id,
                "target": str(target),
            })
            continue
        candidates.append(ExtensionCopyCandidate(
            extension_id=ext.extension_id,
            source=ext.path,
            target=target,
            reason="legacy_only -> new_path copy candidate",
            safe_to_copy=True,
            overwrite_required=False,
        ))

    if skipped:
        # candidate 0 / skipped のみ → blocked 扱い (target conflict)
        if not candidates:
            return ExtensionCopyPlan(
                status="blocked",
                candidates=[],
                blocked_reasons=skipped,
                apply_available=False,
            )
        # candidate あり + 一部 skipped → blocked_reasons に詳細を残す
        # が status は ready (CI 用途で部分実行できないのは v2.7+ 議論)
        return ExtensionCopyPlan(
            status="ready",
            candidates=candidates,
            blocked_reasons=skipped,
            apply_available=False,
        )

    if not candidates:
        return ExtensionCopyPlan(
            status="empty",
            candidates=[],
            blocked_reasons=[],
            apply_available=False,
        )

    return ExtensionCopyPlan(
        status="ready",
        candidates=candidates,
        blocked_reasons=[],
        apply_available=False,
    )
