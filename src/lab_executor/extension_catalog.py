"""
v1.6: Definition Pack Discovery / Catalog Metadata

合言葉: 「**package できる**」から「**どの pack を使うべきか判断できる**」へ。

提供 API:

- `support_level_summary(instrument_paths)`: pack 内 instrument の
  support_level 分布 (verified / tested / experimental / draft 件数)
- `quality_signals(manifest, pack_dir, *, package_verified=None)`:
  scoring せず、構造化された **品質シグナル** (has_readme,
  has_validation_evidence 等) を返す
- `catalog_entry(extension_id, version, ..., source)`:
  catalog 一覧 1 件分の dict を組み立てる
- `list_catalog_installed()` / `list_catalog_packages(dir)`:
  installed / dist directory を catalog として一覧化
- `inspect_package(zip_path)`: package zip を install **せずに**
  中身の catalog / contents / quality_signals を返す

v1.6 では **MCP tool 追加ゼロ**、CLI 経由でのみ呼ばれる。
"""
from __future__ import annotations
import json
import logging
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


SUPPORT_LEVELS = ("verified", "tested", "experimental", "draft")


def support_level_summary(
    pack_dir: Path, instrument_rels: list[str],
) -> dict[str, int]:
    """pack 内 instrument YAML の metadata.support_level を集計"""
    out = {sl: 0 for sl in SUPPORT_LEVELS}
    for rel in instrument_rels:
        p = pack_dir / rel
        if not p.exists():
            continue
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        sl = ((data.get("metadata") or {}).get("support_level") or "")
        if sl in out:
            out[sl] += 1
    return out


def quality_signals(
    manifest: dict[str, Any],
    pack_dir: Path,
    *,
    package_verified: bool | None = None,
    strict_validation_passed: bool | None = None,
) -> dict[str, Any]:
    """**scoring せず**、構造化された quality signals を返す。

    AI エージェントに数値 score を読ませず、boolean / count の signal
    を提示することで誤判断を予防する (提案 P4)。
    """
    contents = manifest.get("contents") or {}
    instr_rels = contents.get("instruments") or []
    sl_summary = support_level_summary(pack_dir, instr_rels)

    has_readme = (pack_dir / "README.md").exists()

    # validation_evidence: 1 つでも instrument に dict があれば has=True
    has_evidence = False
    for rel in instr_rels:
        p = pack_dir / rel
        if not p.exists():
            continue
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        ev = ((data.get("metadata") or {}).get("validation_evidence") or {})
        if ev:
            has_evidence = True
            break

    cat = manifest.get("catalog") or {}
    return {
        "has_readme": has_readme,
        "has_catalog_summary": bool(cat.get("summary")),
        "has_catalog_license": bool(cat.get("license")),
        "has_validation_evidence": has_evidence,
        "verified_instruments": sl_summary.get("verified", 0),
        "tested_instruments": sl_summary.get("tested", 0),
        "experimental_instruments": sl_summary.get("experimental", 0),
        "draft_instruments": sl_summary.get("draft", 0),
        "package_verified": package_verified,           # None=未検査
        # v1.6.1 P1: null の意味を明示する補助 field
        "package_verification_status": (
            "not_checked" if package_verified is None
            else ("verified" if package_verified else "failed")
        ),
        "strict_validation_passed": strict_validation_passed,
        "strict_validation_status": (
            "not_checked" if strict_validation_passed is None
            else ("passed" if strict_validation_passed else "failed")
        ),
    }


def _content_counts(manifest: dict[str, Any]) -> dict[str, int]:
    c = manifest.get("contents") or {}
    return {
        "instruments": len(c.get("instruments") or []),
        "benchmarks": len(c.get("benchmarks") or []),
        "templates": len(c.get("templates") or []),
        "mock_scenarios": len(c.get("mock_scenarios") or []),
        "registry_entries": len(c.get("registry_entries") or []),
    }


def _catalog_section(manifest: dict[str, Any]) -> dict[str, Any]:
    """manifest.catalog を JSON 安全な dict にして返す"""
    cat = manifest.get("catalog") or {}
    return {
        "summary": cat.get("summary", ""),
        "description": cat.get("description", ""),
        "authors": list(cat.get("authors") or []),
        "license": cat.get("license", ""),
        "homepage": cat.get("homepage", ""),
        "tags": list(cat.get("tags") or []),
        "categories": list(cat.get("categories") or []),
        "target_users": list(cat.get("target_users") or []),
        "safety_notes": list(cat.get("safety_notes") or []),
    }


# ============================================================
# catalog (installed)
# ============================================================


@dataclass
class CatalogReport:
    status: str = "ok"
    extensions: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "extensions": list(self.extensions),
            "count": len(self.extensions),
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


def _entry_from_installed(pack: dict[str, Any]) -> dict[str, Any] | None:
    """lockfile entry + install path から catalog entry を組み立てる"""
    install_path = Path(pack.get("path", ""))
    if not install_path.exists():
        return None
    manifest_path = install_path / "extension.yaml"
    if not manifest_path.exists():
        return None
    try:
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return None

    meta_path = install_path / ".install_meta.json"
    install_meta: dict[str, Any] = {}
    if meta_path.exists():
        try:
            install_meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            install_meta = {}

    instr_rels = (manifest.get("contents") or {}).get("instruments") or []
    sl_summary = support_level_summary(install_path, instr_rels)
    signals = quality_signals(manifest, install_path,
                               package_verified=None)
    return {
        "extension_id": manifest.get("extension_id", ""),
        "version": manifest.get("version", ""),
        "catalog": _catalog_section(manifest),
        "contents_summary": _content_counts(manifest),
        "support_level_summary": sl_summary,
        "stability": {
            "support_level": (manifest.get("stability") or {}).get(
                "support_level", ""),
            "executable_code": bool(
                (manifest.get("stability") or {}).get(
                    "executable_code", False)),
        },
        "quality_signals": signals,
        "source": {
            "kind": "installed",
            "path": str(install_path),
            "installed_at": install_meta.get("installed_at", ""),
            "installed_from": install_meta.get("installed_from") or {
                "kind": "directory",
                "source_path": install_meta.get("source_path", ""),
            },
        },
    }


def list_catalog_installed(
    *,
    extensions_dir: Path | None = None,
    lockfile_path: Path | None = None,
) -> CatalogReport:
    """installed pack を catalog 形式で一覧化"""
    from lab_executor.extension_install import list_installed_packs
    rep = CatalogReport()
    packs = list_installed_packs(
        extensions_dir=extensions_dir, lockfile_path=lockfile_path,
    )
    for p in packs:
        e = _entry_from_installed(p)
        if e is None:
            rep.warnings.append({
                "warning_class": "installed_pack_unreadable",
                "message": (
                    f"installed pack '{p.get('extension_id')}' を読めません"
                ),
            })
            continue
        rep.extensions.append(e)
    if rep.warnings and not rep.extensions:
        rep.status = "warning"
    return rep


def list_catalog_packages(dist_dir: str | Path) -> CatalogReport:
    """指定 directory 配下の `.visa-mcp-ext.zip` を catalog 形式で一覧化"""
    rep = CatalogReport()
    d = Path(dist_dir).expanduser()
    if not d.exists() or not d.is_dir():
        rep.errors.append({
            "error_class": "not_found",
            "message": f"directory not found: {d}",
        })
        rep.status = "error"
        return rep
    for zp in sorted(d.glob("*.visa-mcp-ext.zip")):
        ins = inspect_package(zp)
        if ins.get("status") == "ok":
            rep.extensions.append(ins["entry"])
        else:
            rep.warnings.append({
                "warning_class": "package_unreadable",
                "message": f"{zp.name}: {ins.get('errors')}",
            })
    return rep


# ============================================================
# inspect-package
# ============================================================


def inspect_package(zip_path: str | Path) -> dict[str, Any]:
    """zip package を install **せず** に中身を読み、catalog 形式の entry
    を返す。検査までは行わず、軽量の読み取りに留める。完全な整合性検査は
    `verify_extension_package()` (CLI: `extension verify-package`) を使う。
    """
    out: dict[str, Any] = {
        "status": "ok", "package_path": str(zip_path),
        "errors": [], "warnings": [],
    }
    zp = Path(zip_path).expanduser()
    if not zp.exists():
        out["status"] = "error"
        out["errors"].append({
            "error_class": "not_found",
            "message": f"package not found: {zp}",
        })
        return out

    try:
        zf = zipfile.ZipFile(zp, "r")
    except zipfile.BadZipFile as e:
        out["status"] = "error"
        out["errors"].append({
            "error_class": "package_invalid_zip",
            "message": f"zip として読めない: {e}",
        })
        return out

    try:
        all_names = list(zf.namelist())
        # v1.6.1 P1: 軽量 zip slip check。inspect は extract しないが
        # member 名を一次的にチェックして、unsafe member があれば warning
        # に出す (verify-package を必ず通すべきという誘導)。
        unsafe = []
        for n in all_names:
            if n.endswith("/"):
                continue
            nn = n.replace("\\", "/")
            if (nn.startswith("/") or any(p == ".." for p in nn.split("/"))
                    or (len(nn) >= 2 and nn[1] == ":")):
                unsafe.append(n)
        if unsafe:
            out["warnings"].append({
                "warning_class": "inspect_package_unsafe_member",
                "message": (
                    f"zip 内に safe でない member path が見つかった "
                    f"({len(unsafe)} 件). install 前に "
                    f"`extension verify-package` で詳細検査してください"
                ),
                "details": {"unsafe_members": unsafe[:5]},
            })
        names = set(all_names)
        if "extension.yaml" not in names:
            out["status"] = "error"
            out["errors"].append({
                "error_class": "package_missing_required_file",
                "message": "extension.yaml が zip 内に無い",
            })
            return out

        try:
            manifest = yaml.safe_load(
                zf.read("extension.yaml").decode("utf-8")
            ) or {}
        except Exception as e:
            out["status"] = "error"
            out["errors"].append({
                "error_class": "schema_invalid",
                "message": f"extension.yaml parse failed: {e}",
            })
            return out

        # package_manifest.json (任意)
        pkg_manifest: dict[str, Any] | None = None
        if "package_manifest.json" in names:
            try:
                pkg_manifest = json.loads(
                    zf.read("package_manifest.json").decode("utf-8")
                )
            except Exception:
                pkg_manifest = None
        else:
            out["warnings"].append({
                "warning_class": "package_missing_manifest",
                "message": (
                    "package_manifest.json が無い (v1.5 以前 / 手動 zip)"
                ),
            })

        # signals は zip を tmp 展開して計算するが、軽量化のため一部だけ
        # README は zip 内 entry の存在で判定
        has_readme = "README.md" in names

        # support_level_summary を zip 内 instrument file を読んで構築
        instr_rels = (manifest.get("contents") or {}).get(
            "instruments") or []
        sl = {sk: 0 for sk in SUPPORT_LEVELS}
        has_evidence = False
        for rel in instr_rels:
            if rel not in names:
                continue
            try:
                idata = yaml.safe_load(zf.read(rel).decode("utf-8")) or {}
            except Exception:
                continue
            md = (idata.get("metadata") or {})
            v = md.get("support_level", "")
            if v in sl:
                sl[v] += 1
            ev = md.get("validation_evidence") or {}
            if ev:
                has_evidence = True

        cat = manifest.get("catalog") or {}
        signals = {
            "has_readme": has_readme,
            "has_catalog_summary": bool(cat.get("summary")),
            "has_catalog_license": bool(cat.get("license")),
            "has_validation_evidence": has_evidence,
            "verified_instruments": sl["verified"],
            "tested_instruments": sl["tested"],
            "experimental_instruments": sl["experimental"],
            "draft_instruments": sl["draft"],
            "package_verified": None,
            "package_verification_status": "not_checked",
            "strict_validation_passed": None,
            "strict_validation_status": "not_checked",
        }

        entry = {
            "extension_id": manifest.get("extension_id", ""),
            "version": manifest.get("version", ""),
            "catalog": _catalog_section(manifest),
            "contents_summary": _content_counts(manifest),
            "support_level_summary": sl,
            "stability": {
                "support_level": (manifest.get("stability") or {}).get(
                    "support_level", ""),
                "executable_code": bool(
                    (manifest.get("stability") or {}).get(
                        "executable_code", False)),
            },
            "quality_signals": signals,
            "package_manifest": pkg_manifest,
            "source": {
                "kind": "package",
                "package_path": str(zp),
                "package_format": (
                    (pkg_manifest or {}).get("package_format")
                    if pkg_manifest else None
                ),
                "package_format_version": (
                    (pkg_manifest or {}).get("package_format_version")
                    if pkg_manifest else None
                ),
            },
        }
        out["entry"] = entry
        return out
    finally:
        zf.close()
