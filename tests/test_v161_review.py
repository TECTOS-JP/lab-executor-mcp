"""v1.6.1: v1.6.0 review response

- P0: raw 改行 (新規 / 既存 v1.6 file 全 parametrize)
- P1-2: extension_manifest.schema.json に catalog property
- P1-3: example extension.yaml が yaml.safe_load で正しく読める + multi-line
- P1-4: inspect_package が unsafe zip member を warning に出す
- P1-5: zip install の root-manifest / file 数 / size 上限
- P1-6: package_verification_status / strict_validation_status
- P1-7: zip 由来 installed_from の E2E
- P2-8: author vs catalog.authors の docs 記述
- P2-9: error_taxonomy に strict_missing_catalog_* / zip install errors
"""
from __future__ import annotations
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

from lab_executor.extension_install import install_definition_pack_from_zip
from lab_executor.extension_packaging import package_definition_pack
from lab_executor.extension_catalog import (
    inspect_package, list_catalog_installed, quality_signals,
)

ROOT = Path(__file__).parent.parent


# =========================================================
# Version
# =========================================================


V16_FILES_FULL = [
    "src/lab_executor/extension_catalog.py",
    "src/lab_executor/extension_install.py",
    "src/lab_executor/extension_packaging.py",
    "src/lab_executor/extension.py",
    "src/lab_executor/cli.py",
    "docs/extension_catalog.md",
    "docs/extension_install.md",
    "docs/extension_packaging.md",
    "docs/extension_integrity.md",
    "docs/extension_registry_overlay.md",
    "docs/error_taxonomy.md",
    "tests/test_v16_catalog.py",
    "tests/test_v16_zip_install.py",
    "tests/test_v161_review.py",
    "schemas/extension_manifest.schema.json",
    "examples/extensions/mock_basic_pack/extension.yaml",
    "README.md",
    "CHANGELOG.md",
]


@pytest.mark.parametrize("rel", V16_FILES_FULL)
def test_v161_lf_only(rel):
    p = ROOT / rel
    assert p.exists(), f"missing: {p}"
    text = p.read_text(encoding="utf-8")
    assert "\r" not in text, f"{rel} に CR が含まれる"


@pytest.mark.parametrize("rel", V16_FILES_FULL)
def test_v161_multiline(rel):
    p = ROOT / rel
    text = p.read_text(encoding="utf-8")
    assert text.count("\n") + 1 >= 5, f"{rel} が 5 行未満"


# =========================================================
# P1-2: schema に catalog field
# =========================================================


def test_extension_manifest_schema_contains_catalog():
    p = ROOT / "schemas" / "extension_manifest.schema.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    props = data.get("properties") or {}
    assert "catalog" in props, (
        f"catalog property not in schema.properties (keys={list(props)})"
    )
    # $defs に ExtensionCatalog が含まれる (Pydantic 標準形)
    defs = data.get("$defs") or {}
    assert any("Catalog" in k for k in defs), defs.keys()


# =========================================================
# P1-3: example YAML が parse できる + 期待 keys が揃う
# =========================================================


def test_example_extension_yaml_loads_with_catalog():
    p = ROOT / "examples" / "extensions" / "mock_basic_pack" / "extension.yaml"
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    # 必須 root
    for k in ("extension_id", "name", "version", "type", "contents",
              "stability"):
        assert k in data
    # catalog block が dict として正しい
    assert isinstance(data.get("catalog"), dict)
    cat = data["catalog"]
    assert isinstance(cat.get("summary"), str) and cat["summary"]
    assert cat.get("license") == "MIT"
    assert isinstance(cat.get("tags"), list)
    assert isinstance(cat.get("authors"), list)


def test_example_extension_yaml_multiline_layout():
    """raw 上の 1 行潰れ回帰を local file で検知"""
    p = ROOT / "examples" / "extensions" / "mock_basic_pack" / "extension.yaml"
    text = p.read_text(encoding="utf-8")
    assert text.count("\n") + 1 >= 20


# =========================================================
# P1-4: inspect_package が unsafe zip member を warn
# =========================================================


def test_inspect_package_warns_on_unsafe_member(tmp_path):
    """zip slip path があれば inspect で warning"""
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("extension.yaml", (
            "extension_id: t.evil\nname: x\nversion: 0.1.0\n"
            "type: definition_pack\n"
            "stability: { support_level: tested, executable_code: false }\n"
            "contents: { instruments: [] }\n"
        ))
        zf.writestr("../escape.txt", "bad")
    data = inspect_package(evil)
    # inspect は extract しないので status は ok or warning
    # (unsafe member は warning に格上げ)
    classes = {w.get("warning_class") for w in data.get("warnings", [])}
    assert "inspect_package_unsafe_member" in classes


# =========================================================
# P1-5: zip install の root layout / 上限
# =========================================================


def test_zip_install_rejects_no_root_manifest(tmp_path):
    """zip root 直下に extension.yaml が無い場合 → error"""
    p = tmp_path / "nested.zip"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("pack/extension.yaml", (
            "extension_id: t.nest\nname: x\nversion: 0.1.0\n"
            "type: definition_pack\n"
            "stability: { support_level: tested, executable_code: false }\n"
            "contents: { instruments: [] }\n"
        ))
    res = install_definition_pack_from_zip(
        p, extensions_dir=tmp_path / "ext",
        lockfile_path=tmp_path / "lock.json",
        skip_verify=True,
    )
    assert res.status == "error"
    assert any(
        (e.get("details") or {}).get("sub_class")
        == "extension_install_zip_no_root_manifest"
        for e in res.errors
    )


def test_zip_install_rejects_too_many_files(tmp_path):
    """file 数上限 (5000) 超過 → error"""
    p = tmp_path / "huge.zip"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("extension.yaml", "x")
        # 5001 個 (上限 5000) を超える
        for i in range(5001):
            zf.writestr(f"f{i:05d}.txt", "")
    res = install_definition_pack_from_zip(
        p, extensions_dir=tmp_path / "ext",
        lockfile_path=tmp_path / "lock.json",
        skip_verify=True,
    )
    assert res.status == "error"
    assert any(
        (e.get("details") or {}).get("sub_class")
        == "extension_install_zip_too_many_files"
        for e in res.errors
    )


def test_zip_install_rejects_too_large(tmp_path):
    """uncompressed total size 上限 (200MB) 超過 → error"""
    p = tmp_path / "big.zip"
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("extension.yaml", "x")
        # 1 file あたり 25 MB を 9 個 → 225 MB
        chunk = b"a" * (25 * 1024 * 1024)
        for i in range(9):
            zf.writestr(f"big{i}.bin", chunk)
    res = install_definition_pack_from_zip(
        p, extensions_dir=tmp_path / "ext",
        lockfile_path=tmp_path / "lock.json",
        skip_verify=True,
    )
    assert res.status == "error"
    assert any(
        (e.get("details") or {}).get("sub_class")
        == "extension_install_zip_too_large"
        for e in res.errors
    )


# =========================================================
# P1-6: package_verification_status / strict_validation_status
# =========================================================


def test_quality_signals_emits_verification_status_strings():
    sig = quality_signals(
        {"contents": {"instruments": []}, "catalog": {}}, Path("."),
    )
    assert sig["package_verification_status"] == "not_checked"
    assert sig["strict_validation_status"] == "not_checked"
    sig2 = quality_signals(
        {"contents": {"instruments": []}, "catalog": {}}, Path("."),
        package_verified=True, strict_validation_passed=False,
    )
    assert sig2["package_verification_status"] == "verified"
    assert sig2["strict_validation_status"] == "failed"


def test_inspect_package_emits_verification_status_strings(tmp_path):
    src_pack = ROOT / "examples" / "extensions" / "mock_basic_pack"
    pack = tmp_path / "src"
    shutil.copytree(src_pack, pack)
    pres = package_definition_pack(
        pack / "extension.yaml", output_dir=tmp_path / "dist",
    )
    assert pres.status == "ok"
    data = inspect_package(pres.package_path)
    sig = data["entry"]["quality_signals"]
    assert sig["package_verification_status"] == "not_checked"


# =========================================================
# P1-7: zip 由来 installed_from の E2E
# =========================================================


def test_zip_install_records_installed_from_in_catalog(tmp_path):
    """zip install 後 catalog --installed が installed_from.kind=package を
    返す E2E"""
    src_pack = ROOT / "examples" / "extensions" / "mock_basic_pack"
    pack = tmp_path / "src"
    shutil.copytree(src_pack, pack)
    pres = package_definition_pack(
        pack / "extension.yaml", output_dir=tmp_path / "dist",
    )
    assert pres.status == "ok"
    ext_dir = tmp_path / "ext"
    lockfile = tmp_path / "lock.json"
    inst = install_definition_pack_from_zip(
        pres.package_path,
        extensions_dir=ext_dir,
        lockfile_path=lockfile,
    )
    assert inst.status == "ok", inst.errors

    cat = list_catalog_installed(
        extensions_dir=ext_dir, lockfile_path=lockfile,
    )
    data = cat.to_dict()
    assert data["count"] == 1
    e = data["extensions"][0]
    inst_from = e["source"]["installed_from"]
    assert inst_from["kind"] == "package"
    assert inst_from["package_path"] == pres.package_path
    assert len(inst_from["package_sha256"]) == 64
    assert inst_from["package_format_version"] == "1.0"


# =========================================================
# P2-8 / P2-9: docs 反映
# =========================================================


def test_catalog_doc_mentions_author_vs_catalog_authors():
    text = (ROOT / "docs" / "extension_catalog.md").read_text(
        encoding="utf-8")
    assert "author" in text and "catalog.authors" in text
    # 表または段落で両者を区別している
    assert "推奨" in text


def test_catalog_doc_explains_package_verification_status():
    text = (ROOT / "docs" / "extension_catalog.md").read_text(
        encoding="utf-8")
    assert "package_verification_status" in text
    assert "not_checked" in text


def test_error_taxonomy_has_v16_zip_install_section():
    text = (ROOT / "docs" / "error_taxonomy.md").read_text(encoding="utf-8")
    for kw in (
        "Zip install (v1.6",
        "extension_install_zip_no_root_manifest",
        "extension_install_zip_too_many_files",
        "extension_install_zip_too_large",
        "strict_missing_catalog_summary",
        "strict_missing_catalog_license",
    ):
        assert kw in text, f"error_taxonomy.md に {kw!r} 無し"


def test_extension_install_doc_describes_zip_limits():
    text = (ROOT / "docs" / "extension_install.md").read_text(
        encoding="utf-8")
    for kw in ("5000", "200 MB", "zip root",
               "extension_install_zip_no_root_manifest"):
        assert kw in text, f"extension_install.md に {kw!r} 無し"


# =========================================================
# CHANGELOG
# =========================================================


def _run_cli(*args: str) -> tuple[int, str, str]:
    r = subprocess.run(
        [sys.executable, "-m", "lab_visa_mcp.cli", *args],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    return r.returncode, r.stdout, r.stderr


def test_cli_validate_schemas_includes_extension_manifest():
    """validate schemas 経路で extension_manifest schema が引き続き OK"""
    rc, out, err = _run_cli("validate", "schemas", "--json")
    assert rc == 0, err
    data = json.loads(out)
    files = [r["file"] for r in data.get("reports", [])]
    assert any("extension_manifest.schema.json" in f for f in files)
