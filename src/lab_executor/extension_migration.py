"""v2.5.0 / v2.6.0 / v2.7.0: Extension Migration Plan / Copy Plan /
Controlled Apply.

v2.4 で導入した dual-path discovery + duplicate detection に対し、
段階的に migration を計画 → copy 候補 → controlled apply へ進む:

- v2.5: `plan_extension_migration()` (現状分類)
- v2.6: `plan_extension_migration(copy_plan=True)` (copy 候補)
- v2.7: `apply_extension_copy_plan()` (実 copy。厳格な事前条件下)

v2.7 でも **delete / overwrite / move は行わない**。source は触らず、
target が既存なら必ず止める。
"""
from __future__ import annotations
import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
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


# ============================================================
# v2.7.0: Controlled Copy Apply
# ============================================================


class ExtensionCopyApplyError(Exception):
    """`apply_extension_copy_plan()` の事前条件違反。"""

    def __init__(self, error_class: str, message: str,
                  details: dict[str, Any] | None = None):
        super().__init__(message)
        self.error_class = error_class
        self.message = message
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_class": self.error_class,
            "message": self.message,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class ExtensionCopyApplyResult:
    """v2.7.0: `apply_extension_copy_plan()` の戻り値。

    - status: "ok" / "blocked" / "partial_failure"
    - copied / failed / skipped: copy 1 件ごとの記録 (dict)
    - manifest_path: `~/.lab-executor/migration_logs/...` に保存した
      manifest の path (status=blocked 時は None もあり)
    - delete_performed / overwrite_performed: v2.7 では **常に False**
    - blocked_reasons: status=blocked / partial_failure 時の理由
    """
    status: str
    copied: list[dict[str, Any]] = field(default_factory=list)
    failed: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    manifest_path: Path | None = None
    delete_performed: bool = False
    overwrite_performed: bool = False
    blocked_reasons: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "copied": list(self.copied),
            "failed": list(self.failed),
            "skipped": list(self.skipped),
            "manifest_path": (
                str(self.manifest_path)
                if self.manifest_path else None
            ),
            "delete_performed": self.delete_performed,
            "overwrite_performed": self.overwrite_performed,
            "blocked_reasons": list(self.blocked_reasons),
            "schema_version": "v2.7",
        }


def _migration_log_dir() -> Path:
    """manifest 保存先 (`~/.lab-executor/migration_logs/`)。

    `~/.lab-executor/` は v2.5+ の future_default_candidate と同じ
    namespace。manifest はこちらに置くのが自然。
    """
    return Path.home() / ".lab-executor" / "migration_logs"


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _count_files_and_bytes(p: Path) -> tuple[int, int]:
    n = 0
    b = 0
    for f in p.rglob("*"):
        if f.is_file():
            n += 1
            b += f.stat().st_size
    return n, b


def apply_extension_copy_plan(
    *,
    paths: ExtensionPaths | None = None,
    log_dir: Path | None = None,
) -> ExtensionCopyApplyResult:
    """v2.7.0: legacy_only extension を new path へ copy する
    controlled apply。

    安全方針:

    - 実行直前に **migration plan を再計算** (UI 表示後に filesystem
      が変わった可能性をケア)
    - `copy_plan.status == "ready"` かつ `blocked_reasons` が空である
      ことが必須 (v2.6.1 で予約した条件)
    - candidate ごとに **target.tmp-<stamp>/** に copy → atomic-ish
      rename。target が既に存在する場合は **絶対に上書きせず** skip
    - source は **削除しない**
    - manifest を `~/.lab-executor/migration_logs/extension-copy-
      <stamp>.json` に必ず保存 (失敗時も保存する)

    partial failure 時の挙動: fail-fast。途中失敗したら以降の
    candidate を実行せず、成功済みは残す。`status="partial_failure"`
    で返し、manifest にも記録。
    """
    paths = paths or get_extension_paths()
    log_dir = log_dir or _migration_log_dir()

    # 1) 直前再計算
    plan = plan_extension_migration(paths=paths, copy_plan=True)
    cp = plan.copy_plan
    assert cp is not None  # copy_plan=True で必ず非 None

    # 2) 事前条件チェック (blocked / not ready / blocked_reasons あり
    #    のいずれかなら apply 不可)
    blocked: list[dict[str, Any]] = []
    if cp.status != "ready":
        blocked.append({
            "reason_class": "copy_plan_not_ready",
            "copy_plan_status": cp.status,
        })
    if cp.blocked_reasons:
        # v2.6.1 で予約: blocked_reasons があれば apply 不可
        for r in cp.blocked_reasons:
            blocked.append({
                "reason_class": r.get("reason_class", "unknown"),
                "extension_id": r.get("extension_id"),
                "path": r.get("path"),
                "target": r.get("target"),
            })
    if not cp.candidates:
        blocked.append({
            "reason_class": "no_copy_candidates",
        })

    if blocked:
        manifest_path = _write_manifest(
            log_dir=log_dir,
            status="blocked",
            copied=[],
            failed=[],
            skipped=[],
            blocked_reasons=blocked,
            paths=paths,
        )
        return ExtensionCopyApplyResult(
            status="blocked",
            blocked_reasons=blocked,
            manifest_path=manifest_path,
        )

    # 3) 実 copy (candidate 順 / fail-fast)
    copied: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    paths.new_path.mkdir(parents=True, exist_ok=True)

    for cand in cp.candidates:
        target = cand.target
        source = cand.source
        # target 上書き防止 (再チェック)
        if target.exists():
            skipped.append({
                "reason_class": "target_exists_at_apply",
                "extension_id": cand.extension_id,
                "source": str(source),
                "target": str(target),
            })
            # fail-fast: 安全のため停止
            break
        # source 存在再確認
        if not source.exists():
            failed.append({
                "reason_class": "source_missing_at_apply",
                "extension_id": cand.extension_id,
                "source": str(source),
            })
            break

        # temp dir に copy → atomic-ish rename
        tmp = target.parent / f"{target.name}.tmp-{_now_stamp()}"
        try:
            shutil.copytree(source, tmp)
            tmp.rename(target)
        except Exception as e:
            # cleanup tmp if exists
            try:
                if tmp.exists():
                    shutil.rmtree(tmp, ignore_errors=True)
            except Exception:
                pass
            failed.append({
                "reason_class": "copy_failed",
                "extension_id": cand.extension_id,
                "source": str(source),
                "target": str(target),
                "error": str(e),
            })
            break

        n_files, n_bytes = _count_files_and_bytes(target)
        copied.append({
            "extension_id": cand.extension_id,
            "source": str(source),
            "target": str(target),
            "file_count": n_files,
            "bytes": n_bytes,
        })

    status = "ok"
    if failed:
        status = "partial_failure"
    elif skipped:
        status = "partial_failure"

    manifest_path = _write_manifest(
        log_dir=log_dir,
        status=status,
        copied=copied,
        failed=failed,
        skipped=skipped,
        blocked_reasons=[],
        paths=paths,
    )

    return ExtensionCopyApplyResult(
        status=status,
        copied=copied,
        failed=failed,
        skipped=skipped,
        manifest_path=manifest_path,
        delete_performed=False,
        overwrite_performed=False,
    )


def _write_manifest(
    *,
    log_dir: Path,
    status: str,
    copied: list[dict[str, Any]],
    failed: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
    blocked_reasons: list[dict[str, Any]],
    paths: ExtensionPaths,
) -> Path:
    """apply manifest を JSON で保存し、保存先 path を返す。"""
    log_dir.mkdir(parents=True, exist_ok=True)
    fname = f"extension-copy-{_now_stamp()}.json"
    fpath = log_dir / fname
    payload = {
        "schema_version": "v2.7",
        "operation": "extension_copy_apply",
        "created_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "source_default": str(paths.legacy_path),
        "target_default": str(paths.new_path),
        "status": status,
        "copied": copied,
        "failed": failed,
        "skipped": skipped,
        "blocked_reasons": blocked_reasons,
        "delete_performed": False,
        "overwrite_performed": False,
    }
    fpath.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return fpath
