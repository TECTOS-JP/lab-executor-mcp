"""
v1.3: Local Definition Pack management (install / list / uninstall + overlay)

合言葉: 「**definition pack を「作れる」から「安全に導入できる」へ**」

- 実行 Python plugin は **未対応** (v1.x 内予定なし)
- リモート install は **未対応** (ローカル path からのみ)
- install 先: `~/.visa-mcp/extensions/<extension_id>/`
- lockfile: `~/.visa-mcp/extensions.lock.json`
- 整合性: 各 file の sha256 を metadata に保存
- duplicate: 同 id + 同 version は `--force` 必須

v1.3.1 強化点:
- force install を backup-rename 方式へ (既存喪失防止)
- staging copy で `.git/` `__pycache__/` `*.pyc` `.DS_Store` `*.tmp` を除外
- install source が extensions_dir 配下の場合は拒否
- overlay registry の registry_entries path traversal を拒否
- overlay registry entry の必須項目 (id/path) 不足を error 化

詳細仕様: `docs/extension_install.md`, `docs/extension_registry_overlay.md`
"""
from __future__ import annotations
import hashlib
import json
import logging
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from lab_executor.extension import validate_extension_file

logger = logging.getLogger(__name__)


# ============================================================
# Paths
# ============================================================


def default_extensions_dir() -> Path:
    return Path.home() / ".visa-mcp" / "extensions"


def default_lockfile_path() -> Path:
    return Path.home() / ".visa-mcp" / "extensions.lock.json"


# ============================================================
# Lockfile
# ============================================================


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _now_stamp() -> str:
    """backup directory 名用の compact timestamp (UTC、コロン無し)"""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# v1.3.1 P1-6: staging copy 除外ルール
_EXCLUDE_DIR_NAMES = {".git", "__pycache__", ".mypy_cache", ".pytest_cache",
                      ".idea", ".vscode", "node_modules"}
_EXCLUDE_FILE_SUFFIXES = {".pyc", ".pyo", ".tmp", ".swp"}
_EXCLUDE_FILE_NAMES = {".DS_Store", "Thumbs.db"}


def _should_exclude_path(rel: Path) -> bool:
    """staging copy 時に除外すべき相対 path か"""
    parts = rel.parts
    if any(part in _EXCLUDE_DIR_NAMES for part in parts):
        return True
    name = rel.name
    if name in _EXCLUDE_FILE_NAMES:
        return True
    if rel.suffix in _EXCLUDE_FILE_SUFFIXES:
        return True
    return False


def _is_path_inside(child: Path, parent: Path) -> bool:
    """child が parent 配下に収まるかを resolve 後で判定"""
    try:
        cp = child.resolve()
        pp = parent.resolve()
    except (OSError, RuntimeError):
        return False
    try:
        cp.relative_to(pp)
        return True
    except ValueError:
        return False


def _read_lockfile(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"installed_extensions": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("lockfile parse failed (%s); starting fresh", path)
        return {"installed_extensions": []}


def _write_lockfile(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)


# ============================================================
# Install / list / uninstall
# ============================================================


@dataclass
class InstallResult:
    status: str   # "ok" / "error"
    extension_id: str = ""
    version: str = ""
    install_path: str = ""
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "extension_id": self.extension_id,
            "version": self.version,
            "install_path": self.install_path,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "metadata": self.metadata,
        }


def install_definition_pack(
    extension_yaml_path: str | Path,
    *,
    force: bool = False,
    extensions_dir: Path | None = None,
    lockfile_path: Path | None = None,
) -> InstallResult:
    """v1.3.0: definition pack を local user 領域へ安全に install。

    Steps:
      1. extension.yaml を read + path 安全性検査 (validate_extension_file)
      2. duplicate (extension_id) を lockfile から確認
      3. extension.yaml の dir 全体を staging tmp にコピー
      4. validate (sub-files schema 通過確認)
      5. atomic rename で install_path へ
      6. sha256 metadata 保存
      7. lockfile を更新
    """
    extensions_dir = extensions_dir or default_extensions_dir()
    lockfile_path = lockfile_path or default_lockfile_path()
    result = InstallResult(status="error")

    src = Path(extension_yaml_path).expanduser()
    if not src.exists():
        result.errors.append({
            "error_class": "not_found",
            "message": f"extension.yaml not found: {src}",
        })
        return result

    # v1.3.1 P1-7: install 元が extensions_dir 配下にある場合は拒否
    # (force 時に source を自分で消す事故を防ぐ)
    try:
        src_resolved = src.resolve()
        ext_dir_resolved = extensions_dir.resolve()
        if ext_dir_resolved in src_resolved.parents:
            result.errors.append({
                "error_class": "validation",
                "message": (
                    f"install source path is inside extensions_dir "
                    f"({ext_dir_resolved}); refusing to re-install from "
                    f"managed location"
                ),
                "details": {
                    "sub_class": "extension_source_inside_extensions_dir",
                },
            })
            return result
    except (OSError, RuntimeError):
        # resolve に失敗するケース (壊れた symlink 等) は後段で拾う
        pass

    # 1+4. validate_extension_file (pack 全体 + path 安全)
    val_rep = validate_extension_file(src)
    if val_rep.errors:
        result.errors.extend(val_rep.errors)
        result.errors.append({
            "error_class": "validation",
            "message": "extension pack validation failed; install aborted",
            "details": {"sub_class": "extension_validation_failed"},
        })
        return result
    result.warnings.extend(val_rep.warnings)

    manifest = val_rep.manifest or {}
    ext_id = manifest.get("extension_id")
    version = manifest.get("version")
    if not ext_id or not version:
        result.errors.append({
            "error_class": "validation",
            "message": "manifest に extension_id / version が無い",
        })
        return result
    result.extension_id = ext_id
    result.version = version

    # 2. duplicate チェック
    lock = _read_lockfile(lockfile_path)
    existing = [e for e in lock.get("installed_extensions", [])
                if e.get("extension_id") == ext_id]
    if existing and not force:
        ex = existing[0]
        result.errors.append({
            "error_class": "validation",
            "message": (
                f"extension_id={ext_id!r} は既に install 済み "
                f"(version={ex.get('version')}). 上書きには --force が必要"
            ),
            "details": {
                "sub_class": "extension_duplicate_install",
                "existing_version": ex.get("version"),
                "new_version": version,
            },
        })
        return result

    # 3. staging copy (pack 内 file を tmp に copy、除外ルール適用)
    pack_src_dir = src.parent
    install_path = extensions_dir / ext_id
    extensions_dir.mkdir(parents=True, exist_ok=True)
    tmpdir = Path(tempfile.mkdtemp(
        prefix=f"visa-mcp-ext-{ext_id}-", dir=str(extensions_dir),
    ))
    backup_path: Path | None = None
    try:
        # pack 内 file をすべて copy (再帰)、ただし以下は除外
        for src_path in pack_src_dir.rglob("*"):
            if not src_path.is_file():
                continue
            rel = src_path.relative_to(pack_src_dir)
            if _should_exclude_path(rel):
                continue
            dst = tmpdir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_path, dst)

        # 5. backup-rename 方式 (v1.3.1 P1-2)
        #   - 既存 install_path を backup_path へ rename
        #   - tmpdir を install_path へ rename
        #   - 成功時 backup_path を削除
        #   - 失敗時 backup_path を install_path へ戻す
        if install_path.exists():
            backup_path = install_path.with_name(
                install_path.name + ".bak-" + _now_stamp(),
            )
            install_path.rename(backup_path)
        tmpdir.replace(install_path)
        if backup_path is not None and backup_path.exists():
            shutil.rmtree(backup_path, ignore_errors=True)
            backup_path = None
    except Exception as e:
        # rollback
        shutil.rmtree(tmpdir, ignore_errors=True)
        if backup_path is not None and backup_path.exists():
            try:
                if install_path.exists():
                    shutil.rmtree(install_path, ignore_errors=True)
                backup_path.rename(install_path)
            except Exception as rb_err:
                logger.error(
                    "rollback from backup failed: %s (backup=%s)",
                    rb_err, backup_path,
                )
        result.errors.append({
            "error_class": "internal",
            "message": f"staging copy failed: {e}",
        })
        return result

    # 6. sha256 metadata
    checksums: dict[str, str] = {}
    for f in install_path.rglob("*"):
        if f.is_file() and f.name != ".install_meta.json":
            rel = str(f.relative_to(install_path)).replace("\\", "/")
            checksums[rel] = hashlib.sha256(f.read_bytes()).hexdigest()

    meta = {
        "extension_id": ext_id,
        "version": version,
        "installed_at": _now_iso(),
        "source_path": str(src),
        "visa_mcp_version": _current_visa_mcp_version(),
        "checksums": checksums,
        "manifest": manifest,
        # v1.6: installed_from で install 元を構造化記録
        "installed_from": {
            "kind": "directory",
            "source_path": str(src),
        },
    }
    (install_path / ".install_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    result.metadata = meta
    result.install_path = str(install_path)

    # 7. lockfile 更新 (既存 entry を置換)
    new_entries = [
        e for e in lock.get("installed_extensions", [])
        if e.get("extension_id") != ext_id
    ]
    new_entries.append({
        "extension_id": ext_id,
        "version": version,
        "path": str(install_path),
        "installed_at": meta["installed_at"],
        "visa_mcp_version": meta["visa_mcp_version"],
    })
    _write_lockfile(lockfile_path, {"installed_extensions": new_entries})

    result.status = "ok"
    return result


def install_definition_pack_from_zip(
    zip_path: str | Path,
    *,
    force: bool = False,
    extensions_dir: Path | None = None,
    lockfile_path: Path | None = None,
    skip_verify: bool = False,
) -> InstallResult:
    """v1.6.0: definition pack zip (.visa-mcp-ext.zip) を install。

    Steps:
      1. verify_extension_package() を必ず通す (skip_verify は test 用のみ)
         - zip slip / 絶対 path / checksum / executable_code 検査
         - extension.yaml の再 validate も内部で行われる
      2. zip を **tmp directory に展開**
      3. extension.yaml が tmp 内に出てくることを確認
      4. tmp 内 extension.yaml を `install_definition_pack` に流す
         (既存の install フローを再利用、source_path は zip path を記録)

    v1.6 では **remote URL / 署名 / trust store は未対応**。
    `zip_path` は **local file path** のみ。
    """
    import tempfile
    import zipfile

    result = InstallResult(status="error")
    zp = Path(zip_path).expanduser()
    if not zp.exists():
        result.errors.append({
            "error_class": "not_found",
            "message": f"package zip not found: {zp}",
        })
        return result
    if not zp.is_file():
        result.errors.append({
            "error_class": "validation",
            "message": f"package zip path is not a file: {zp}",
            "details": {"sub_class": "extension_install_zip_invalid"},
        })
        return result

    # 1. verify-package
    if not skip_verify:
        from lab_executor.extension_packaging import verify_extension_package
        vrep = verify_extension_package(zp)
        if vrep.status == "error":
            # verify エラーを install エラーとして返却
            for e in vrep.errors:
                result.errors.append({
                    "error_class": e.get("error_class", "validation"),
                    "message": "(verify) " + str(e.get("message", "")),
                    "details": e.get("details") or {},
                })
            result.errors.append({
                "error_class": "validation",
                "message": "package verification failed; install aborted",
                "details": {"sub_class": "extension_install_zip_verify_failed"},
            })
            return result
        # warning は伝搬
        for w in vrep.warnings:
            result.warnings.append({
                "warning_class": w.get("warning_class", "warning"),
                "message": "(verify) " + str(w.get("message", "")),
                "details": w.get("details") or {},
            })

    # 2. tmp extract (v1.6.1 P1: size / count 上限を docs と合わせて確認)
    #    - max files: 5000 (definition pack には十分)
    #    - max uncompressed total: 200 MB
    #    - extension.yaml が zip root にあること必須 (nested root 非対応)
    #    安全 path のみ展開する (二重防御。verify-package で zip slip は
    #    既に弾いているが、skip_verify=True の場合に備える)
    _MAX_FILES = 5000
    _MAX_TOTAL_BYTES = 200 * 1024 * 1024
    tmpdir = Path(tempfile.mkdtemp(prefix="visa-mcp-zipinstall-"))
    try:
        try:
            with zipfile.ZipFile(zp, "r") as zf:
                # v1.6.1 P1: 全体 size / count を先に確認
                infos = [i for i in zf.infolist() if not i.is_dir()]
                if len(infos) > _MAX_FILES:
                    result.errors.append({
                        "error_class": "validation",
                        "message": (
                            f"zip 内 file 数が上限 {_MAX_FILES} 超 "
                            f"({len(infos)})"
                        ),
                        "details": {
                            "sub_class": "extension_install_zip_too_many_files",
                            "count": len(infos),
                            "limit": _MAX_FILES,
                        },
                    })
                    return result
                total = sum(i.file_size for i in infos)
                if total > _MAX_TOTAL_BYTES:
                    result.errors.append({
                        "error_class": "validation",
                        "message": (
                            f"zip 内 uncompressed size が上限 "
                            f"{_MAX_TOTAL_BYTES} 超 ({total} bytes)"
                        ),
                        "details": {
                            "sub_class":
                                "extension_install_zip_too_large",
                            "size": total,
                            "limit": _MAX_TOTAL_BYTES,
                        },
                    })
                    return result
                # v1.6.1 P1: extension.yaml は zip root にあること必須
                if "extension.yaml" not in zf.namelist():
                    result.errors.append({
                        "error_class": "not_found",
                        "message": (
                            "zip root に extension.yaml がない "
                            "(nested package root は未対応)"
                        ),
                        "details": {
                            "sub_class":
                                "extension_install_zip_no_root_manifest",
                        },
                    })
                    return result
                for name in zf.namelist():
                    if name.endswith("/"):
                        continue
                    # zip slip 二重 check
                    n = name.replace("\\", "/")
                    if (n.startswith("/") or any(p == ".." for p in n.split("/"))
                            or (len(n) >= 2 and n[1] == ":")):
                        result.errors.append({
                            "error_class": "package_zip_slip",
                            "message": (
                                f"zip member path 安全性違反: {name!r}"
                            ),
                            "details": {"path": name,
                                        "sub_class":
                                            "extension_install_zip_unsafe"},
                        })
                        return result
                    target = tmpdir / n
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(name) as src_f, open(target, "wb") as dst_f:
                        dst_f.write(src_f.read())
        except zipfile.BadZipFile as e:
            result.errors.append({
                "error_class": "package_invalid_zip",
                "message": f"zip として読めない: {e}",
            })
            return result

        # 3. extension.yaml が tmp 内にあるか
        manifest = tmpdir / "extension.yaml"
        if not manifest.exists():
            result.errors.append({
                "error_class": "not_found",
                "message": "zip 内に extension.yaml が無い",
                "details": {
                    "sub_class": "extension_install_zip_no_manifest",
                },
            })
            return result

        # 4. 既存の install_definition_pack に流す
        inner = install_definition_pack(
            manifest,
            force=force,
            extensions_dir=extensions_dir,
            lockfile_path=lockfile_path,
        )
        # source_path を zip 自身に書き換え (metadata 上の追跡性のため)
        if inner.status == "ok" and inner.metadata is not None:
            inner.metadata["source_path"] = str(zp)
            inner.metadata["source_format"] = "visa-mcp-extension-package"
            # v1.6: installed_from を package 由来として記録
            try:
                zip_sha = hashlib.sha256(zp.read_bytes()).hexdigest()
            except Exception:
                zip_sha = ""
            pkg_format_version = None
            # zip 内 package_manifest.json から format_version を拾う
            try:
                import zipfile as _zipfile
                with _zipfile.ZipFile(zp, "r") as _zf:
                    if "package_manifest.json" in _zf.namelist():
                        _pkg = json.loads(
                            _zf.read("package_manifest.json").decode("utf-8")
                        )
                        pkg_format_version = _pkg.get(
                            "package_format_version")
            except Exception:
                pass
            inner.metadata["installed_from"] = {
                "kind": "package",
                "package_path": str(zp),
                "package_sha256": zip_sha,
                "package_format_version": pkg_format_version,
            }
            # .install_meta.json も書き直す
            try:
                (Path(inner.install_path) / ".install_meta.json").write_text(
                    json.dumps(inner.metadata, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except Exception as e:
                logger.warning(
                    "could not update .install_meta.json source_path: %s",
                    e,
                )
        # warnings を継承
        if result.warnings:
            inner.warnings = list(result.warnings) + list(inner.warnings)
        return inner
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def list_installed_packs(
    *,
    extensions_dir: Path | None = None,
    lockfile_path: Path | None = None,
) -> list[dict[str, Any]]:
    """install 済み pack 一覧を返す (lockfile ベース)"""
    lockfile_path = lockfile_path or default_lockfile_path()
    lock = _read_lockfile(lockfile_path)
    return list(lock.get("installed_extensions", []))


def uninstall_definition_pack(
    extension_id: str,
    *,
    extensions_dir: Path | None = None,
    lockfile_path: Path | None = None,
) -> dict[str, Any]:
    """指定 extension_id の install を取り消す。返却: result dict"""
    extensions_dir = extensions_dir or default_extensions_dir()
    lockfile_path = lockfile_path or default_lockfile_path()
    lock = _read_lockfile(lockfile_path)
    entries = lock.get("installed_extensions", [])
    target = next((e for e in entries
                    if e.get("extension_id") == extension_id), None)
    if target is None:
        return {
            "status": "error",
            "errors": [{
                "error_class": "not_found",
                "message": f"extension_id={extension_id!r} は install されていません",
            }],
        }

    install_path = Path(target["path"])
    try:
        if install_path.exists():
            shutil.rmtree(install_path)
    except Exception as e:
        return {
            "status": "error",
            "errors": [{
                "error_class": "internal",
                "message": f"uninstall path 削除失敗: {e}",
            }],
        }

    remaining = [e for e in entries if e.get("extension_id") != extension_id]
    _write_lockfile(lockfile_path, {"installed_extensions": remaining})
    return {
        "status": "ok",
        "extension_id": extension_id,
        "removed_path": str(install_path),
    }


# ============================================================
# Overlay registry
# ============================================================


@dataclass
class OverlayEntry:
    id: str
    vendor: str
    model: str
    category: str
    support_level: str
    path: str          # 絶対 path
    source: dict[str, Any]   # {"kind": "builtin"} or {"kind": "extension", ...}


@dataclass
class OverlayValidationReport:
    status: str = "ok"  # ok / warning / error
    entries: list[OverlayEntry] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "entries": [
                {
                    "id": e.id, "vendor": e.vendor, "model": e.model,
                    "category": e.category, "support_level": e.support_level,
                    "path": e.path, "source": e.source,
                }
                for e in self.entries
            ],
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "builtin_count": sum(
                1 for e in self.entries if e.source.get("kind") == "builtin"
            ),
            "extension_count": sum(
                1 for e in self.entries if e.source.get("kind") == "extension"
            ),
        }


def load_overlay_registry(
    builtin_index_path: str | Path | None,
    *,
    extensions_dir: Path | None = None,
    lockfile_path: Path | None = None,
) -> OverlayValidationReport:
    """built-in registry (INDEX.yaml) + installed extensions の
    registry_entries を **overlay** として統合し、duplicate id を error
    として検出する。

    優先順位 (v1.3 では duplicate を error にするだけで明示 override 無し):
      1. built-in registry IDと extension registry IDが衝突 → error
      2. extension 同士の id 衝突 → error
    """
    rep = OverlayValidationReport()

    # built-in
    if builtin_index_path is not None:
        bpath = Path(builtin_index_path)
        if bpath.exists():
            try:
                raw = yaml.safe_load(bpath.read_text(encoding="utf-8")) or {}
                for item in raw.get("instruments") or []:
                    rep.entries.append(OverlayEntry(
                        id=item.get("id", ""),
                        vendor=item.get("vendor", ""),
                        model=item.get("model", ""),
                        category=item.get("category", ""),
                        support_level=item.get("support_level", ""),
                        path=str((bpath.parent / item.get("path", "")).resolve()),
                        source={"kind": "builtin"},
                    ))
            except Exception as e:
                rep.errors.append({
                    "error_class": "schema_invalid",
                    "message": f"built-in INDEX.yaml parse failed: {e}",
                })

    # extensions
    for pack in list_installed_packs(
        extensions_dir=extensions_dir, lockfile_path=lockfile_path,
    ):
        ext_id = pack.get("extension_id", "")
        ext_ver = pack.get("version", "")
        pack_path = Path(pack["path"])
        # extension.yaml を読み、contents.registry_entries を解決
        manifest_path = pack_path / "extension.yaml"
        if not manifest_path.exists():
            rep.warnings.append({
                "warning_class": "extension_missing_manifest",
                "message": (
                    f"installed pack '{ext_id}' に extension.yaml が無い"
                ),
            })
            continue
        try:
            mf = yaml.safe_load(
                manifest_path.read_text(encoding="utf-8"),
            ) or {}
        except Exception:
            continue
        contents = (mf.get("contents") or {})
        for rel in (contents.get("registry_entries") or []):
            entry_file = pack_path / rel
            if not entry_file.exists():
                continue
            try:
                edata = yaml.safe_load(
                    entry_file.read_text(encoding="utf-8"),
                ) or {}
            except Exception:
                continue
            for item in edata.get("instruments") or []:
                source = {
                    "kind": "extension",
                    "extension_id": ext_id,
                    "extension_version": ext_ver,
                }
                item_id = item.get("id", "")
                item_path_raw = item.get("path", "")
                item_vendor = item.get("vendor", "")
                item_model = item.get("model", "")
                item_category = item.get("category", "")
                item_support = item.get("support_level", "")

                # v1.3.1 P1-4: 必須項目 (id / path) 不足を error 化
                if not item_id:
                    rep.errors.append({
                        "error_class": "validation",
                        "message": (
                            f"registry entry に id が無い "
                            f"(extension={ext_id}, file={rel})"
                        ),
                        "details": {
                            "sub_class": "registry_entry_missing_id",
                            "source": source,
                            "registry_entries_file": rel,
                        },
                    })
                    continue
                if not item_path_raw:
                    rep.errors.append({
                        "error_class": "validation",
                        "message": (
                            f"registry entry id={item_id!r} に path が無い "
                            f"(extension={ext_id})"
                        ),
                        "details": {
                            "sub_class": "registry_entry_missing_path",
                            "id": item_id,
                            "source": source,
                        },
                    })
                    continue

                # 補足: vendor / model / category / support_level の欠落は
                # warning (description 用途、衝突検出には不要)
                for field_name, val, w_class in (
                    ("vendor", item_vendor, "registry_entry_missing_vendor"),
                    ("model", item_model, "registry_entry_missing_model"),
                    ("category", item_category,
                     "registry_entry_missing_category"),
                    ("support_level", item_support,
                     "registry_entry_missing_support_level"),
                ):
                    if not val:
                        rep.warnings.append({
                            "warning_class": w_class,
                            "message": (
                                f"registry entry id={item_id!r}: "
                                f"{field_name} が空"
                            ),
                            "details": {"id": item_id, "source": source},
                        })

                # v1.3.1 P1-3: registry entry path が pack 外を指す場合は error
                resolved_entry = (pack_path / item_path_raw).resolve()
                if not _is_path_inside(resolved_entry, pack_path):
                    rep.errors.append({
                        "error_class": "validation",
                        "message": (
                            f"registry entry id={item_id!r} の path "
                            f"{item_path_raw!r} が pack ({ext_id}) 外を指している"
                        ),
                        "details": {
                            "sub_class": "registry_entry_path_outside_pack",
                            "id": item_id,
                            "path": item_path_raw,
                            "source": source,
                        },
                    })
                    continue

                rep.entries.append(OverlayEntry(
                    id=item_id,
                    vendor=item_vendor,
                    model=item_model,
                    category=item_category,
                    support_level=item_support,
                    path=str(resolved_entry),
                    source=source,
                ))

    # duplicate id 検出
    seen: dict[str, OverlayEntry] = {}
    for e in rep.entries:
        if not e.id:
            continue
        if e.id in seen:
            other = seen[e.id]
            rep.errors.append({
                "error_class": "validation",
                "message": (
                    f"overlay registry に duplicate id={e.id!r}: "
                    f"{other.source} と {e.source}"
                ),
                "details": {
                    "sub_class": "overlay_registry_duplicate_id",
                    "id": e.id,
                    "sources": [other.source, e.source],
                },
            })
        else:
            seen[e.id] = e

    if rep.errors:
        rep.status = "error"
    elif rep.warnings:
        rep.status = "warning"
    return rep


def _current_visa_mcp_version() -> str:
    try:
        from lab_visa_mcp import __version__
        return __version__
    except Exception:
        return "unknown"
