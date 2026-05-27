"""v2.8.0: Migration Log Inspection + Copied Pack Verification.

v2.7 で `~/.lab-executor/migration_logs/extension-copy-<stamp>.json`
に保存し始めた apply manifest を、CLI / API で **読む / 検証する**
段階。実 rollback / target 削除 / overwrite は v2.8 でも行わない。

提供 API:

- `list_extension_migration_logs()` — log ファイル一覧
- `load_extension_migration_log(path)` — 1 件 parse + schema 検査
- `verify_extension_migration_log(path)` — copied target が現在も
  存在し読めるかを確認
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
