"""
v1.4: Installed Definition Pack Integrity

合言葉: 「**install できる**」から「**install したものを信頼して使い続けられる**」へ。

v1.3 で `~/.visa-mcp/extensions/<extension_id>/` 配下に
`.install_meta.json` (sha256 つき) を保存するようにした。v1.4 は
それを使った「drift 検出」「整合 inspect」「strict validation」を
CLI に追加する。

提供 API:

- `check_installed_extension(extension_id, *, strict=False)`
  → `IntegrityReport`
- `check_all_installed_extensions(*, strict=False)`
  → list[IntegrityReport]
- `inspect_installed_extension(extension_id)`
  → InspectReport
- `uninstall_dry_run(extension_id)`
  → dict (削除予定情報)

MCP tool ゼロ追加。CLI から呼ぶ前提。
"""
from __future__ import annotations
import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from lab_executor.extension import validate_extension_file
from lab_executor.extension_install import (
    default_extensions_dir, default_lockfile_path, _read_lockfile,
    list_installed_packs, load_overlay_registry,
)

logger = logging.getLogger(__name__)


# ============================================================
# Reports
# ============================================================


@dataclass
class IntegrityReport:
    """1 つの installed extension の整合性レポート"""
    extension_id: str = ""
    version: str = ""
    install_path: str = ""
    # ok / modified / missing_file / extra_file / invalid
    integrity: str = "ok"
    files_checked: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    recommended_actions: list[dict[str, Any]] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.errors:
            return "error"
        if self.warnings:
            return "warning"
        return "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "extension_id": self.extension_id,
            "version": self.version,
            "install_path": self.install_path,
            "integrity": self.integrity,
            "files_checked": self.files_checked,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "recommended_actions": list(self.recommended_actions),
        }


@dataclass
class InspectReport:
    """1 つの installed extension の詳細

    `integrity` は **軽量チェックの結果** (`.install_meta.json` /
    `extension.yaml` の存在のみを見る)。完全な sha256 drift 検査は
    `check_installed_extension()` (CLI: `visa-mcp extension check`) を
    使う。これを v1.4.1 から `integrity_check_level` / `full_check_tool`
    として JSON 上にも明示する。
    """
    extension_id: str = ""
    version: str = ""
    installed_at: str = ""
    source_path: str = ""
    visa_mcp_version: str = ""
    install_path: str = ""
    contents_summary: dict[str, int] = field(default_factory=dict)
    registry_entry_ids: list[str] = field(default_factory=list)
    integrity: str = "ok"
    # v1.4.1: integrity の検査レベル明示
    integrity_check_level: str = "light"
    warnings: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "extension_id": self.extension_id,
            "version": self.version,
            "installed_at": self.installed_at,
            "source_path": self.source_path,
            "visa_mcp_version": self.visa_mcp_version,
            "install_path": self.install_path,
            "contents_summary": dict(self.contents_summary),
            "registry_entry_ids": list(self.registry_entry_ids),
            "integrity": self.integrity,
            "integrity_check_level": self.integrity_check_level,
            "full_check_tool": (
                f"visa-mcp extension check {self.extension_id}"
                if self.extension_id else
                "visa-mcp extension check <extension_id>"
            ),
            "warnings": list(self.warnings),
        }


# ============================================================
# check
# ============================================================


def _sha256_of(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _read_install_meta(install_path: Path) -> dict[str, Any] | None:
    meta_file = install_path / ".install_meta.json"
    if not meta_file.exists():
        return None
    try:
        return json.loads(meta_file.read_text(encoding="utf-8"))
    except Exception:
        return None


def _reinstall_action(meta: dict[str, Any] | None) -> dict[str, Any]:
    source = (meta or {}).get("source_path") or "<original-extension.yaml>"
    return {
        "action": "reinstall",
        "command": f"visa-mcp extension install {source} --force",
    }


def _uninstall_action(extension_id: str) -> dict[str, Any]:
    return {
        "action": "uninstall",
        "command": f"visa-mcp extension uninstall {extension_id}",
    }


def check_installed_extension(
    extension_id: str,
    *,
    strict: bool = False,
    extensions_dir: Path | None = None,
    lockfile_path: Path | None = None,
) -> IntegrityReport:
    """指定 extension_id の install 状態 (整合性) を検査する。

    検査内容:
      1. lockfile に entry がある
      2. install path が存在する
      3. .install_meta.json が読める
      4. 記録された各 file が存在し sha256 一致 (drift 検出)
      5. metadata 外の余剰 file (extra_file) を warning
      6. extension.yaml を再 validate (path safety / executable_code / schema)
      7. strict 時は warning を error に格上げ + verified を厳しく扱う

    integrity 値:
      ok / modified / missing_file / extra_file / invalid
    """
    extensions_dir = extensions_dir or default_extensions_dir()
    lockfile_path = lockfile_path or default_lockfile_path()
    rep = IntegrityReport(extension_id=extension_id)

    lock = _read_lockfile(lockfile_path)
    entry = next(
        (e for e in lock.get("installed_extensions", [])
         if e.get("extension_id") == extension_id),
        None,
    )
    if entry is None:
        rep.integrity = "invalid"
        rep.errors.append({
            "error_class": "not_found",
            "message": (
                f"extension_id={extension_id!r} は install されていません"
            ),
        })
        return rep

    rep.version = entry.get("version", "")
    install_path = Path(entry.get("path") or extensions_dir / extension_id)
    rep.install_path = str(install_path)

    if not install_path.exists():
        rep.integrity = "missing_file"
        rep.errors.append({
            "error_class": "extension_install_path_missing",
            "message": (
                f"install path が存在しません: {install_path}"
            ),
        })
        rep.recommended_actions.extend([
            _reinstall_action(None),
            _uninstall_action(extension_id),
        ])
        return rep

    meta = _read_install_meta(install_path)
    if meta is None:
        rep.integrity = "invalid"
        rep.errors.append({
            "error_class": "extension_install_meta_missing",
            "message": (
                f".install_meta.json が無い / 読めません ({install_path})"
            ),
        })
        rep.recommended_actions.append(_reinstall_action(None))
        return rep

    # 4. checksum drift
    checksums = (meta.get("checksums") or {})
    recorded = set(checksums.keys())
    actual_files: set[str] = set()
    for f in install_path.rglob("*"):
        if not f.is_file():
            continue
        if f.name == ".install_meta.json":
            continue
        rel = str(f.relative_to(install_path)).replace("\\", "/")
        actual_files.add(rel)

    missing = sorted(recorded - actual_files)
    extras = sorted(actual_files - recorded)
    modified: list[str] = []
    for rel in sorted(recorded & actual_files):
        full = install_path / rel
        try:
            actual_hash = _sha256_of(full)
        except OSError as e:
            rep.errors.append({
                "error_class": "extension_checksum_unreadable",
                "message": f"{rel}: {e}",
                "details": {"path": rel},
            })
            modified.append(rel)
            continue
        if actual_hash != checksums[rel]:
            modified.append(rel)
            rep.errors.append({
                "error_class": "extension_checksum_mismatch",
                "message": f"{rel}: sha256 mismatch",
                "details": {
                    "path": rel,
                    "expected": checksums[rel],
                    "actual": actual_hash,
                },
            })
    rep.files_checked = len(recorded)

    if missing:
        for rel in missing:
            rep.errors.append({
                "error_class": "extension_file_missing",
                "message": f"記録された file {rel!r} が存在しません",
                "details": {"path": rel},
            })
    if extras:
        for rel in extras:
            # extra は warning (ユーザー追加 file の可能性)
            rep.warnings.append({
                "warning_class": "extension_extra_file",
                "message": (
                    f"metadata 外の file {rel!r} が install path に存在"
                ),
                "details": {"path": rel},
            })

    # integrity status 集約
    if missing:
        rep.integrity = "missing_file"
    elif modified:
        rep.integrity = "modified"
    elif extras and rep.integrity == "ok":
        rep.integrity = "extra_file"

    # 6. extension.yaml を再 validate
    manifest_path = install_path / "extension.yaml"
    if not manifest_path.exists():
        rep.integrity = "invalid"
        rep.errors.append({
            "error_class": "extension_manifest_missing",
            "message": "extension.yaml が install path に存在しません",
        })
    else:
        # v1.4.1 P1: strict を validate にも伝搬する。これにより
        # strict_support_level_draft / strict_verified_requires_evidence
        # 等も check --strict で拾える
        val_rep = validate_extension_file(manifest_path, strict=strict)
        for e in val_rep.errors:
            rep.errors.append({
                "error_class": e.get("error_class", "validation"),
                "message": "(revalidate) " + str(e.get("message", "")),
                "details": e.get("details") or {},
            })
        # validate の warning は extra_file と区別するため接頭辞をつける
        for w in val_rep.warnings:
            rep.warnings.append({
                "warning_class": w.get("warning_class", "warning"),
                "message": "(revalidate) " + str(w.get("message", "")),
                "details": w.get("details") or {},
            })
        if val_rep.errors and rep.integrity in ("ok", "extra_file"):
            rep.integrity = "invalid"

    # strict: warning を error に格上げ
    if strict and rep.warnings:
        # support_level / verified に関する強化 (instrument 側ではなく
        # manifest の stability を見る軽い確認に留める)
        for w in list(rep.warnings):
            rep.errors.append({
                "error_class": "strict_" + w.get("warning_class", "warning"),
                "message": "(strict) " + str(w.get("message", "")),
                "details": w.get("details") or {},
            })
        rep.warnings.clear()
        if rep.integrity == "ok":
            rep.integrity = "invalid"

    # recommended actions
    if rep.errors:
        rep.recommended_actions.append(_reinstall_action(meta))
        rep.recommended_actions.append(_uninstall_action(extension_id))

    return rep


def check_all_installed_extensions(
    *,
    strict: bool = False,
    extensions_dir: Path | None = None,
    lockfile_path: Path | None = None,
) -> list[IntegrityReport]:
    extensions_dir = extensions_dir or default_extensions_dir()
    lockfile_path = lockfile_path or default_lockfile_path()
    out: list[IntegrityReport] = []
    for pack in list_installed_packs(
        extensions_dir=extensions_dir, lockfile_path=lockfile_path,
    ):
        ext_id = pack.get("extension_id", "")
        if not ext_id:
            continue
        out.append(check_installed_extension(
            ext_id, strict=strict,
            extensions_dir=extensions_dir,
            lockfile_path=lockfile_path,
        ))
    return out


# ============================================================
# inspect
# ============================================================


def inspect_installed_extension(
    extension_id: str,
    *,
    extensions_dir: Path | None = None,
    lockfile_path: Path | None = None,
) -> InspectReport:
    extensions_dir = extensions_dir or default_extensions_dir()
    lockfile_path = lockfile_path or default_lockfile_path()
    rep = InspectReport(extension_id=extension_id)

    lock = _read_lockfile(lockfile_path)
    entry = next(
        (e for e in lock.get("installed_extensions", [])
         if e.get("extension_id") == extension_id),
        None,
    )
    if entry is None:
        rep.integrity = "invalid"
        rep.warnings.append({
            "warning_class": "not_found",
            "message": (
                f"extension_id={extension_id!r} は install されていません"
            ),
        })
        return rep

    rep.version = entry.get("version", "")
    install_path = Path(entry.get("path")
                         or extensions_dir / extension_id)
    rep.install_path = str(install_path)

    meta = _read_install_meta(install_path) or {}
    rep.installed_at = meta.get("installed_at", "")
    rep.source_path = meta.get("source_path", "")
    rep.visa_mcp_version = meta.get("visa_mcp_version", "")

    # contents summary を manifest から
    manifest_path = install_path / "extension.yaml"
    if manifest_path.exists():
        try:
            mf = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
            contents = (mf.get("contents") or {})
            for key in ("instruments", "benchmarks", "templates",
                         "system_configs", "registry_entries"):
                v = contents.get(key) or []
                rep.contents_summary[key] = len(v) if isinstance(v, list) else 0
            # registry_entries 内の id 一覧
            for rel in (contents.get("registry_entries") or []):
                entry_file = install_path / rel
                if not entry_file.exists():
                    continue
                try:
                    edata = yaml.safe_load(
                        entry_file.read_text(encoding="utf-8"),
                    ) or {}
                except Exception:
                    continue
                for item in edata.get("instruments") or []:
                    iid = item.get("id")
                    if iid:
                        rep.registry_entry_ids.append(iid)
        except Exception as e:
            rep.warnings.append({
                "warning_class": "extension_manifest_unreadable",
                "message": f"extension.yaml parse failed: {e}",
            })

    # integrity は軽量に: checksum check は別 CLI (check) で行う
    if not (install_path / ".install_meta.json").exists():
        rep.integrity = "invalid"
        rep.warnings.append({
            "warning_class": "extension_install_meta_missing",
            "message": ".install_meta.json 無し",
        })
    elif not manifest_path.exists():
        rep.integrity = "invalid"
        rep.warnings.append({
            "warning_class": "extension_manifest_missing",
            "message": "extension.yaml 無し",
        })

    return rep


# ============================================================
# uninstall dry-run
# ============================================================


def uninstall_dry_run(
    extension_id: str,
    *,
    extensions_dir: Path | None = None,
    lockfile_path: Path | None = None,
) -> dict[str, Any]:
    """uninstall せず「何が削除されるか」を返す"""
    extensions_dir = extensions_dir or default_extensions_dir()
    lockfile_path = lockfile_path or default_lockfile_path()

    lock = _read_lockfile(lockfile_path)
    entry = next(
        (e for e in lock.get("installed_extensions", [])
         if e.get("extension_id") == extension_id),
        None,
    )
    if entry is None:
        return {
            "status": "error",
            "errors": [{
                "error_class": "not_found",
                "message": (
                    f"extension_id={extension_id!r} は install されていません"
                ),
            }],
        }

    install_path = Path(entry.get("path") or extensions_dir / extension_id)

    # この pack が overlay に出していた id 一覧
    overlay_ids: list[str] = []
    try:
        rep = load_overlay_registry(
            None,
            extensions_dir=extensions_dir,
            lockfile_path=lockfile_path,
        )
        for e in rep.entries:
            if (e.source.get("kind") == "extension"
                    and e.source.get("extension_id") == extension_id):
                overlay_ids.append(e.id)
    except Exception:
        pass

    file_count = 0
    if install_path.exists():
        for f in install_path.rglob("*"):
            if f.is_file():
                file_count += 1

    return {
        "status": "ok",
        "dry_run": True,
        "extension_id": extension_id,
        "would_remove_path": str(install_path),
        "would_remove_file_count": file_count,
        "would_remove_lockfile_entry": True,
        "would_remove_overlay_ids": overlay_ids,
    }
