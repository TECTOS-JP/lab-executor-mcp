"""
v1.2: ExtensionManifest schema + validator (experimental)

`extension.yaml` (definition pack manifest) を Pydantic で受け取り、
validate する。**executable Python plugin は v1.2 では未対応** であり、
`stability.executable_code: true` は validation error とする。

詳細: `docs/extension_policy.md` / `docs/definition_packs.md`
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator


SUPPORT_LEVELS = ("verified", "tested", "experimental", "draft")


class ExtensionCatalog(BaseModel):
    """v1.6: catalog metadata (任意)。pack の選定 / 比較に使う。

    `extension.yaml` の root `catalog:` field として記述する。schema
    レベルではすべて optional。`extension package --strict` で
    `summary` / `license` 空は warning。
    """
    summary: str = ""
    description: str = ""
    authors: list[dict[str, Any]] = Field(default_factory=list)
    license: str = ""
    homepage: str = ""
    tags: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    target_users: list[str] = Field(default_factory=list)
    safety_notes: list[str] = Field(default_factory=list)


class ExtensionContents(BaseModel):
    """definition pack の中身ファイル参照 (extension.yaml からの相対 path)。
    すべて optional だが、少なくとも 1 セクションが非空である必要がある。
    """
    instruments: list[str] = Field(default_factory=list)
    benchmarks: list[str] = Field(default_factory=list)
    templates: list[str] = Field(default_factory=list)
    mock_scenarios: list[str] = Field(default_factory=list)
    registry_entries: list[str] = Field(default_factory=list)


class ExtensionStability(BaseModel):
    support_level: str = "draft"
    # v1.2 では definition pack のみ。executable_code=True は禁止。
    executable_code: bool = False

    @field_validator("support_level")
    @classmethod
    def _support_level_known(cls, v: str) -> str:
        if v not in SUPPORT_LEVELS:
            raise ValueError(
                f"support_level={v!r} は {list(SUPPORT_LEVELS)} のいずれか必須"
            )
        return v

    @field_validator("executable_code")
    @classmethod
    def _no_executable_code(cls, v: bool) -> bool:
        if v is True:
            raise ValueError(
                "v1.2 では executable_code=true は禁止。definition pack は"
                "data-driven な YAML/JSON のみを含めてください "
                "(docs/extension_policy.md 参照)"
            )
        return v


_EXTENSION_ID_RE = re.compile(r"^[a-z0-9]+(?:[.\-_][a-z0-9]+)*$")
_SEMVER_RE = re.compile(
    r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z\-.]+)?$"
)


class ExtensionManifest(BaseModel):
    """v1.2: definition pack manifest (`extension.yaml`)"""

    extension_id: str
    name: str
    version: str
    type: Literal["definition_pack"] = "definition_pack"
    visa_mcp_compatibility: str = ">=1.2,<2.0"
    description: str = ""
    author: str = ""
    homepage: str = ""
    license: str = ""
    contents: ExtensionContents = Field(default_factory=ExtensionContents)
    stability: ExtensionStability = Field(default_factory=ExtensionStability)
    # v1.6: catalog metadata (任意)。pack の選定 / 比較用。
    catalog: ExtensionCatalog = Field(default_factory=ExtensionCatalog)

    @field_validator("extension_id")
    @classmethod
    def _extension_id_format(cls, v: str) -> str:
        if not _EXTENSION_ID_RE.match(v):
            raise ValueError(
                f"extension_id={v!r} は小文字英数 + '.' / '-' / '_' のみ "
                f"(reverse-DNS 推奨, e.g. 'tectos.mock.basic')"
            )
        return v

    @field_validator("version")
    @classmethod
    def _version_semver(cls, v: str) -> str:
        if not _SEMVER_RE.match(v):
            raise ValueError(f"version={v!r} は SemVer 形式が必要")
        return v


# ============================================================
# Validation report (CLI 用)
# ============================================================


@dataclass
class ExtensionValidationReport:
    file: str
    status: str = "ok"      # ok / warning / error
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    manifest: dict[str, Any] | None = None
    files_checked: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "file": self.file,
            "schema": "extension_manifest.schema.json",
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "manifest": self.manifest,
            "files_checked": self.files_checked,
        }


def validate_extension_file(
    path: str | Path,
    *,
    strict: bool = False,
) -> ExtensionValidationReport:
    """`extension.yaml` を読み、manifest + 参照ファイル群を検証する。

    v1.4: `strict=True` で normal 時の warning を error に格上げする
    (registry 掲載 / CI / release 前検査向け)。具体的には:
      - `empty_contents` warning → error
      - `registry_entries_format` warning → error
      - 参照 instrument 内の `support_level=draft` を error
      - support_level=verified なのに validation_evidence 無し → error
    """
    rep = ExtensionValidationReport(file=str(path))
    p = Path(path)
    if not p.exists():
        rep.status = "error"
        rep.errors.append({
            "error_class": "not_found",
            "message": f"extension.yaml not found: {p}",
        })
        return rep
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        manifest = ExtensionManifest(**raw)
    except Exception as e:
        rep.status = "error"
        rep.errors.append({
            "error_class": "schema_invalid",
            "message": f"extension manifest validation failed: {e}",
        })
        return rep

    rep.manifest = manifest.model_dump()

    # 中身が空でないこと
    c = manifest.contents
    if not any([c.instruments, c.benchmarks, c.templates,
                c.mock_scenarios, c.registry_entries]):
        rep.warnings.append({
            "warning_class": "empty_contents",
            "message": "contents の全セクションが空。少なくとも 1 ファイル参照を推奨",
        })

    # 参照ファイル存在 + 各 sub-validation
    base = p.parent
    files_checked = 0
    base_resolved = base.resolve()

    def _ref(rel: str) -> Path:
        return base / rel

    def _check_path_safety(rel: str, field: str) -> bool:
        """v1.2.1: contents.* path が extension.yaml 配下のみであることを
        強制する (definition pack 外部参照は contribution の安全性に直結)
        """
        candidate = Path(rel)
        if candidate.is_absolute():
            rep.errors.append({
                "error_class": "validation",
                "message": (
                    f"{field}: 絶対パスは禁止 (extension.yaml 配下の "
                    f"相対パスのみ可): {rel}"
                ),
                "field_path": field,
                "details": {"sub_class": "extension_path_outside_pack"},
            })
            return False
        try:
            resolved = (base / candidate).resolve()
            resolved.relative_to(base_resolved)
        except (OSError, ValueError):
            rep.errors.append({
                "error_class": "validation",
                "message": (
                    f"{field}: extension.yaml 配下を逸脱するパス (.. による "
                    f"traversal): {rel}"
                ),
                "field_path": field,
                "details": {"sub_class": "extension_path_outside_pack"},
            })
            return False
        return True

    # instruments
    from lab_executor.registry import validate_instrument_file
    for rel in c.instruments:
        files_checked += 1
        if not _check_path_safety(rel, "contents.instruments"):
            continue
        full = _ref(rel)
        if not full.exists():
            rep.errors.append({
                "error_class": "not_found",
                "message": f"referenced instrument file missing: {rel}",
                "field_path": "contents.instruments",
            })
            continue
        sub = validate_instrument_file(full)
        if sub.errors:
            rep.errors.append({
                "error_class": "schema_invalid",
                "message": (
                    f"instrument validation failed: {rel} "
                    f"({len(sub.errors)} errors)"
                ),
                "field_path": "contents.instruments",
                "details": {"sub_errors": sub.errors},
            })

    # benchmarks
    from lab_executor.testing.benchmark_task import load_benchmark_task
    for rel in c.benchmarks:
        files_checked += 1
        if not _check_path_safety(rel, "contents.benchmarks"):
            continue
        full = _ref(rel)
        if not full.exists():
            rep.errors.append({
                "error_class": "not_found",
                "message": f"referenced benchmark file missing: {rel}",
                "field_path": "contents.benchmarks",
            })
            continue
        try:
            load_benchmark_task(full)
        except Exception as e:
            rep.errors.append({
                "error_class": "schema_invalid",
                "message": f"benchmark validation failed: {rel}: {e}",
                "field_path": "contents.benchmarks",
            })

    # templates (DSL ExperimentPlan として)
    from lab_executor.registry import validate_plan_file
    for rel in c.templates:
        files_checked += 1
        if not _check_path_safety(rel, "contents.templates"):
            continue
        full = _ref(rel)
        if not full.exists():
            rep.errors.append({
                "error_class": "not_found",
                "message": f"referenced template file missing: {rel}",
                "field_path": "contents.templates",
            })
            continue
        sub = validate_plan_file(full)
        if sub.errors:
            rep.errors.append({
                "error_class": "schema_invalid",
                "message": f"template validation failed: {rel}",
                "field_path": "contents.templates",
                "details": {"sub_errors": sub.errors},
            })

    # mock_scenarios (YAML として parse できることのみ確認)
    for rel in c.mock_scenarios:
        files_checked += 1
        if not _check_path_safety(rel, "contents.mock_scenarios"):
            continue
        full = _ref(rel)
        if not full.exists():
            rep.errors.append({
                "error_class": "not_found",
                "message": f"referenced mock scenario file missing: {rel}",
                "field_path": "contents.mock_scenarios",
            })
            continue
        try:
            yaml.safe_load(full.read_text(encoding="utf-8"))
        except Exception as e:
            rep.errors.append({
                "error_class": "schema_invalid",
                "message": f"mock scenario parse failed: {rel}: {e}",
                "field_path": "contents.mock_scenarios",
            })

    # registry_entries (YAML として parse できること + entries 形式の最低限)
    for rel in c.registry_entries:
        files_checked += 1
        if not _check_path_safety(rel, "contents.registry_entries"):
            continue
        full = _ref(rel)
        if not full.exists():
            rep.errors.append({
                "error_class": "not_found",
                "message": f"referenced registry entries file missing: {rel}",
                "field_path": "contents.registry_entries",
            })
            continue
        try:
            data = yaml.safe_load(full.read_text(encoding="utf-8")) or {}
            if not isinstance(data, dict) or "instruments" not in data:
                rep.warnings.append({
                    "warning_class": "registry_entries_format",
                    "message": (
                        f"{rel}: 'instruments' キーが見つかりません "
                        "(registry INDEX 部分相当を期待)"
                    ),
                })
        except Exception as e:
            rep.errors.append({
                "error_class": "schema_invalid",
                "message": f"registry entries parse failed: {rel}: {e}",
                "field_path": "contents.registry_entries",
            })

    rep.files_checked = files_checked

    # v1.6: catalog metadata の存在 (任意)。normal では warning、
    # strict では error。pack 選定 / registry 掲載で重要な field のみ。
    cat = manifest.catalog
    if not cat.summary:
        rep.warnings.append({
            "warning_class": "missing_catalog_summary",
            "message": "catalog.summary が空。pack 選定時に概要が表示されない",
            "field_path": "catalog.summary",
        })
    if not cat.license:
        rep.warnings.append({
            "warning_class": "missing_catalog_license",
            "message": "catalog.license が空 (root level の license も推奨)",
            "field_path": "catalog.license",
        })

    # v1.4: strict mode promotion
    if strict:
        # empty_contents / registry_entries_format / catalog warning → error
        promote_classes = {
            "empty_contents", "registry_entries_format",
            "missing_catalog_summary", "missing_catalog_license",
        }
        remaining_warnings: list[dict[str, Any]] = []
        for w in rep.warnings:
            if w.get("warning_class") in promote_classes:
                rep.errors.append({
                    "error_class": "strict_" + w["warning_class"],
                    "message": "(strict) " + str(w.get("message", "")),
                    "field_path": w.get("field_path", ""),
                })
            else:
                remaining_warnings.append(w)
        rep.warnings = remaining_warnings

        # instruments 参照: support_level=draft を strict で error 化、
        # support_level=verified なのに validation_evidence 無しを strict で error 化
        base = p.parent
        for rel in manifest.contents.instruments:
            full = base / rel
            if not full.exists():
                continue
            try:
                idata = yaml.safe_load(full.read_text(encoding="utf-8")) or {}
            except Exception:
                continue
            md = (idata.get("metadata") or {})
            sl = md.get("support_level", "")
            if sl == "draft":
                rep.errors.append({
                    "error_class": "strict_support_level_draft",
                    "message": (
                        f"(strict) instrument {rel!r} の "
                        f"metadata.support_level=draft は registry 掲載品質"
                        "ではありません"
                    ),
                    "field_path": "metadata.support_level",
                })
            if sl == "verified":
                ev = md.get("validation_evidence") or {}
                if not ev:
                    rep.errors.append({
                        "error_class": "strict_verified_requires_evidence",
                        "message": (
                            f"(strict) instrument {rel!r} は "
                            "support_level=verified だが "
                            "metadata.validation_evidence が空"
                        ),
                        "field_path": "metadata.validation_evidence",
                    })

        # v1.4.1 P1: registry_entries 内 entry の深掘り検査
        #   - id / path / vendor / model / category / support_level 必須
        #   - path が pack 外を指さない
        #   - support_level は SUPPORT_LEVELS のいずれか
        #   - 参照先 instrument YAML が pack 内にある場合、
        #     metadata.support_level と一致
        from lab_executor.registry import SUPPORT_LEVELS
        base_resolved2 = base.resolve()
        for rel in manifest.contents.registry_entries:
            full = base / rel
            if not full.exists():
                continue
            try:
                data = yaml.safe_load(full.read_text(encoding="utf-8")) or {}
            except Exception:
                continue
            for i, item in enumerate(data.get("instruments") or []):
                fp = f"contents.registry_entries[{rel}].instruments[{i}]"
                item_id = item.get("id", "")
                item_path = item.get("path", "")
                item_vendor = item.get("vendor", "")
                item_model = item.get("model", "")
                item_category = item.get("category", "")
                item_sl = item.get("support_level", "")
                if not item_id:
                    rep.errors.append({
                        "error_class": "strict_registry_entry_missing_id",
                        "message": f"(strict) {fp}: id が無い",
                        "field_path": fp + ".id",
                    })
                if not item_path:
                    rep.errors.append({
                        "error_class": "strict_registry_entry_missing_path",
                        "message": (
                            f"(strict) {fp} (id={item_id!r}): path が無い"
                        ),
                        "field_path": fp + ".path",
                    })
                for fname, fval in (
                    ("vendor", item_vendor),
                    ("model", item_model),
                    ("category", item_category),
                    ("support_level", item_sl),
                ):
                    if not fval:
                        rep.errors.append({
                            "error_class":
                                f"strict_registry_entry_missing_{fname}",
                            "message": (
                                f"(strict) {fp} (id={item_id!r}): "
                                f"{fname} が無い"
                            ),
                            "field_path": fp + f".{fname}",
                        })
                if item_sl and item_sl not in SUPPORT_LEVELS:
                    rep.errors.append({
                        "error_class":
                            "strict_registry_entry_invalid_support_level",
                        "message": (
                            f"(strict) {fp} (id={item_id!r}): "
                            f"support_level={item_sl!r} は "
                            f"{list(SUPPORT_LEVELS)} のいずれかが必要"
                        ),
                        "field_path": fp + ".support_level",
                    })
                # path が pack 外を指していないか
                if item_path:
                    try:
                        resolved = (base / item_path).resolve()
                        resolved.relative_to(base_resolved2)
                    except (OSError, ValueError):
                        rep.errors.append({
                            "error_class":
                                "strict_registry_entry_path_outside_pack",
                            "message": (
                                f"(strict) {fp} (id={item_id!r}): "
                                f"path {item_path!r} が pack 外を指している"
                            ),
                            "field_path": fp + ".path",
                        })
                        continue
                    # 参照先 instrument YAML との support_level 一致
                    if resolved.exists() and item_sl:
                        try:
                            idata = yaml.safe_load(
                                resolved.read_text(encoding="utf-8"),
                            ) or {}
                            inner_sl = ((idata.get("metadata") or {})
                                         .get("support_level"))
                            if inner_sl and inner_sl != item_sl:
                                rep.errors.append({
                                    "error_class":
                                        "strict_registry_entry_support_level_mismatch",
                                    "message": (
                                        f"(strict) {fp} (id={item_id!r}): "
                                        f"registry support_level="
                                        f"{item_sl!r} と instrument "
                                        f"metadata.support_level="
                                        f"{inner_sl!r} が不一致"
                                    ),
                                    "field_path": fp + ".support_level",
                                })
                        except Exception:
                            pass

    if rep.errors:
        rep.status = "error"
    elif rep.warnings:
        rep.status = "warning"
    return rep
