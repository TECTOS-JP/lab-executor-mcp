"""
v1.5: Definition Pack Packaging / Publishing Preparation

合言葉: 「**作れる / install できる / 整合できる**」から
「**配布可能な成果物としてまとめられる**」へ。

v1.5 は **packaging まで**。zip install / remote install / signature
には進まない (v1.6+ 候補)。

提供 API:

- `package_definition_pack(extension_yaml, *, output_dir, strict=False)`
  → `PackageResult`
- `verify_extension_package(zip_path)`
  → `VerifyResult`

package 形式:

```
<extension_id>-<version>.visa-mcp-ext.zip
├── extension.yaml
├── package_manifest.json       ← v1.5 新規 (package 自身の metadata)
├── checksums.sha256            ← v1.5 新規 (相対 path + sha256)
├── README.md                   (任意 / --strict で推奨)
├── instruments/
├── benchmarks/
├── templates/
├── registry_entries/
└── mock_scenarios/
```

`package_manifest.json` 例:

```json
{
  "package_format": "visa-mcp-extension-package",
  "package_format_version": "1.0",
  "extension_id": "tectos.mock.basic",
  "extension_version": "0.1.0",
  "created_at": "2026-05-23T12:00:00+00:00",
  "created_by": "visa-mcp 1.5.0",
  "executable_code": false,
  "files": [
    {"path": "extension.yaml", "sha256": "..."},
    ...
  ]
}
```
"""
from __future__ import annotations
import hashlib
import json
import logging
import tempfile
import shutil
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from lab_executor.extension import validate_extension_file

logger = logging.getLogger(__name__)


PACKAGE_FORMAT = "visa-mcp-extension-package"
PACKAGE_FORMAT_VERSION = "1.0"
PACKAGE_SUFFIX = ".visa-mcp-ext.zip"

# v1.5: package 内に持ち込まない file/dir (staging copy と同じ除外)
_EXCLUDE_DIR_NAMES = {".git", "__pycache__", ".mypy_cache", ".pytest_cache",
                      ".idea", ".vscode", "node_modules"}
_EXCLUDE_FILE_SUFFIXES = {".pyc", ".pyo", ".tmp", ".swp"}
_EXCLUDE_FILE_NAMES = {".DS_Store", "Thumbs.db"}
# package 自身が生成する制御 file (元 pack に転がっていても無視 + 上書き)
_RESERVED_NAMES = {"package_manifest.json", "checksums.sha256"}


def _should_exclude(rel: Path) -> bool:
    parts = rel.parts
    if any(p in _EXCLUDE_DIR_NAMES for p in parts):
        return True
    if rel.name in _EXCLUDE_FILE_NAMES:
        return True
    if rel.suffix in _EXCLUDE_FILE_SUFFIXES:
        return True
    return False


def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _sha256_file(p: Path) -> str:
    return _sha256_bytes(p.read_bytes())


def _current_version() -> str:
    try:
        from lab_visa_mcp import __version__
        return __version__
    except Exception:
        return "unknown"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ============================================================
# package
# ============================================================


@dataclass
class PackageResult:
    status: str = "error"  # ok / error
    extension_id: str = ""
    version: str = ""
    package_path: str = ""
    package_sha256: str = ""
    file_count: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    manifest: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "extension_id": self.extension_id,
            "version": self.version,
            "package_path": self.package_path,
            "package_sha256": self.package_sha256,
            "file_count": self.file_count,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "manifest": self.manifest,
        }


def package_definition_pack(
    extension_yaml_path: str | Path,
    *,
    output_dir: str | Path | None = None,
    strict: bool = False,
) -> PackageResult:
    """definition pack を **配布可能 zip** にまとめる。

    手順:
      1. extension.yaml を validate (`strict` を伝搬)
      2. pack directory 内の file を staging tmp に集約 (除外ルール適用)
      3. checksums.sha256 を生成
      4. package_manifest.json を生成
      5. zip 化 (deterministic な順序で書き出し)
      6. zip 自身の sha256 を計算して返却
    """
    result = PackageResult()
    src = Path(extension_yaml_path).expanduser()
    if not src.exists():
        result.errors.append({
            "error_class": "not_found",
            "message": f"extension.yaml not found: {src}",
        })
        return result

    # 1. validate
    val_rep = validate_extension_file(src, strict=strict)
    if val_rep.errors:
        result.errors.extend(val_rep.errors)
        result.errors.append({
            "error_class": "validation",
            "message": "extension validation failed; package aborted",
            "details": {"sub_class": "extension_validation_failed"},
        })
        return result
    result.warnings.extend(val_rep.warnings)

    manifest = val_rep.manifest or {}
    ext_id = manifest.get("extension_id", "")
    version = manifest.get("version", "")
    if not ext_id or not version:
        result.errors.append({
            "error_class": "validation",
            "message": "manifest に extension_id / version が無い",
        })
        return result
    result.extension_id = ext_id
    result.version = version

    pack_dir = src.parent
    # output_dir default = <pack_dir>/dist
    out_dir = Path(output_dir).expanduser() if output_dir else (
        pack_dir / "dist"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_name = f"{ext_id}-{version}{PACKAGE_SUFFIX}"
    zip_path = out_dir / zip_name

    # strict 追加チェック:
    # - support_level=verified の pack で README.md が無いと missing_pack_readme
    pack_readme = pack_dir / "README.md"
    if not pack_readme.exists():
        w = {
            "warning_class": "missing_pack_readme",
            "message": "definition pack に README.md がありません",
        }
        if strict:
            result.errors.append({
                "error_class": "strict_missing_pack_readme",
                "message": "(strict) " + w["message"],
            })
        else:
            result.warnings.append(w)

    # 2. staging copy
    tmpdir = Path(tempfile.mkdtemp(prefix=f"visa-mcp-pack-{ext_id}-"))
    try:
        # collect files
        collected: list[tuple[Path, str]] = []   # (src, rel)
        for f in pack_dir.rglob("*"):
            if not f.is_file():
                continue
            rel = f.relative_to(pack_dir)
            if _should_exclude(rel):
                continue
            # 元 pack に偶然同名 file があっても package 生成側で上書き
            if str(rel).replace("\\", "/") in _RESERVED_NAMES:
                continue
            collected.append((f, str(rel).replace("\\", "/")))

        if not collected:
            result.errors.append({
                "error_class": "validation",
                "message": "package に含める file が無い",
                "details": {"sub_class": "empty_package"},
            })
            return result

        # zip-slip / 絶対 path 防止 (rel は relative_to で取れているが二重 check)
        for _, rel in collected:
            p = Path(rel)
            if p.is_absolute() or any(part == ".." for part in p.parts):
                result.errors.append({
                    "error_class": "validation",
                    "message": (
                        f"package 内 path 安全性違反: {rel}"
                    ),
                    "details": {"sub_class": "package_path_unsafe"},
                })
                return result

        # 3. checksums.sha256
        # sorted deterministic
        collected.sort(key=lambda t: t[1])
        checksums_lines = []
        files_meta: list[dict[str, Any]] = []
        for full, rel in collected:
            digest = _sha256_file(full)
            checksums_lines.append(f"{digest}  {rel}")
            files_meta.append({"path": rel, "sha256": digest})
        checksums_text = "\n".join(checksums_lines) + "\n"
        checksums_sha = _sha256_bytes(checksums_text.encode("utf-8"))

        # 4. package_manifest.json
        pkg_manifest = {
            "package_format": PACKAGE_FORMAT,
            "package_format_version": PACKAGE_FORMAT_VERSION,
            "extension_id": ext_id,
            "extension_version": version,
            "created_at": _now_iso(),
            "created_by": f"visa-mcp {_current_version()}",
            "executable_code": False,   # v1.5 では恒に false
            "file_count": len(files_meta),
            "files": files_meta,
            "checksums_file": "checksums.sha256",
            "checksums_sha256": checksums_sha,
        }
        manifest_text = json.dumps(pkg_manifest, ensure_ascii=False, indent=2)

        # 5. zip 化
        if zip_path.exists():
            zip_path.unlink()
        tmp_zip = zip_path.with_suffix(zip_path.suffix + ".tmp")
        with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            # 順序: extension.yaml → package_manifest.json → checksums.sha256
            #       → 他 file (sorted)
            # extension.yaml が collected の中にある前提だが、
            # _RESERVED_NAMES と衝突する場合は除外済み
            for full, rel in collected:
                zf.writestr(rel, full.read_bytes())
            zf.writestr("package_manifest.json", manifest_text)
            zf.writestr("checksums.sha256", checksums_text)
        tmp_zip.replace(zip_path)

        # 6. zip 自身の sha256
        result.package_sha256 = _sha256_file(zip_path)
        result.package_path = str(zip_path)
        result.file_count = len(collected)
        result.manifest = pkg_manifest
        # strict 系で errors が積まれていた場合 (例: strict_missing_pack_readme)、
        # zip は作るが status は error として返す
        result.status = "error" if result.errors else "ok"
        return result
    except Exception as e:
        result.errors.append({
            "error_class": "internal",
            "message": f"packaging failed: {e}",
        })
        return result
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ============================================================
# verify
# ============================================================


@dataclass
class VerifyResult:
    status: str = "ok"  # ok / warning / error
    package_path: str = ""
    extension_id: str = ""
    version: str = ""
    file_count: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    manifest: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "package_path": self.package_path,
            "extension_id": self.extension_id,
            "version": self.version,
            "file_count": self.file_count,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "manifest": self.manifest,
        }


def _is_safe_zip_member(name: str) -> bool:
    """zip slip / absolute path / drive letter を拒否"""
    if not name:
        return False
    n = name.replace("\\", "/")
    if n.startswith("/"):
        return False
    # Windows drive letter
    if len(n) >= 2 and n[1] == ":":
        return False
    parts = n.split("/")
    if any(p == ".." for p in parts):
        return False
    return True


def verify_extension_package(zip_path: str | Path) -> VerifyResult:
    """package zip の整合性を検証する。

    検査:
      - zip として読める
      - すべての member が safe path (zip slip 拒否)
      - extension.yaml / package_manifest.json / checksums.sha256 が存在
      - package_manifest.json が schema-like (必須 keys)
      - executable_code=false
      - checksums.sha256 と zip 内 file の sha256 が一致
      - package_manifest.files の sha256 が zip 内 file と一致
      - extension.yaml を tmp 展開して validate_extension_file が通る
    """
    result = VerifyResult(package_path=str(zip_path))
    p = Path(zip_path).expanduser()
    if not p.exists():
        result.errors.append({
            "error_class": "not_found",
            "message": f"package not found: {p}",
        })
        result.status = "error"
        return result

    try:
        zf = zipfile.ZipFile(p, "r")
    except zipfile.BadZipFile as e:
        result.errors.append({
            "error_class": "package_invalid_zip",
            "message": f"zip として読めない: {e}",
        })
        result.status = "error"
        return result

    try:
        names = zf.namelist()
        # zip slip
        for name in names:
            if name.endswith("/"):
                continue  # directory entry
            if not _is_safe_zip_member(name):
                result.errors.append({
                    "error_class": "package_zip_slip",
                    "message": (
                        f"zip member path 安全性違反: {name!r}"
                    ),
                    "details": {"path": name},
                })
        if result.errors:
            result.status = "error"
            return result

        # 必須 member
        for required in ("extension.yaml", "package_manifest.json",
                          "checksums.sha256"):
            if required not in names:
                result.errors.append({
                    "error_class": "package_missing_required_file",
                    "message": f"必須 file が package に無い: {required}",
                    "details": {"path": required},
                })
        if result.errors:
            result.status = "error"
            return result

        # package_manifest.json
        try:
            pkg_manifest = json.loads(
                zf.read("package_manifest.json").decode("utf-8")
            )
        except Exception as e:
            result.errors.append({
                "error_class": "package_manifest_invalid",
                "message": f"package_manifest.json parse failed: {e}",
            })
            result.status = "error"
            return result
        result.manifest = pkg_manifest

        if pkg_manifest.get("package_format") != PACKAGE_FORMAT:
            result.errors.append({
                "error_class": "package_format_invalid",
                "message": (
                    f"package_format が {PACKAGE_FORMAT!r} ではない"
                ),
            })
        if pkg_manifest.get("executable_code") is True:
            result.errors.append({
                "error_class": "package_executable_code_true",
                "message": (
                    "package_manifest.executable_code=true は許可されない"
                ),
            })

        result.extension_id = pkg_manifest.get("extension_id", "")
        result.version = pkg_manifest.get("extension_version", "")

        # checksums.sha256 を読み、各 file の sha256 を再計算して照合
        cs_text = zf.read("checksums.sha256").decode("utf-8")
        cs_recorded: dict[str, str] = {}
        for line in cs_text.splitlines():
            line = line.strip()
            if not line:
                continue
            # "<sha>  <rel>"
            parts = line.split("  ", 1)
            if len(parts) != 2:
                continue
            sha, rel = parts
            cs_recorded[rel] = sha

        # zip 内 (excluded controls 以外) の実 sha256 を計算
        actual: dict[str, str] = {}
        for name in names:
            if name in ("package_manifest.json", "checksums.sha256"):
                continue
            if name.endswith("/"):
                continue
            actual[name] = _sha256_bytes(zf.read(name))

        # checksum entry vs actual
        missing_in_zip = sorted(set(cs_recorded) - set(actual))
        extra_in_zip = sorted(set(actual) - set(cs_recorded))
        modified: list[str] = []
        for rel in sorted(set(cs_recorded) & set(actual)):
            if cs_recorded[rel] != actual[rel]:
                modified.append(rel)
                result.errors.append({
                    "error_class": "package_checksum_mismatch",
                    "message": f"{rel}: sha256 mismatch (checksums.sha256)",
                    "details": {
                        "path": rel,
                        "expected": cs_recorded[rel],
                        "actual": actual[rel],
                    },
                })
        for rel in missing_in_zip:
            result.errors.append({
                "error_class": "package_file_missing",
                "message": (
                    f"checksums.sha256 に記録された file {rel!r} が "
                    "zip 内に無い"
                ),
                "details": {"path": rel},
            })
        for rel in extra_in_zip:
            result.warnings.append({
                "warning_class": "package_extra_file",
                "message": (
                    f"checksums.sha256 に無い file が zip 内に存在: {rel}"
                ),
                "details": {"path": rel},
            })

        # package_manifest.files の sha256 整合
        for fi in (pkg_manifest.get("files") or []):
            rel = fi.get("path", "")
            sha = fi.get("sha256", "")
            if rel and rel in actual and sha and actual[rel] != sha:
                result.errors.append({
                    "error_class": "package_manifest_sha_mismatch",
                    "message": (
                        f"package_manifest.files: {rel!r} の sha256 が "
                        "実 file と不一致"
                    ),
                    "details": {
                        "path": rel,
                        "expected": sha,
                        "actual": actual[rel],
                    },
                })

        result.file_count = len(actual)

        # extension.yaml validate (tmp 展開)
        with tempfile.TemporaryDirectory(prefix="visa-mcp-verify-") as td:
            tmp = Path(td)
            # safe path のみ抽出済みなので extractall は許容するが、
            # それでもメンバーごとに再チェック
            for name in names:
                if name.endswith("/"):
                    continue
                if not _is_safe_zip_member(name):
                    continue
                target = tmp / name
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(name) as src, open(target, "wb") as dst:
                    dst.write(src.read())
            val_rep = validate_extension_file(tmp / "extension.yaml")
            for e in val_rep.errors:
                result.errors.append({
                    "error_class": e.get("error_class", "validation"),
                    "message": "(verify) " + str(e.get("message", "")),
                    "details": e.get("details") or {},
                })
            for w in val_rep.warnings:
                result.warnings.append({
                    "warning_class": w.get("warning_class", "warning"),
                    "message": "(verify) " + str(w.get("message", "")),
                    "details": w.get("details") or {},
                })

        if result.errors:
            result.status = "error"
        elif result.warnings:
            result.status = "warning"
        else:
            result.status = "ok"
        return result
    finally:
        zf.close()
