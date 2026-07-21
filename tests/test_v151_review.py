"""v1.5.1: v1.5.0 review response

- P0: raw 改行 / multi-line (新規 + 既存 v1.5 file)
- P1-2: CLI help に具体例と strict 説明 (package / verify-package)
- P1-3: docs に normal vs strict 比較表
- P1-4: package_manifest.json の field description 明確化
- P1-5: package → install → overlay registry 反映の確認テスト
"""
from __future__ import annotations
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

from lab_executor.extension_install import (
    install_definition_pack, load_overlay_registry,
)
from lab_executor.extension_packaging import (
    package_definition_pack, verify_extension_package,
)

ROOT = Path(__file__).parent.parent


# =========================================================
# Version
# =========================================================


V15_FILES_FULL = [
    "src/lab_executor/extension_packaging.py",
    "src/lab_executor/extension_install.py",
    "src/lab_executor/extension_integrity.py",
    "src/lab_executor/extension.py",
    "src/lab_executor/cli.py",
    "docs/extension_packaging.md",
    "docs/extension_publishing_checklist.md",
    "docs/extension_integrity.md",
    "docs/extension_install.md",
    "docs/extension_registry_overlay.md",
    "docs/error_taxonomy.md",
    "tests/test_v15_extension_packaging.py",
    "tests/test_v151_review.py",
    "CHANGELOG.md",
    "schemas/extension_manifest.schema.json",
]


@pytest.mark.parametrize("rel", V15_FILES_FULL)
def test_v151_lf_only(rel):
    p = ROOT / rel
    assert p.exists(), f"missing: {p}"
    text = p.read_text(encoding="utf-8")
    assert "\r" not in text, f"{rel} に CR 含む"


@pytest.mark.parametrize("rel", V15_FILES_FULL)
def test_v151_multiline(rel):
    p = ROOT / rel
    text = p.read_text(encoding="utf-8")
    assert text.count("\n") + 1 >= 5, f"{rel} が 5 行未満"


# =========================================================
# Fixtures
# =========================================================


@pytest.fixture
def temp_pack(tmp_path):
    src_pack = ROOT / "examples" / "extensions" / "mock_basic_pack"
    dst = tmp_path / "src_pack"
    shutil.copytree(src_pack, dst)
    return {
        "pack_dir": dst,
        "pack_yaml": dst / "extension.yaml",
        "out_dir": tmp_path / "dist",
        "extensions_dir": tmp_path / "extensions",
        "lockfile_path": tmp_path / "extensions.lock.json",
    }


# =========================================================
# P1-2: CLI help に具体例 / strict 説明
# =========================================================


def _run_cli(*args: str) -> tuple[int, str, str]:
    r = subprocess.run(
        [sys.executable, "-m", "lab_visa_mcp.cli", *args],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    return r.returncode, r.stdout, r.stderr


def test_cli_extension_package_help_has_example_and_strict_info():
    rc, out, err = _run_cli("extension", "package", "--help")
    text = out + err
    assert "例:" in text or "Example" in text or "例" in text
    # 具体的なコマンド例
    assert "visa-mcp extension package" in text
    # strict 説明 (verified evidence / README)
    assert "strict" in text.lower()
    assert "validation_evidence" in text or "verified" in text
    assert "README" in text


def test_cli_extension_verify_package_help_has_example_and_checks():
    rc, out, err = _run_cli("extension", "verify-package", "--help")
    text = out + err
    assert "visa-mcp extension verify-package" in text
    # 検査項目の列挙
    assert "checksum" in text.lower() or "sha256" in text.lower()
    assert "zip slip" in text.lower() or "zip_slip" in text.lower()
    assert "executable_code" in text


# =========================================================
# P1-3: docs に normal vs strict 比較表
# =========================================================


def test_packaging_doc_has_normal_vs_strict_table():
    text = (ROOT / "docs" / "extension_packaging.md").read_text(
        encoding="utf-8")
    # 比較表 section
    assert "Normal vs Strict" in text
    # 表項目
    for kw in (
        "ローカル開発", "CI / registry 掲載",
        "strict_verified_requires_evidence",
        "strict_missing_pack_readme",
        "strict_support_level_draft",
        "strict_registry_entry",
    ):
        assert kw in text, f"normal/strict 表に {kw!r} 無し"


# =========================================================
# P1-4: package_manifest field description
# =========================================================


def test_packaging_doc_has_manifest_field_table():
    text = (ROOT / "docs" / "extension_packaging.md").read_text(
        encoding="utf-8")
    assert "Field 仕様" in text or "field 仕様" in text.lower()
    # 各 field の説明が表に含まれる
    for kw in (
        "package_format", "package_format_version",
        "extension_id", "extension_version",
        "created_at", "created_by",
        "executable_code", "file_count", "files",
        "checksums_file", "checksums_sha256",
    ):
        assert kw in text, f"manifest field 表に {kw!r} 無し"
    # 後方互換ポリシー
    assert "後方互換" in text or "minor up" in text


# =========================================================
# P1-5: package → install → overlay registry 反映
# =========================================================


def test_packaged_then_installed_pack_appears_in_overlay(temp_pack, tmp_path):
    """package → 展開 → install → overlay registry に extension 由来として
    現れる、までの一連を確認"""
    # 1. package
    res = package_definition_pack(
        temp_pack["pack_yaml"], output_dir=temp_pack["out_dir"],
    )
    assert res.status == "ok", res.errors
    zip_path = Path(res.package_path)
    assert zip_path.exists()

    # 2. verify-package
    vrep = verify_extension_package(zip_path)
    assert vrep.status in ("ok", "warning"), vrep.errors

    # 3. zip を展開して、その中の extension.yaml から install
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extracted)
    inst = install_definition_pack(
        extracted / "extension.yaml",
        extensions_dir=temp_pack["extensions_dir"],
        lockfile_path=temp_pack["lockfile_path"],
    )
    assert inst.status == "ok", inst.errors
    assert inst.extension_id == "visa-mcp.mock.basic"

    # 4. overlay registry に extension 由来として現れる
    #    (registry_entries が無い example pack なので、entries には載らないが、
    #     installed_extensions として list_installed_packs に出る)
    from lab_executor.extension_install import list_installed_packs
    packs = list_installed_packs(
        extensions_dir=temp_pack["extensions_dir"],
        lockfile_path=temp_pack["lockfile_path"],
    )
    assert any(p["extension_id"] == "visa-mcp.mock.basic" for p in packs)

    # builtin registry と共に overlay を読む
    overlay = load_overlay_registry(
        ROOT / "registry" / "INDEX.yaml",
        extensions_dir=temp_pack["extensions_dir"],
        lockfile_path=temp_pack["lockfile_path"],
    )
    assert overlay.status in ("ok", "warning"), overlay.errors
    # builtin / extension の sum が >= builtin 単独以上であること
    assert overlay.to_dict()["builtin_count"] >= 1


def test_packaged_pack_with_registry_entries_shows_in_overlay(
        temp_pack, tmp_path):
    """registry_entries 付き pack を package → install → overlay で
    extension 由来 entry が見えること"""
    # registry_entries を含む pack を新規作成
    pack = tmp_path / "regp"
    pack.mkdir()
    (pack / "extension.yaml").write_text(
        "extension_id: tectos.regpack\nname: rp\nversion: 0.1.0\n"
        "type: definition_pack\n"
        "stability: { support_level: tested, executable_code: false }\n"
        "contents:\n"
        "  instruments: [ instruments/foo.yaml ]\n"
        "  registry_entries: [ entries.yaml ]\n",
        encoding="utf-8",
    )
    (pack / "instruments").mkdir()
    (pack / "instruments" / "foo.yaml").write_text(
        "metadata:\n  manufacturer: TECTOS\n  model: Foo\n  category: dmm\n"
        "  support_level: tested\ncommands: {}\n",
        encoding="utf-8",
    )
    (pack / "entries.yaml").write_text(
        "instruments:\n"
        "  - { id: tectos_foo, vendor: TECTOS, model: Foo,\n"
        "      category: dmm, support_level: tested,\n"
        "      path: instruments/foo.yaml }\n",
        encoding="utf-8",
    )

    # package
    pres = package_definition_pack(
        pack / "extension.yaml", output_dir=tmp_path / "dist",
    )
    assert pres.status == "ok", pres.errors

    # verify
    vrep = verify_extension_package(pres.package_path)
    assert vrep.status in ("ok", "warning"), vrep.errors

    # extract + install
    extracted = tmp_path / "extracted2"
    extracted.mkdir()
    with zipfile.ZipFile(pres.package_path) as zf:
        zf.extractall(extracted)
    inst = install_definition_pack(
        extracted / "extension.yaml",
        extensions_dir=temp_pack["extensions_dir"],
        lockfile_path=temp_pack["lockfile_path"],
    )
    assert inst.status == "ok", inst.errors

    # overlay registry に extension 由来 entry "tectos_foo" が見える
    overlay = load_overlay_registry(
        None,
        extensions_dir=temp_pack["extensions_dir"],
        lockfile_path=temp_pack["lockfile_path"],
    )
    ext_entries = [
        e for e in overlay.entries
        if e.source.get("kind") == "extension"
    ]
    assert any(e.id == "tectos_foo" for e in ext_entries), (
        f"extension 由来 entries: {[e.id for e in ext_entries]}"
    )
    # source.extension_id が正しい
    foo = next(e for e in ext_entries if e.id == "tectos_foo")
    assert foo.source["extension_id"] == "tectos.regpack"
    assert foo.source["extension_version"] == "0.1.0"


# =========================================================
# CHANGELOG
# =========================================================

