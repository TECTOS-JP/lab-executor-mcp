"""v2.8.0 / v2.9.0: Migration Log Inspection + Rollback/Cleanup Plan.

v2.7 で `~/.lab-executor/migration_logs/extension-copy-<stamp>.json`
に保存し始めた apply manifest を、CLI / API で **読む / 検証する /
戻す計画を出す / 整理する計画を出す** 段階。

実 rollback / target 削除 / legacy source 削除 / overwrite は **v2.9
でも一切行わない** (plan only)。

提供 API:

- v2.8: `list_extension_migration_logs()` / `load_extension_migration
  _log(path)` / `verify_extension_migration_log(path)`
- v2.9: `plan_extension_rollback_from_log(path)` / `plan_extension_
  cleanup_from_log(path)`
"""
from __future__ import annotations
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


SUPPORTED_MANIFEST_SCHEMAS = ("v2.7",)
SUPPORTED_OPERATIONS = ("extension_copy_apply",)


def default_migration_log_dir() -> Path:
    """v2.7 と同じ場所を見る"""
    return Path.home() / ".lab-executor" / "migration_logs"


def find_latest_extension_copy_manifest(
    *,
    log_dir: Path | None = None,
) -> Path | None:
    """v2.10.0: 最新の `extension_copy_apply` manifest path を返す。

    `operation == extension_copy_apply` のもののみ対象。timestamp 降順
    で最初に見つかった file を返す。見つからなければ None。
    """
    summaries = list_extension_migration_logs(log_dir=log_dir)
    for s in summaries:
        if s.operation == "extension_copy_apply":
            return s.manifest_path
    return None


# ============================================================
# Models
# ============================================================


@dataclass(frozen=True)
class MigrationLogSummary:
    """`migration-log list` で表示する 1 件分の要約"""
    manifest_path: Path
    created_at: str
    operation: str
    status: str
    copied_count: int
    failed_count: int
    skipped_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_path": str(self.manifest_path),
            "created_at": self.created_at,
            "operation": self.operation,
            "status": self.status,
            "copied_count": self.copied_count,
            "failed_count": self.failed_count,
            "skipped_count": self.skipped_count,
        }


@dataclass(frozen=True)
class ExtensionCopyApplyManifest:
    """v2.7 形式 apply manifest の parsed view"""
    schema_version: str
    operation: str
    created_at: str
    source_default: str
    target_default: str
    status: str
    copied: list[dict[str, Any]]
    failed: list[dict[str, Any]]
    skipped: list[dict[str, Any]]
    blocked_reasons: list[dict[str, Any]]
    delete_performed: bool
    overwrite_performed: bool
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "operation": self.operation,
            "created_at": self.created_at,
            "source_default": self.source_default,
            "target_default": self.target_default,
            "status": self.status,
            "copied": list(self.copied),
            "failed": list(self.failed),
            "skipped": list(self.skipped),
            "blocked_reasons": list(self.blocked_reasons),
            "delete_performed": self.delete_performed,
            "overwrite_performed": self.overwrite_performed,
        }


@dataclass
class MigrationLogVerificationResult:
    """`verify_extension_migration_log()` の戻り値"""
    status: str  # "ok" / "warning" / "error"
    manifest_path: Path
    checked: list[dict[str, Any]] = field(default_factory=list)
    failed: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "manifest_path": str(self.manifest_path),
            "checked": list(self.checked),
            "failed": list(self.failed),
            "warnings": list(self.warnings),
        }


# ============================================================
# list / load
# ============================================================


def list_extension_migration_logs(
    *,
    log_dir: Path | None = None,
) -> list[MigrationLogSummary]:
    """`~/.lab-executor/migration_logs/extension-copy-*.json` を
    timestamp 降順で列挙する。`operation == extension_copy_apply` の
    みを対象 (将来別 operation が増えても混在しないように)。"""
    log_dir = log_dir or default_migration_log_dir()
    if not log_dir.exists() or not log_dir.is_dir():
        return []
    out: list[MigrationLogSummary] = []
    for p in sorted(log_dir.glob("extension-copy-*.json"),
                    reverse=True):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("skip unreadable log %s: %s", p, e)
            continue
        if data.get("operation") not in SUPPORTED_OPERATIONS:
            continue
        out.append(MigrationLogSummary(
            manifest_path=p,
            created_at=str(data.get("created_at", "")),
            operation=str(data.get("operation", "")),
            status=str(data.get("status", "")),
            copied_count=len(data.get("copied") or []),
            failed_count=len(data.get("failed") or []),
            skipped_count=len(data.get("skipped") or []),
        ))
    return out


def load_extension_migration_log(
    path: Path | str,
) -> ExtensionCopyApplyManifest:
    """1 件の manifest を読んで schema を最低限 validate する。

    Raises:
        FileNotFoundError: manifest が無い
        ValueError: schema_version 非対応 / operation 非対応 / JSON
            parse 失敗
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"manifest not found: {p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        raise ValueError(
            f"manifest parse failed: {e}"
        ) from e
    sv = data.get("schema_version")
    if sv not in SUPPORTED_MANIFEST_SCHEMAS:
        raise ValueError(
            f"unsupported manifest schema_version={sv!r} "
            f"(supported: {SUPPORTED_MANIFEST_SCHEMAS})"
        )
    op = data.get("operation")
    if op not in SUPPORTED_OPERATIONS:
        raise ValueError(
            f"unsupported manifest operation={op!r} "
            f"(supported: {SUPPORTED_OPERATIONS})"
        )
    return ExtensionCopyApplyManifest(
        schema_version=str(sv),
        operation=str(op),
        created_at=str(data.get("created_at", "")),
        source_default=str(data.get("source_default", "")),
        target_default=str(data.get("target_default", "")),
        status=str(data.get("status", "")),
        copied=list(data.get("copied") or []),
        failed=list(data.get("failed") or []),
        skipped=list(data.get("skipped") or []),
        blocked_reasons=list(data.get("blocked_reasons") or []),
        delete_performed=bool(data.get("delete_performed", False)),
        overwrite_performed=bool(data.get("overwrite_performed", False)),
        raw=dict(data),
    )


# ============================================================
# verify
# ============================================================


def verify_extension_migration_log(
    path: Path | str,
) -> MigrationLogVerificationResult:
    """copy 済 pack が現在も target 側に存在し、metadata が一致するかを
    確認する。

    検出する error:

    - target_missing
    - target_extension_yaml_missing
    - extension_id_mismatch
    - manifest_schema_unsupported (load 段階で raise)
    - delete_performed_unexpected
    - overwrite_performed_unexpected

    warning:

    - source_missing (source は今後整理される可能性があるため warning)
    """
    p = Path(path)
    try:
        m = load_extension_migration_log(p)
    except FileNotFoundError as e:
        return MigrationLogVerificationResult(
            status="error",
            manifest_path=p,
            failed=[{
                "error_class": "manifest_not_found",
                "message": str(e),
            }],
        )
    except ValueError as e:
        return MigrationLogVerificationResult(
            status="error",
            manifest_path=p,
            failed=[{
                "error_class": "manifest_schema_unsupported",
                "message": str(e),
            }],
        )

    result = MigrationLogVerificationResult(
        status="ok",
        manifest_path=p,
    )

    # safety invariants
    if m.delete_performed:
        result.failed.append({
            "error_class": "delete_performed_unexpected",
            "message": (
                "manifest claims delete_performed=true; v2.7+ "
                "controlled apply must never delete"
            ),
        })
    if m.overwrite_performed:
        result.failed.append({
            "error_class": "overwrite_performed_unexpected",
            "message": (
                "manifest claims overwrite_performed=true; v2.7+ "
                "controlled apply must never overwrite"
            ),
        })

    # copied[] verification
    for entry in m.copied:
        ext_id = entry.get("extension_id")
        source = Path(entry.get("source", ""))
        target = Path(entry.get("target", ""))
        check: dict[str, Any] = {
            "extension_id": ext_id,
            "source": str(source),
            "target": str(target),
            "target_exists": False,
            "extension_yaml_readable": False,
            "extension_id_match": False,
            "source_exists": False,
        }
        # source: 参考情報。source_missing は warning
        check["source_exists"] = source.exists()
        if not source.exists():
            result.warnings.append({
                "warning_class": "source_missing",
                "extension_id": ext_id,
                "source": str(source),
            })

        if not target.exists():
            result.failed.append({
                "error_class": "target_missing",
                "extension_id": ext_id,
                "target": str(target),
            })
            result.checked.append(check)
            continue
        check["target_exists"] = True

        yaml_p = target / "extension.yaml"
        if not yaml_p.exists():
            result.failed.append({
                "error_class": "target_extension_yaml_missing",
                "extension_id": ext_id,
                "target": str(target),
            })
            result.checked.append(check)
            continue
        try:
            ydata = yaml.safe_load(
                yaml_p.read_text(encoding="utf-8")
            ) or {}
            check["extension_yaml_readable"] = True
        except Exception as e:
            result.failed.append({
                "error_class": "target_extension_yaml_unreadable",
                "extension_id": ext_id,
                "target": str(target),
                "error": str(e),
            })
            result.checked.append(check)
            continue

        actual_id = ydata.get("extension_id")
        if actual_id != ext_id:
            result.failed.append({
                "error_class": "extension_id_mismatch",
                "expected": ext_id,
                "actual": actual_id,
                "target": str(target),
            })
        else:
            check["extension_id_match"] = True

        result.checked.append(check)

    # status
    if result.failed:
        result.status = "error"
    elif result.warnings:
        result.status = "warning"
    else:
        result.status = "ok"
    return result


# ============================================================
# v2.9.0: Rollback Plan / Cleanup Plan (plan only, no file changes)
# ============================================================


@dataclass(frozen=True)
class ExtensionRollbackCandidate:
    """1 件の rollback 候補。v2.9 では `apply_available=False` 固定。"""
    extension_id: str
    target: Path
    legacy_source: Path | None
    target_exists: bool
    legacy_source_exists: bool
    safe_to_plan: bool
    reason: str = ""
    apply_available: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "extension_id": self.extension_id,
            "rollback_action": "remove_copied_target",
            "target": str(self.target),
            "legacy_source": (
                str(self.legacy_source) if self.legacy_source else None
            ),
            "target_exists": self.target_exists,
            "legacy_source_exists": self.legacy_source_exists,
            "safe_to_plan": self.safe_to_plan,
            "reason": self.reason,
            "apply_available": self.apply_available,
        }


@dataclass
class ExtensionRollbackPlan:
    """v2.9.0 / v2.10.0: `plan_extension_rollback_from_log()` の戻り値。

    v2.10 で `already_absent` を分離 (target が既に無いものは blocked
    ではなく「rollback 不要」として扱う)。
    """
    status: str  # "ok" / "warning" / "error"
    manifest_path: Path
    candidates: list[ExtensionRollbackCandidate] = field(
        default_factory=list)
    already_absent: list[dict[str, Any]] = field(default_factory=list)
    blocked_reasons: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    apply_available: bool = False  # v2.9+ で常に False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "operation": "extension_copy_rollback_plan",
            "manifest_path": str(self.manifest_path),
            "summary": {
                "rollback_candidates": len(self.candidates),
                "already_absent": len(self.already_absent),
                "blocked": len(self.blocked_reasons),
                "warnings": len(self.warnings),
            },
            "candidates": [c.to_dict() for c in self.candidates],
            "already_absent": list(self.already_absent),
            "blocked_reasons": list(self.blocked_reasons),
            "warnings": list(self.warnings),
            "apply_available": self.apply_available,
            "schema_version": "v2.10",
        }


@dataclass(frozen=True)
class ExtensionCleanupCandidate:
    """1 件の cleanup (legacy source 整理) 候補。"""
    extension_id: str
    legacy_source: Path
    copied_target: Path
    target_verified: bool
    legacy_source_exists: bool
    safe_to_plan: bool
    reason: str = ""
    apply_available: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "extension_id": self.extension_id,
            "cleanup_action": "remove_legacy_source_candidate",
            "legacy_source": str(self.legacy_source),
            "copied_target": str(self.copied_target),
            "target_verified": self.target_verified,
            "legacy_source_exists": self.legacy_source_exists,
            "safe_to_plan": self.safe_to_plan,
            "reason": self.reason,
            "apply_available": self.apply_available,
        }


@dataclass
class ExtensionCleanupPlan:
    """v2.9.0 / v2.10.0: `plan_extension_cleanup_from_log()` の戻り値。

    v2.10 で `legacy_source_missing` を分離 (v2.9 までは
    `already_cleaned_or_missing` warning にまとめていたが、実 cleanup
    がまだ無い段階では「already cleaned」と断定できないため、現状を
    そのまま `legacy_source_missing` リストとして報告する)。
    """
    status: str  # "ok" / "warning" / "error"
    manifest_path: Path
    candidates: list[ExtensionCleanupCandidate] = field(
        default_factory=list)
    legacy_source_missing: list[dict[str, Any]] = field(
        default_factory=list)
    blocked_reasons: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    apply_available: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "operation": "extension_cleanup_plan",
            "manifest_path": str(self.manifest_path),
            "summary": {
                "cleanup_candidates": len(self.candidates),
                "legacy_source_missing": len(self.legacy_source_missing),
                "blocked": len(self.blocked_reasons),
                "warnings": len(self.warnings),
            },
            "candidates": [c.to_dict() for c in self.candidates],
            "legacy_source_missing": list(self.legacy_source_missing),
            "blocked_reasons": list(self.blocked_reasons),
            "warnings": list(self.warnings),
            "apply_available": self.apply_available,
            "schema_version": "v2.10",
        }


def plan_extension_rollback_from_log(
    manifest_path: Path | str,
) -> ExtensionRollbackPlan:
    """v2.9.0: manifest を読み、rollback 候補を出す (plan only)。

    rollback 候補条件:

    - manifest が読める / schema 対応
    - operation == extension_copy_apply
    - copied[] に target がある
    - target が存在する
    - legacy source が存在する (戻す先がある)

    blocked にする条件:

    - target が無い (既に消えている)
    - legacy source が無い (戻す先が無い)
    - delete_performed / overwrite_performed が true (manifest 改ざん)
    """
    p = Path(manifest_path)
    plan = ExtensionRollbackPlan(status="ok", manifest_path=p)
    try:
        m = load_extension_migration_log(p)
    except FileNotFoundError as e:
        plan.status = "error"
        plan.blocked_reasons.append({
            "reason_class": "manifest_not_found",
            "message": str(e),
        })
        return plan
    except ValueError as e:
        plan.status = "error"
        plan.blocked_reasons.append({
            "reason_class": "manifest_schema_unsupported",
            "message": str(e),
        })
        return plan

    # manifest invariant
    if m.delete_performed:
        plan.status = "error"
        plan.blocked_reasons.append({
            "reason_class": "delete_performed_unexpected",
            "message": (
                "manifest claims delete_performed=true; rollback "
                "cannot proceed against tampered manifest"
            ),
        })
        return plan
    if m.overwrite_performed:
        plan.status = "error"
        plan.blocked_reasons.append({
            "reason_class": "overwrite_performed_unexpected",
            "message": (
                "manifest claims overwrite_performed=true; rollback "
                "cannot proceed against tampered manifest"
            ),
        })
        return plan

    for entry in m.copied:
        ext_id = entry.get("extension_id")
        target = Path(entry.get("target", ""))
        source = Path(entry.get("source", ""))
        target_exists = target.exists()
        source_exists = source.exists()

        if not target_exists:
            # v2.10: target が無い = rollback 不要 (already_absent)
            # blocked ではなく「対象外」として分類
            plan.already_absent.append({
                "extension_id": ext_id,
                "target": str(target),
                "reason": "target_missing",
                "message": (
                    "Copied target is already missing; rollback is "
                    "not needed for this entry."
                ),
            })
            continue

        if not source_exists:
            # 戻す先がない → blocked (target を勝手に消すと復元できない)
            plan.blocked_reasons.append({
                "extension_id": ext_id,
                "reason_class": "legacy_source_missing",
                "message": (
                    "Legacy source does not exist; refusing to "
                    "rollback (no fallback path)."
                ),
                "target": str(target),
                "legacy_source": str(source),
            })
            continue

        plan.candidates.append(ExtensionRollbackCandidate(
            extension_id=str(ext_id) if ext_id else "",
            target=target,
            legacy_source=source,
            target_exists=True,
            legacy_source_exists=True,
            safe_to_plan=True,
            reason="copied target exists; legacy source available",
        ))

    # plan-only reminder warning (informational only)
    plan.warnings.append({
        "warning_class": "rollback_is_plan_only",
        "message": (
            "No files were changed. v2.9+ only displays rollback "
            "candidates; --apply is not available."
        ),
    })

    # v2.10: 案 A — status は real problem だけで決める
    # plan-only warning や already_absent は status を warning に
    # しない (warnings に残すだけ)
    if plan.blocked_reasons:
        plan.status = "error" if not plan.candidates else "warning"
    else:
        plan.status = "ok"
    return plan


def plan_extension_cleanup_from_log(
    manifest_path: Path | str,
) -> ExtensionCleanupPlan:
    """v2.9.0: manifest と verify 結果を使い、legacy source の整理
    候補を出す (plan only)。

    cleanup 候補条件:

    - manifest が読める / schema 対応
    - verify_extension_migration_log の結果が ok または「source_missing
      のみ warning」
    - copied target が存在し、`extension.yaml` が読め、`extension_id`
      が一致 (verify ok)
    - legacy source が存在する

    cleanup 候補にしない条件:

    - target_missing / extension_id_mismatch /
      target_extension_yaml_missing /
      target_extension_yaml_unreadable
    - delete_performed_unexpected / overwrite_performed_unexpected
    - manifest_schema_unsupported

    source_missing の場合は、cleanup 不要 (already_cleaned_or_missing)
    として warning に記録、candidate にはしない。
    """
    p = Path(manifest_path)
    plan = ExtensionCleanupPlan(status="ok", manifest_path=p)

    try:
        m = load_extension_migration_log(p)
    except FileNotFoundError as e:
        plan.status = "error"
        plan.blocked_reasons.append({
            "reason_class": "manifest_not_found",
            "message": str(e),
        })
        return plan
    except ValueError as e:
        plan.status = "error"
        plan.blocked_reasons.append({
            "reason_class": "manifest_schema_unsupported",
            "message": str(e),
        })
        return plan

    if m.delete_performed:
        plan.status = "error"
        plan.blocked_reasons.append({
            "reason_class": "delete_performed_unexpected",
            "message": (
                "manifest claims delete_performed=true; refusing to "
                "produce cleanup plan against tampered manifest"
            ),
        })
        return plan
    if m.overwrite_performed:
        plan.status = "error"
        plan.blocked_reasons.append({
            "reason_class": "overwrite_performed_unexpected",
            "message": (
                "manifest claims overwrite_performed=true; refusing to "
                "produce cleanup plan against tampered manifest"
            ),
        })
        return plan

    # v2.10: verify_extension_migration_log() を内部で使い、entry
    # ごとに verify error/warning を cleanup-plan の blocked/warning へ
    # 変換する (verify 条件を一元化)
    verify_res = verify_extension_migration_log(p)

    # verify が出した entry 別 error/warning を extension_id → 詳細
    # マップにする
    failed_by_id: dict[str, list[dict[str, Any]]] = {}
    for f in verify_res.failed:
        eid = f.get("extension_id") or f.get("expected") or ""
        failed_by_id.setdefault(str(eid), []).append(f)
    # verify 自体のメタ error (manifest schema / delete_/overwrite_
    # unexpected) は cleanup-plan を全体 block
    overall_meta_errors = [
        f for f in verify_res.failed
        if f.get("error_class") in (
            "delete_performed_unexpected",
            "overwrite_performed_unexpected",
        )
    ]
    if overall_meta_errors:
        for f in overall_meta_errors:
            plan.blocked_reasons.append({
                "reason_class": f.get("error_class"),
                "message": f.get("message", ""),
            })
        plan.status = "error"
        return plan

    for entry in m.copied:
        ext_id = entry.get("extension_id") or ""
        target = Path(entry.get("target", ""))
        source = Path(entry.get("source", ""))

        # この entry に対する verify failed があるか
        entry_failed = failed_by_id.get(str(ext_id), [])
        # cleanup を妨げる error class
        BLOCKING = {
            "target_missing",
            "target_extension_yaml_missing",
            "target_extension_yaml_unreadable",
            "extension_id_mismatch",
        }
        blocking = [f for f in entry_failed
                    if f.get("error_class") in BLOCKING]
        if blocking:
            for f in blocking:
                plan.blocked_reasons.append({
                    "extension_id": ext_id,
                    "reason_class": f.get("error_class"),
                    "message": f.get("message", ""),
                })
            continue

        # ここまでで verify ok 相当 (target 健全)
        # v2.10: source missing は legacy_source_missing リストへ
        # (warning にまとめず、構造化して報告)
        if not source.exists():
            plan.legacy_source_missing.append({
                "extension_id": ext_id,
                "legacy_source": str(source),
                "copied_target": str(target),
                "message": (
                    "Legacy source is not present. v2.10 cannot tell "
                    "whether this is from a prior cleanup or an "
                    "unexpected absence; treat as informational."
                ),
            })
            continue

        plan.candidates.append(ExtensionCleanupCandidate(
            extension_id=str(ext_id) if ext_id else "",
            legacy_source=source,
            copied_target=target,
            target_verified=True,
            legacy_source_exists=True,
            safe_to_plan=True,
            reason="copied target verified; legacy source can be cleaned",
        ))

    # plan-only reminder warning (informational)
    plan.warnings.append({
        "warning_class": "cleanup_is_plan_only",
        "message": (
            "No files were changed. v2.9+ only displays cleanup "
            "candidates; --apply is not available."
        ),
    })

    # v2.10 案 A: status は real problem だけで決める
    if plan.blocked_reasons:
        plan.status = "error" if not plan.candidates else "warning"
    else:
        plan.status = "ok"
    return plan
