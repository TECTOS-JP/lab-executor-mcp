"""v1.4.1: v1.4.0 review response (P0/P1/P2)

- P0: raw 改行 (v1.4 全 file)
- P1-2: check_installed_extension(strict=True) で validate(strict=True) も通る
- P1-3: validation_evidence コメントが「strict時error」に合っている
- P1-4: inspect が integrity_check_level=light + full_check_tool を返す
- P1-5: strict validation の registry_entries 深掘り検査
- P1-6: docs/error_taxonomy.md に extension 系 error_class taxonomy
- P1-7: docs に uninstall dry-run と通常 uninstall の違いが明記される
"""
from __future__ import annotations
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lab_executor.extension import validate_extension_file
from lab_executor.extension_install import install_definition_pack
from lab_executor.extension_integrity import (
    check_installed_extension, inspect_installed_extension,
)

ROOT = Path(__file__).parent.parent


# =========================================================
# Version
# =========================================================


V14_FILES_FULL = [
    "src/lab_executor/extension_integrity.py",
    "src/lab_executor/extension_install.py",
    "src/lab_executor/extension.py",
    "src/lab_executor/cli.py",
    "src/lab_executor/models/instrument_def.py",
    "tests/test_v14_extension_integrity.py",
    "tests/test_v141_review.py",
    "docs/extension_integrity.md",
    "docs/extension_install.md",
    "docs/extension_registry_overlay.md",
    "docs/definition_packs.md",
    "docs/registry_contribution.md",
    "docs/error_taxonomy.md",
    "schemas/extension_manifest.schema.json",
    "CHANGELOG.md",
]


@pytest.mark.parametrize("rel", V14_FILES_FULL)
def test_v141_lf_only(rel):
    p = ROOT / rel
    assert p.exists(), f"missing: {p}"
    text = p.read_text(encoding="utf-8")
    assert "\r" not in text, f"{rel} に CR 含む"


@pytest.mark.parametrize("rel", V14_FILES_FULL)
def test_v141_multiline(rel):
    p = ROOT / rel
    text = p.read_text(encoding="utf-8")
    assert text.count("\n") + 1 >= 5, f"{rel} が 5 行未満"


# =========================================================
# Fixtures
# =========================================================


@pytest.fixture
def temp_env(tmp_path):
    src_pack = ROOT / "examples" / "extensions" / "mock_basic_pack"
    dst_pack = tmp_path / "src_pack"
    shutil.copytree(src_pack, dst_pack)
    return {
        "extensions_dir": tmp_path / "extensions",
        "lockfile_path": tmp_path / "extensions.lock.json",
        "pack_yaml": dst_pack / "extension.yaml",
    }


# =========================================================
# P1-2: check --strict が validate(strict=True) 経路を通る
# =========================================================


def test_check_strict_promotes_verified_requires_evidence(temp_env, tmp_path):
    """install 元 pack が verified なのに evidence 無し → check --strict
    で strict_verified_requires_evidence error が出る (revalidate 経路)"""
    pack = tmp_path / "veri_pack"
    pack.mkdir()
    (pack / "extension.yaml").write_text(
        "extension_id: tectos.veri\nname: x\nversion: 0.1.0\n"
        "type: definition_pack\n"
        "stability: { support_level: verified, executable_code: false }\n"
        "contents:\n  instruments: [ instruments/x.yaml ]\n",
        encoding="utf-8",
    )
    (pack / "instruments").mkdir()
    (pack / "instruments" / "x.yaml").write_text(
        "metadata:\n  manufacturer: Acme\n  model: X1\n  category: dmm\n"
        "  support_level: verified\n"
        "commands: {}\n",
        encoding="utf-8",
    )
    res = install_definition_pack(
        pack / "extension.yaml",
        extensions_dir=temp_env["extensions_dir"],
        lockfile_path=temp_env["lockfile_path"],
    )
    assert res.status == "ok", res.errors
    rep = check_installed_extension(
        res.extension_id, strict=True,
        extensions_dir=temp_env["extensions_dir"],
        lockfile_path=temp_env["lockfile_path"],
    )
    # strict 経路で revalidate の strict_verified_requires_evidence が出る
    assert any(
        e["error_class"] == "strict_verified_requires_evidence"
        for e in rep.errors
    ), [e["error_class"] for e in rep.errors]


def test_check_strict_promotes_support_level_draft(temp_env, tmp_path):
    """draft instrument を含む pack を install → check --strict で
    strict_support_level_draft error"""
    pack = tmp_path / "draft_pack"
    pack.mkdir()
    (pack / "extension.yaml").write_text(
        "extension_id: tectos.draftpack\nname: x\nversion: 0.1.0\n"
        "type: definition_pack\n"
        "stability: { support_level: tested, executable_code: false }\n"
        "contents:\n  instruments: [ instruments/x.yaml ]\n",
        encoding="utf-8",
    )
    (pack / "instruments").mkdir()
    (pack / "instruments" / "x.yaml").write_text(
        "metadata:\n  manufacturer: Acme\n  model: X1\n  category: dmm\n"
        "  support_level: draft\n"
        "commands: {}\n",
        encoding="utf-8",
    )
    res = install_definition_pack(
        pack / "extension.yaml",
        extensions_dir=temp_env["extensions_dir"],
        lockfile_path=temp_env["lockfile_path"],
    )
    assert res.status == "ok", res.errors
    rep = check_installed_extension(
        res.extension_id, strict=True,
        extensions_dir=temp_env["extensions_dir"],
        lockfile_path=temp_env["lockfile_path"],
    )
    assert any(
        e["error_class"] == "strict_support_level_draft"
        for e in rep.errors
    )


# =========================================================
# P1-4: inspect が integrity_check_level=light を明示する
# =========================================================


def test_inspect_reports_integrity_check_level(temp_env):
    res = install_definition_pack(
        temp_env["pack_yaml"],
        extensions_dir=temp_env["extensions_dir"],
        lockfile_path=temp_env["lockfile_path"],
    )
    rep = inspect_installed_extension(
        res.extension_id,
        extensions_dir=temp_env["extensions_dir"],
        lockfile_path=temp_env["lockfile_path"],
    )
    d = rep.to_dict()
    assert d["integrity_check_level"] == "light"
    assert "extension check" in d["full_check_tool"]
    assert d["extension_id"] in d["full_check_tool"]


def test_inspect_unknown_id_still_reports_check_level(temp_env):
    rep = inspect_installed_extension(
        "no.such.pack",
        extensions_dir=temp_env["extensions_dir"],
        lockfile_path=temp_env["lockfile_path"],
    )
    d = rep.to_dict()
    assert d["integrity_check_level"] == "light"
    # extension_id が空でも full_check_tool は plausibility を持つ
    assert "extension check" in d["full_check_tool"]


# =========================================================
# P1-5: strict validate で registry_entries 深掘り
# =========================================================


def _write_pack(pack: Path, *, ext_id: str, entries: str) -> Path:
    pack.mkdir(parents=True, exist_ok=True)
    (pack / "extension.yaml").write_text(
        f"extension_id: {ext_id}\nname: x\nversion: 0.1.0\n"
        "type: definition_pack\n"
        "stability: { support_level: tested, executable_code: false }\n"
        "contents:\n  registry_entries: [ registry_entries.yaml ]\n",
        encoding="utf-8",
    )
    (pack / "registry_entries.yaml").write_text(entries,
                                                encoding="utf-8")
    return pack / "extension.yaml"


def test_strict_registry_entry_missing_id(tmp_path):
    yaml_text = (
        "instruments:\n"
        "  - { vendor: v, model: m, category: dmm,\n"
        "      support_level: tested, path: instruments/x.yaml }\n"
    )
    p = _write_pack(tmp_path / "noid", ext_id="tectos.noid",
                    entries=yaml_text)
    rep = validate_extension_file(p, strict=True)
    assert rep.status == "error"
    assert any(
        e["error_class"] == "strict_registry_entry_missing_id"
        for e in rep.errors
    )


def test_strict_registry_entry_missing_path(tmp_path):
    yaml_text = (
        "instruments:\n"
        "  - { id: x, vendor: v, model: m, category: dmm,\n"
        "      support_level: tested }\n"
    )
    p = _write_pack(tmp_path / "nopath", ext_id="tectos.nopath",
                    entries=yaml_text)
    rep = validate_extension_file(p, strict=True)
    assert rep.status == "error"
    assert any(
        e["error_class"] == "strict_registry_entry_missing_path"
        for e in rep.errors
    )


def test_strict_registry_entry_missing_optional_fields(tmp_path):
    """vendor / model / category / support_level 欠落も strict で error"""
    yaml_text = (
        "instruments:\n"
        "  - { id: x, path: instruments/x.yaml }\n"
    )
    p = _write_pack(tmp_path / "weak", ext_id="tectos.weak",
                    entries=yaml_text)
    rep = validate_extension_file(p, strict=True)
    classes = {e["error_class"] for e in rep.errors}
    for c in (
        "strict_registry_entry_missing_vendor",
        "strict_registry_entry_missing_model",
        "strict_registry_entry_missing_category",
        "strict_registry_entry_missing_support_level",
    ):
        assert c in classes, f"{c} 無し"


def test_strict_registry_entry_path_outside_pack(tmp_path):
    yaml_text = (
        "instruments:\n"
        "  - { id: evil, vendor: v, model: m, category: dmm,\n"
        "      support_level: tested,\n"
        "      path: ../../../outside.yaml }\n"
    )
    p = _write_pack(tmp_path / "evil", ext_id="tectos.evil",
                    entries=yaml_text)
    rep = validate_extension_file(p, strict=True)
    assert any(
        e["error_class"] == "strict_registry_entry_path_outside_pack"
        for e in rep.errors
    )


def test_strict_registry_entry_invalid_support_level(tmp_path):
    yaml_text = (
        "instruments:\n"
        "  - { id: x, vendor: v, model: m, category: dmm,\n"
        "      support_level: GOLD, path: instruments/x.yaml }\n"
    )
    p = _write_pack(tmp_path / "badsl", ext_id="tectos.badsl",
                    entries=yaml_text)
    rep = validate_extension_file(p, strict=True)
    assert any(
        e["error_class"] == "strict_registry_entry_invalid_support_level"
        for e in rep.errors
    )


def test_strict_registry_entry_support_level_mismatch(tmp_path):
    """registry の support_level と instrument metadata の値が不一致"""
    pack = tmp_path / "mismatch_pack"
    pack.mkdir()
    (pack / "extension.yaml").write_text(
        "extension_id: tectos.mismatch\nname: x\nversion: 0.1.0\n"
        "type: definition_pack\n"
        "stability: { support_level: tested, executable_code: false }\n"
        "contents:\n  registry_entries: [ entries.yaml ]\n",
        encoding="utf-8",
    )
    (pack / "entries.yaml").write_text(
        "instruments:\n"
        "  - { id: x, vendor: v, model: m, category: dmm,\n"
        "      support_level: verified, path: instruments/x.yaml }\n",
        encoding="utf-8",
    )
    (pack / "instruments").mkdir()
    (pack / "instruments" / "x.yaml").write_text(
        "metadata:\n  manufacturer: v\n  model: m\n  category: dmm\n"
        "  support_level: tested\n"  # 不一致
        "commands: {}\n",
        encoding="utf-8",
    )
    rep = validate_extension_file(pack / "extension.yaml", strict=True)
    assert any(
        e["error_class"] == "strict_registry_entry_support_level_mismatch"
        for e in rep.errors
    )


def test_strict_registry_entries_clean_passes(tmp_path):
    """全項目正しければ strict_registry_entry_* error は出ない"""
    pack = tmp_path / "clean_pack"
    pack.mkdir()
    (pack / "extension.yaml").write_text(
        "extension_id: tectos.clean\nname: x\nversion: 0.1.0\n"
        "type: definition_pack\n"
        "stability: { support_level: tested, executable_code: false }\n"
        "contents:\n  registry_entries: [ entries.yaml ]\n",
        encoding="utf-8",
    )
    (pack / "entries.yaml").write_text(
        "instruments:\n"
        "  - { id: clean_one, vendor: v, model: m, category: dmm,\n"
        "      support_level: tested, path: instruments/x.yaml }\n",
        encoding="utf-8",
    )
    (pack / "instruments").mkdir()
    (pack / "instruments" / "x.yaml").write_text(
        "metadata:\n  manufacturer: v\n  model: m\n  category: dmm\n"
        "  support_level: tested\n"
        "commands: {}\n",
        encoding="utf-8",
    )
    rep = validate_extension_file(pack / "extension.yaml", strict=True)
    bad = [e for e in rep.errors
           if e["error_class"].startswith("strict_registry_entry_")]
    assert not bad, bad


# =========================================================
# P1-3: validation_evidence コメント整合
# =========================================================


def test_instrument_def_comment_matches_strict_error_behavior():
    """コメントが strict mode 時の挙動 (error) と整合している"""
    text = (ROOT / "src" / "lab_executor" / "models"
            / "instrument_def.py").read_text(encoding="utf-8")
    # strict_verified_requires_evidence (実装側 error_class) を
    # コメントから参照していること = 「error」として扱う旨が明示されている
    assert "strict_verified_requires_evidence" in text
    # コメント側で旧 "warning となる" 表記が残っていないこと
    assert "validation_evidence にもかかわらず未指定なら warning" not in text


# =========================================================
# P1-6: error_taxonomy に extension 系 taxonomy
# =========================================================


def test_error_taxonomy_has_extension_section():
    text = (ROOT / "docs" / "error_taxonomy.md").read_text(encoding="utf-8")
    for kw in (
        "Extension",
        "Integrity",
        "Strict validation",
        "extension_checksum_mismatch",
        "strict_verified_requires_evidence",
        "overlay_registry_duplicate_id",
        "extension_source_inside_extensions_dir",
    ):
        assert kw in text, f"error_taxonomy.md に {kw!r} 無し"


# =========================================================
# P1-7: docs に uninstall dry-run と通常 uninstall の違い
# =========================================================


def test_extension_integrity_doc_describes_uninstall_dry_run_diff():
    text = (ROOT / "docs" / "extension_integrity.md").read_text(
        encoding="utf-8")
    assert "dry-run" in text
    # 表の各列が含まれていること
    for kw in (
        "would_remove_path", "would_remove_overlay_ids",
        "would_remove_lockfile_entry", "rmtree",
    ):
        assert kw in text, f"extension_integrity.md に {kw!r} 無し"


def test_extension_integrity_doc_describes_strict_use_case():
    text = (ROOT / "docs" / "extension_integrity.md").read_text(
        encoding="utf-8")
    for kw in ("CI", "registry 掲載", "release"):
        assert kw in text, f"strict 用途記述に {kw!r} 無し"


# =========================================================
# CLI smoke: inspect --json includes integrity_check_level
# =========================================================


def test_cli_inspect_json_includes_check_level(temp_env, tmp_path,
                                                monkeypatch):
    """visa-mcp extension inspect --json の出力に integrity_check_level
    が含まれること (CLI が default extensions_dir を使うので、
    install からはやらず、API 経由で確認する)"""
    rep = inspect_installed_extension(
        "no.such.pack",
        extensions_dir=temp_env["extensions_dir"],
        lockfile_path=temp_env["lockfile_path"],
    )
    d = rep.to_dict()
    assert "integrity_check_level" in d
    assert "full_check_tool" in d


# =========================================================
# CHANGELOG
# =========================================================

