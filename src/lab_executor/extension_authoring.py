"""
v1.7: Definition Pack Authoring Assistant / Scaffolding

合言葉: 「**良い definition pack を作りやすくする**」

v1.2〜v1.6 で「定義→install→check→package→catalog/install」までは
揃った。v1.7 では入口側、つまり **新規 contributor (または将来の自分)
が空のディレクトリから安全な pack を作れる**ようにする。

提供 API:

- `init_extension_pack(pack_name, *, target_dir, extension_id, template,
  author, force)` → `InitResult`
- `package_dry_run(extension_yaml, *, strict)` → dict
  (file 一覧 / 除外 / manifest preview / checksums preview。zip は作らない)
- `doctor_extension(extension_yaml, *, strict)` → `DoctorReport`
  (validate / strict / package dry-run / catalog / README / license /
  registry 整合性をまとめて出し、`recommended_actions` を返す)

MCP tool 追加ゼロ、CLI 経由のみ。
"""
from __future__ import annotations
import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from lab_executor.extension import validate_extension_file
from lab_executor.extension_packaging import (
    _EXCLUDE_DIR_NAMES, _EXCLUDE_FILE_NAMES, _EXCLUDE_FILE_SUFFIXES,
    PACKAGE_FORMAT, PACKAGE_FORMAT_VERSION, PACKAGE_SUFFIX,
)

logger = logging.getLogger(__name__)


# ============================================================
# init: scaffold a new pack
# ============================================================


@dataclass
class InitResult:
    status: str = "error"  # ok / error
    pack_path: str = ""
    extension_id: str = ""
    files_created: list[str] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "pack_path": self.pack_path,
            "extension_id": self.extension_id,
            "files_created": list(self.files_created),
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


TEMPLATES = ("minimal", "mock_basic", "instrument_pack")


# template content は同梱 (Jinja2 依存を避け、str.replace で済ます)

_MINIMAL_EXTENSION_YAML = """\
extension_id: {extension_id}
name: {name}
version: 0.1.0
type: definition_pack
visa_mcp_compatibility: ">=1.7,<2.0"
author: "{author}"
license: "MIT"

catalog:
  summary: "Short summary of this definition pack."
  description: >
    TODO: longer description.
  authors:
    - {{ name: "{author}" }}
  license: "MIT"
  homepage: ""
  tags: []
  categories:
    - instrument-definitions
  target_users:
    - developers
  safety_notes:
    - "Review all instrument definitions before using on real hardware."

contents:
  instruments: []
  benchmarks: []
  templates: []
  mock_scenarios: []
  registry_entries: []

stability:
  support_level: draft
  executable_code: false
"""

_INSTRUMENT_PACK_EXTENSION_YAML = """\
extension_id: {extension_id}
name: {name}
version: 0.1.0
type: definition_pack
visa_mcp_compatibility: ">=1.7,<2.0"
author: "{author}"
license: "MIT"

catalog:
  summary: "Instrument definitions pack (TODO: describe scope)."
  authors:
    - {{ name: "{author}" }}
  license: "MIT"
  tags:
    - instruments
  categories:
    - instrument-definitions
  safety_notes:
    - "Review all instrument definitions before using on real hardware."

contents:
  instruments:
    - instruments/example_instrument.yaml
  registry_entries:
    - registry_entries/INDEX.yaml

stability:
  support_level: draft
  executable_code: false
"""

_MOCK_BASIC_EXTENSION_YAML = """\
extension_id: {extension_id}
name: {name}
version: 0.1.0
type: definition_pack
visa_mcp_compatibility: ">=1.7,<2.0"
author: "{author}"
license: "MIT"

catalog:
  summary: "Mock pack scaffold (no real hardware)."
  authors:
    - {{ name: "{author}" }}
  license: "MIT"
  tags:
    - mock
  categories:
    - instrument-definitions
    - benchmarks
  safety_notes:
    - "Mock instruments only. No real hardware I/O."

contents:
  instruments: []

stability:
  support_level: draft
  executable_code: false
"""

_README_MD = """\
# {name}

`{extension_id}` — TODO short description.

## What is in this pack

- TODO: list instruments / benchmarks / templates

## support_level

`draft` — set higher level once verified per
`docs/extension_publishing_checklist.md`.

## Authoring

```bash
visa-mcp validate extension extension.yaml
visa-mcp validate extension extension.yaml --strict
visa-mcp extension doctor extension.yaml --strict
visa-mcp extension package extension.yaml --dry-run
visa-mcp extension package extension.yaml --strict
visa-mcp extension verify-package dist/{extension_id}-0.1.0{suffix}
```

## Safety

TODO: list safety notes specific to this pack.
"""


_INSTR_README = """\
# instruments/

Place instrument YAML files here. Each file describes one instrument
(manufacturer, model, commands, safety, verify, etc.).

Run `visa-mcp validate instrument <file>` to validate a single file.
"""

_BENCH_README = """\
# benchmarks/

Place AI agent benchmark task YAML files here.

Run `visa-mcp validate benchmark <file>` per file.
"""

_TPL_README = """\
# templates/

Place ExperimentPlan templates here (DSL `dsl_version=0.8`).

Run `visa-mcp validate plan <file>` per file.
"""

_REG_INDEX_YAML = """\
# registry_entries / INDEX.yaml
# Each instrument in this pack can be exposed to the overlay registry
# by listing it here. id must be globally unique across builtin + other
# installed packs.
instruments: []
"""

_EXAMPLE_INSTRUMENT_YAML = """\
metadata:
  manufacturer: "Acme"
  model: "Example-1"
  description: "TODO: short instrument description."
  category: "dmm"
  support_level: "draft"
  tested_interfaces: []
commands: {}
"""


def init_extension_pack(
    pack_name: str,
    *,
    target_dir: str | Path | None = None,
    extension_id: str | None = None,
    template: str = "minimal",
    author: str = "",
    force: bool = False,
) -> InitResult:
    """空 directory に definition pack の scaffold を生成する。

    Args:
        pack_name: 生成するディレクトリ名 (= human display name の基準)
        target_dir: 親ディレクトリ (default: cwd)
        extension_id: reverse-DNS id (default: `local.<pack_name>`)
        template: "minimal" / "mock_basic" / "instrument_pack"
        author: catalog.authors / author に入れる名前
        force: 既存 directory があっても上書き

    生成された pack は、続けて `validate extension --strict` を
    すぐ通せる (draft で許容される範囲) ように catalog / safety_notes を
    最低限埋めている。
    """
    res = InitResult()
    if template not in TEMPLATES:
        res.errors.append({
            "error_class": "validation",
            "message": (
                f"unknown template: {template!r} "
                f"(choose from {list(TEMPLATES)})"
            ),
            "details": {"sub_class": "extension_init_unknown_template"},
        })
        return res

    base = Path(target_dir or ".").expanduser()
    pack_path = base / pack_name
    if pack_path.exists() and not force:
        res.errors.append({
            "error_class": "validation",
            "message": (
                f"directory already exists: {pack_path} "
                "(use --force to overwrite)"
            ),
            "details": {
                "sub_class": "extension_init_target_exists",
                "path": str(pack_path),
            },
        })
        return res

    ext_id = extension_id or f"local.{pack_name.lower().replace('_', '-')}"
    # 軽い validation (reverse-DNS-ish)
    import re
    if not re.match(r"^[a-z0-9]+(?:[.\-_][a-z0-9]+)*$", ext_id):
        res.errors.append({
            "error_class": "validation",
            "message": (
                f"extension_id={ext_id!r} は reverse-DNS style "
                "(小文字英数 + . / - / _) が必要"
            ),
            "details": {"sub_class": "extension_init_invalid_id"},
        })
        return res

    # v1.7.1 P1: --force は既存 directory への上書きを許すが、
    # 既存 file (template が生成しない補助 file 等) は **残す**。
    # 完全クリーンが必要な場合は手動で rmtree してから init すること。
    if pack_path.exists() and force:
        retained = [
            str(p.relative_to(pack_path)).replace("\\", "/")
            for p in pack_path.rglob("*") if p.is_file()
        ]
        if retained:
            res.warnings.append({
                "warning_class": "extension_init_force_retains_files",
                "message": (
                    f"--force は既存 file ({len(retained)} 件) を残し、"
                    "template が生成する file のみ上書きする。完全クリーン"
                    "が必要なら手動で directory を削除してから init して"
                    "ください"
                ),
                "details": {
                    "retained_files_count": len(retained),
                    "retained_files_sample": retained[:10],
                },
            })

    pack_path.mkdir(parents=True, exist_ok=True)

    yaml_tmpl = {
        "minimal": _MINIMAL_EXTENSION_YAML,
        "mock_basic": _MOCK_BASIC_EXTENSION_YAML,
        "instrument_pack": _INSTRUMENT_PACK_EXTENSION_YAML,
    }[template]
    ext_yaml = yaml_tmpl.format(
        extension_id=ext_id,
        name=pack_name,
        author=author or "TODO",
    )
    (pack_path / "extension.yaml").write_text(ext_yaml, encoding="utf-8")
    res.files_created.append("extension.yaml")

    readme = _README_MD.format(
        extension_id=ext_id, name=pack_name, suffix=PACKAGE_SUFFIX,
    )
    (pack_path / "README.md").write_text(readme, encoding="utf-8")
    res.files_created.append("README.md")

    if template == "instrument_pack":
        (pack_path / "instruments").mkdir(exist_ok=True)
        (pack_path / "instruments" / "README.md").write_text(
            _INSTR_README, encoding="utf-8")
        (pack_path / "instruments" / "example_instrument.yaml").write_text(
            _EXAMPLE_INSTRUMENT_YAML, encoding="utf-8")
        (pack_path / "registry_entries").mkdir(exist_ok=True)
        (pack_path / "registry_entries" / "INDEX.yaml").write_text(
            _REG_INDEX_YAML, encoding="utf-8")
        res.files_created.extend([
            "instruments/README.md",
            "instruments/example_instrument.yaml",
            "registry_entries/INDEX.yaml",
        ])
    elif template == "mock_basic":
        (pack_path / "instruments").mkdir(exist_ok=True)
        (pack_path / "instruments" / "README.md").write_text(
            _INSTR_README, encoding="utf-8")
        (pack_path / "benchmarks").mkdir(exist_ok=True)
        (pack_path / "benchmarks" / "README.md").write_text(
            _BENCH_README, encoding="utf-8")
        res.files_created.extend([
            "instruments/README.md", "benchmarks/README.md",
        ])
    # minimal: extension.yaml + README.md のみ

    res.pack_path = str(pack_path)
    res.extension_id = ext_id
    res.status = "ok"
    return res


# ============================================================
# package --dry-run
# ============================================================


def _should_exclude_packaging(rel: Path) -> bool:
    """extension_packaging._should_exclude と同じロジック (循環を避けるため
    定数のみ import して内製)"""
    parts = rel.parts
    if any(p in _EXCLUDE_DIR_NAMES for p in parts):
        return True
    if rel.name in _EXCLUDE_FILE_NAMES:
        return True
    if rel.suffix in _EXCLUDE_FILE_SUFFIXES:
        return True
    return False


def package_dry_run(
    extension_yaml_path: str | Path,
    *,
    strict: bool = False,
) -> dict[str, Any]:
    """zip を作らずに package 内容のプレビューを返す。

    含まれる予定 / 除外 file 一覧、package_manifest preview、
    checksums preview を返す。実 zip は作らない。
    """
    out: dict[str, Any] = {
        "status": "ok",
        "file": str(extension_yaml_path),
        "extension_id": "",
        "version": "",
        "package_name": "",
        "files_included": [],
        "files_excluded": [],
        "package_manifest_preview": None,
        "checksums_preview_count": 0,
        "errors": [],
        "warnings": [],
    }
    src = Path(extension_yaml_path).expanduser()
    if not src.exists():
        out["status"] = "error"
        out["errors"].append({
            "error_class": "not_found",
            "message": f"extension.yaml not found: {src}",
        })
        return out

    val_rep = validate_extension_file(src, strict=strict)
    if val_rep.errors:
        out["errors"].extend(val_rep.errors)
        out["status"] = "error"
        return out
    out["warnings"].extend(val_rep.warnings)

    manifest = val_rep.manifest or {}
    ext_id = manifest.get("extension_id", "")
    version = manifest.get("version", "")
    out["extension_id"] = ext_id
    out["version"] = version
    out["package_name"] = f"{ext_id}-{version}{PACKAGE_SUFFIX}"

    pack_dir = src.parent
    files_meta: list[dict[str, Any]] = []
    excluded: list[str] = []
    for f in sorted(pack_dir.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(pack_dir)
        rel_str = str(rel).replace("\\", "/")
        if _should_exclude_packaging(rel):
            excluded.append(rel_str)
            continue
        if rel_str in ("package_manifest.json", "checksums.sha256"):
            # package 側で常に上書き
            continue
        digest = hashlib.sha256(f.read_bytes()).hexdigest()
        files_meta.append({"path": rel_str, "sha256": digest})

    out["files_included"] = [fi["path"] for fi in files_meta]
    out["files_excluded"] = excluded
    out["checksums_preview_count"] = len(files_meta)
    out["package_manifest_preview"] = {
        "package_format": PACKAGE_FORMAT,
        "package_format_version": PACKAGE_FORMAT_VERSION,
        "extension_id": ext_id,
        "extension_version": version,
        "executable_code": False,
        "file_count": len(files_meta),
        "files": files_meta[:10],
        "files_truncated": len(files_meta) > 10,
    }
    if not files_meta:
        out["errors"].append({
            "error_class": "validation",
            "message": "package に含める file が無い",
            "details": {"sub_class": "empty_package"},
        })
        out["status"] = "error"
    return out


# ============================================================
# doctor
# ============================================================


@dataclass
class DoctorReport:
    status: str = "ok"  # ok / warning / error
    file: str = ""
    extension_id: str = ""
    summary: dict[str, Any] = field(default_factory=dict)
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    recommended_actions: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "file": self.file,
            "extension_id": self.extension_id,
            "summary": dict(self.summary),
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "recommended_actions": list(self.recommended_actions),
        }


def doctor_extension(
    extension_yaml_path: str | Path,
    *,
    strict: bool = False,
) -> DoctorReport:
    """authoring 中の pack に対して `validate` / `strict validate` /
    `package dry-run` / catalog / README / license / registry 整合性を
    まとめて出し、`recommended_actions` を返す。

    error がなく package も dry-run できれば `ready_to_package: true`、
    strict も通れば `ready_for_registry_review: true`。
    """
    rep = DoctorReport(file=str(extension_yaml_path))
    src = Path(extension_yaml_path).expanduser()
    if not src.exists():
        rep.status = "error"
        rep.errors.append({
            "error_class": "not_found",
            "message": f"extension.yaml not found: {src}",
        })
        return rep

    # 1. normal validate
    val_normal = validate_extension_file(src)
    for e in val_normal.errors:
        rep.errors.append({**e, "stage": "validate"})
    for w in val_normal.warnings:
        rep.warnings.append({**w, "stage": "validate"})

    manifest = val_normal.manifest or {}
    rep.extension_id = manifest.get("extension_id", "")

    # 2. strict validate (常に走らせて strict_* を doctor で可視化)
    val_strict = validate_extension_file(src, strict=True)
    strict_errors = [e for e in val_strict.errors
                     if e.get("error_class", "").startswith("strict_")]
    # strict-only errors は doctor では「strict 時に error 化される警告」
    # として表示 (--strict 指定時は本体 errors に格上げ)
    for e in strict_errors:
        if strict:
            rep.errors.append({**e, "stage": "strict_validate"})
        else:
            rep.warnings.append({
                **e,
                "warning_class": "strict_would_fail",
                "stage": "strict_validate",
            })

    # 3. package dry-run
    dry = package_dry_run(src, strict=False)
    package_ok = dry["status"] == "ok"
    if not package_ok:
        for e in dry.get("errors") or []:
            rep.errors.append({**e, "stage": "package_dry_run"})

    # 4. catalog / README / license check
    pack_dir = src.parent
    has_readme = (pack_dir / "README.md").exists()
    cat = manifest.get("catalog") or {}
    has_catalog_summary = bool(cat.get("summary"))
    has_catalog_license = bool(cat.get("license"))
    has_safety_notes = bool(cat.get("safety_notes"))

    # 5. instrument validation_evidence (verified なら必須)
    missing_evidence: list[str] = []
    # v1.9: 各 instrument に対して strict validate を集計
    from lab_executor.registry import validate_instrument_file
    instr_strict_passed = 0
    instr_strict_failed = 0
    missing_verify_commands = 0
    missing_safe_shutdown_instr = 0
    manual_ref_todo_instr = 0
    instr_total = 0
    for rel in (manifest.get("contents") or {}).get("instruments") or []:
        ip = pack_dir / rel
        if not ip.exists():
            continue
        instr_total += 1
        try:
            idata = yaml.safe_load(ip.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        md = idata.get("metadata") or {}
        if md.get("support_level") == "verified" and not (
                md.get("validation_evidence") or {}):
            missing_evidence.append(rel)
        # strict validate
        try:
            inst_rep = validate_instrument_file(ip, strict=True)
        except Exception:
            inst_rep = None
        if inst_rep is None:
            continue
        if inst_rep.errors:
            instr_strict_failed += 1
            for e in inst_rep.errors:
                ec = e.get("error_class", "")
                if ec == "instrument_missing_verify":
                    missing_verify_commands += 1
                elif ec == "instrument_missing_safe_shutdown":
                    missing_safe_shutdown_instr += 1
                elif ec == "instrument_manual_ref_todo":
                    manual_ref_todo_instr += 1
        else:
            instr_strict_passed += 1

    # 6. recommended_actions
    if not has_readme:
        rep.recommended_actions.append({
            "action": "add_readme",
            "reason": "README.md improves package discoverability",
            "command": f"echo '# {rep.extension_id}' > "
                       f"{pack_dir / 'README.md'}",
        })
    if not has_catalog_summary:
        rep.recommended_actions.append({
            "action": "add_catalog_summary",
            "reason": "catalog.summary is shown in extension catalog "
                       "/ inspect-package outputs",
        })
    if not has_catalog_license:
        rep.recommended_actions.append({
            "action": "add_catalog_license",
            "reason": "catalog.license clarifies redistribution terms",
        })
    if not has_safety_notes:
        rep.recommended_actions.append({
            "action": "add_safety_notes",
            "reason": "catalog.safety_notes alerts users about real-hw "
                       "concerns",
        })
    for rel in missing_evidence:
        rep.recommended_actions.append({
            "action": "add_validation_evidence",
            "reason": (
                f"{rel}: support_level=verified but "
                "metadata.validation_evidence is empty"
            ),
            "details": {"path": rel},
        })

    # 7. summary
    has_strict_problems = (
        bool(strict_errors)
        or not has_readme
        or not has_catalog_summary
        or not has_catalog_license
        or bool(missing_evidence)
    )
    rep.summary = {
        "errors": len(rep.errors),
        "warnings": len(rep.warnings),
        "has_readme": has_readme,
        "has_catalog_summary": has_catalog_summary,
        "has_catalog_license": has_catalog_license,
        "has_safety_notes": has_safety_notes,
        "missing_validation_evidence_count": len(missing_evidence),
        "ready_to_package": package_ok and not rep.errors,
        "ready_for_registry_review": (
            package_ok and not rep.errors and not has_strict_problems
        ),
        # v1.9: instrument quality summary
        "instrument_quality": {
            "total": instr_total,
            "strict_passed": instr_strict_passed,
            "strict_failed": instr_strict_failed,
            "missing_verify_commands": missing_verify_commands,
            "missing_safe_shutdown_instruments": missing_safe_shutdown_instr,
            "manual_ref_todo_instruments": manual_ref_todo_instr,
        },
    }

    if rep.errors:
        rep.status = "error"
    elif rep.warnings:
        rep.status = "warning"
    return rep
