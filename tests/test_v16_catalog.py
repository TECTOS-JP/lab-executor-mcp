"""v1.6.0: Catalog metadata / discovery tests

- catalog field を extension.yaml schema レベルで受け入れる
- support_level_summary / quality_signals helpers
- list_catalog_installed / list_catalog_packages / inspect_package
- CLI extension catalog / inspect-package
- installed_from (directory / package)
- strict mode で catalog.summary / license 空 → error
"""
from __future__ import annotations
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from lab_executor.extension import (
    ExtensionCatalog, ExtensionManifest, validate_extension_file,
)
from lab_executor.extension_install import (
    install_definition_pack, install_definition_pack_from_zip,
)
from lab_executor.extension_packaging import package_definition_pack
from lab_executor.extension_catalog import (
    support_level_summary, quality_signals, inspect_package,
    list_catalog_installed, list_catalog_packages,
)

ROOT = Path(__file__).parent.parent


# =========================================================
# Schema
# =========================================================


def test_catalog_field_optional_defaults_empty():
    cat = ExtensionCatalog()
    assert cat.summary == ""
    assert cat.license == ""
    assert cat.tags == []
    assert cat.authors == []


def test_manifest_accepts_catalog_dict():
    m = ExtensionManifest(
        extension_id="t.cat",
        name="Cat Pack",
        version="0.1.0",
        type="definition_pack",
        stability={"support_level": "tested", "executable_code": False},
        catalog={
            "summary": "hello",
            "license": "MIT",
            "tags": ["a", "b"],
            "authors": [{"name": "TECTOS"}],
        },
    )
    assert m.catalog.summary == "hello"
    assert m.catalog.license == "MIT"
    assert m.catalog.tags == ["a", "b"]
    assert m.catalog.authors == [{"name": "TECTOS"}]


def test_manifest_without_catalog_still_works():
    m = ExtensionManifest(
        extension_id="t.nocat",
        name="No Cat",
        version="0.1.0",
        type="definition_pack",
        stability={"support_level": "tested", "executable_code": False},
    )
    assert m.catalog.summary == ""


# =========================================================
# helpers
# =========================================================


@pytest.fixture
def temp_pack(tmp_path):
    src = ROOT / "examples" / "extensions" / "mock_basic_pack"
    dst = tmp_path / "src_pack"
    shutil.copytree(src, dst)
    return {
        "pack_dir": dst,
        "pack_yaml": dst / "extension.yaml",
        "out_dir": tmp_path / "dist",
        "extensions_dir": tmp_path / "extensions",
        "lockfile_path": tmp_path / "extensions.lock.json",
    }


def test_support_level_summary_counts_correctly(tmp_path):
    pack = tmp_path / "p"
    pack.mkdir()
    inst = pack / "instruments"
    inst.mkdir()
    (inst / "a.yaml").write_text(
        "metadata: { manufacturer: A, model: M, category: dmm,\n"
        "  support_level: verified }\ncommands: {}\n",
        encoding="utf-8")
    (inst / "b.yaml").write_text(
        "metadata: { manufacturer: A, model: M, category: dmm,\n"
        "  support_level: tested }\ncommands: {}\n",
        encoding="utf-8")
    (inst / "c.yaml").write_text(
        "metadata: { manufacturer: A, model: M, category: dmm,\n"
        "  support_level: draft }\ncommands: {}\n",
        encoding="utf-8")
    s = support_level_summary(
        pack, ["instruments/a.yaml", "instruments/b.yaml",
               "instruments/c.yaml"],
    )
    assert s == {"verified": 1, "tested": 1, "experimental": 0,
                  "draft": 1}


def test_quality_signals_structure(temp_pack):
    manifest = yaml.safe_load(
        temp_pack["pack_yaml"].read_text(encoding="utf-8"))
    sig = quality_signals(manifest, temp_pack["pack_dir"])
    # 必須 key
    for k in (
        "has_readme", "has_catalog_summary", "has_catalog_license",
        "has_validation_evidence",
        "verified_instruments", "tested_instruments",
        "experimental_instruments", "draft_instruments",
        "package_verified", "strict_validation_passed",
    ):
        assert k in sig
    # **数値 score は出さない** (回帰防止)
    assert "quality_score" not in sig
    assert "score" not in sig
    assert "recommended" not in sig


# =========================================================
# catalog (installed)
# =========================================================


def test_list_catalog_installed_returns_entries(temp_pack):
    install_definition_pack(
        temp_pack["pack_yaml"],
        extensions_dir=temp_pack["extensions_dir"],
        lockfile_path=temp_pack["lockfile_path"],
    )
    rep = list_catalog_installed(
        extensions_dir=temp_pack["extensions_dir"],
        lockfile_path=temp_pack["lockfile_path"],
    )
    data = rep.to_dict()
    assert data["count"] == 1
    e = data["extensions"][0]
    assert e["extension_id"] == "visa-mcp.mock.basic"
    assert "catalog" in e
    assert "contents_summary" in e
    assert "support_level_summary" in e
    assert "quality_signals" in e
    assert e["source"]["kind"] == "installed"
    # installed_from が directory
    assert e["source"]["installed_from"]["kind"] == "directory"


def test_list_catalog_installed_empty(tmp_path):
    rep = list_catalog_installed(
        extensions_dir=tmp_path / "ext",
        lockfile_path=tmp_path / "lock.json",
    )
    assert rep.to_dict()["count"] == 0


# =========================================================
# inspect-package
# =========================================================


def test_inspect_package_reads_zip_metadata(temp_pack):
    pres = package_definition_pack(
        temp_pack["pack_yaml"], output_dir=temp_pack["out_dir"],
    )
    assert pres.status == "ok"
    data = inspect_package(pres.package_path)
    assert data["status"] == "ok"
    e = data["entry"]
    assert e["extension_id"] == "visa-mcp.mock.basic"
    assert e["version"] == "0.1.0"
    assert e["source"]["kind"] == "package"
    assert e["source"]["package_path"] == pres.package_path
    assert e["source"]["package_format"] == "visa-mcp-extension-package"
    assert e["package_manifest"] is not None
    # quality_signals に score が出ないこと
    assert "score" not in e["quality_signals"]


def test_inspect_package_reads_catalog_metadata(tmp_path):
    """catalog field 付き pack を package → inspect で catalog が読める"""
    pack = tmp_path / "cat_pack"
    pack.mkdir()
    (pack / "extension.yaml").write_text(
        "extension_id: t.catalog\nname: cp\nversion: 0.1.0\n"
        "type: definition_pack\n"
        "stability: { support_level: tested, executable_code: false }\n"
        "contents:\n  instruments: [ instruments/foo.yaml ]\n"
        "catalog:\n"
        "  summary: Hello catalog\n"
        "  license: MIT\n"
        "  tags: [demo, test]\n",
        encoding="utf-8")
    (pack / "instruments").mkdir()
    (pack / "instruments" / "foo.yaml").write_text(
        "metadata: { manufacturer: A, model: M, category: dmm,\n"
        "  support_level: tested }\ncommands: {}\n",
        encoding="utf-8")
    (pack / "README.md").write_text("# pack\n", encoding="utf-8")
    pres = package_definition_pack(
        pack / "extension.yaml", output_dir=tmp_path / "dist",
    )
    assert pres.status == "ok"
    data = inspect_package(pres.package_path)
    assert data["status"] == "ok"
    cat = data["entry"]["catalog"]
    assert cat["summary"] == "Hello catalog"
    assert cat["license"] == "MIT"
    assert cat["tags"] == ["demo", "test"]
    sig = data["entry"]["quality_signals"]
    assert sig["has_readme"] is True
    assert sig["has_catalog_summary"] is True
    assert sig["has_catalog_license"] is True


def test_inspect_package_reports_quality_signals(temp_pack):
    pres = package_definition_pack(
        temp_pack["pack_yaml"], output_dir=temp_pack["out_dir"],
    )
    data = inspect_package(pres.package_path)
    sig = data["entry"]["quality_signals"]
    assert isinstance(sig["verified_instruments"], int)
    assert isinstance(sig["tested_instruments"], int)


def test_inspect_package_rejects_missing(tmp_path):
    data = inspect_package(tmp_path / "no.zip")
    assert data["status"] == "error"
    assert any(e["error_class"] == "not_found" for e in data["errors"])


def test_inspect_package_rejects_bad_zip(tmp_path):
    p = tmp_path / "bad.zip"
    p.write_bytes(b"not a zip")
    data = inspect_package(p)
    assert data["status"] == "error"
    assert any(e["error_class"] == "package_invalid_zip"
               for e in data["errors"])


# =========================================================
# list_catalog_packages
# =========================================================


def test_list_catalog_packages_lists_dir(temp_pack):
    pres = package_definition_pack(
        temp_pack["pack_yaml"], output_dir=temp_pack["out_dir"],
    )
    assert pres.status == "ok"
    rep = list_catalog_packages(temp_pack["out_dir"])
    data = rep.to_dict()
    assert data["count"] == 1
    assert data["extensions"][0]["extension_id"] == "visa-mcp.mock.basic"


def test_list_catalog_packages_missing_dir(tmp_path):
    rep = list_catalog_packages(tmp_path / "no_such")
    assert rep.status == "error"


# =========================================================
# installed_from (zip 経由)
# =========================================================


def test_zip_install_writes_installed_from_package(temp_pack):
    pres = package_definition_pack(
        temp_pack["pack_yaml"], output_dir=temp_pack["out_dir"],
    )
    assert pres.status == "ok"
    inst = install_definition_pack_from_zip(
        pres.package_path,
        extensions_dir=temp_pack["extensions_dir"],
        lockfile_path=temp_pack["lockfile_path"],
    )
    assert inst.status == "ok", inst.errors
    meta = json.loads(
        (Path(inst.install_path) / ".install_meta.json").read_text(
            encoding="utf-8"))
    inst_from = meta.get("installed_from") or {}
    assert inst_from.get("kind") == "package"
    assert inst_from.get("package_path") == pres.package_path
    assert len(inst_from.get("package_sha256") or "") == 64
    assert inst_from.get("package_format_version") == "1.0"


def test_yaml_install_writes_installed_from_directory(temp_pack):
    res = install_definition_pack(
        temp_pack["pack_yaml"],
        extensions_dir=temp_pack["extensions_dir"],
        lockfile_path=temp_pack["lockfile_path"],
    )
    assert res.status == "ok"
    meta = json.loads(
        (Path(res.install_path) / ".install_meta.json").read_text(
            encoding="utf-8"))
    inst_from = meta.get("installed_from") or {}
    assert inst_from.get("kind") == "directory"


# =========================================================
# strict warnings for catalog
# =========================================================


def test_validate_extension_warns_missing_catalog_summary(tmp_path):
    p = tmp_path / "ext.yaml"
    p.write_text(
        "extension_id: t.no_cat\nname: x\nversion: 0.1.0\n"
        "type: definition_pack\n"
        "stability: { support_level: tested, executable_code: false }\n"
        "contents:\n  instruments: []\n",
        encoding="utf-8",
    )
    # contents が空なので empty_contents warning も出るが、
    # catalog 系 warning が含まれることを確認
    rep = validate_extension_file(p)
    classes = {w["warning_class"] for w in rep.warnings}
    assert "missing_catalog_summary" in classes
    assert "missing_catalog_license" in classes


def test_validate_extension_strict_promotes_catalog_warnings(tmp_path):
    pack = tmp_path / "no_cat_pack"
    pack.mkdir()
    (pack / "extension.yaml").write_text(
        "extension_id: t.no_cat2\nname: x\nversion: 0.1.0\n"
        "type: definition_pack\n"
        "stability: { support_level: tested, executable_code: false }\n"
        "contents:\n  instruments: [ instruments/x.yaml ]\n",
        encoding="utf-8",
    )
    (pack / "instruments").mkdir()
    (pack / "instruments" / "x.yaml").write_text(
        "metadata: { manufacturer: A, model: M, category: dmm,\n"
        "  support_level: tested }\ncommands: {}\n",
        encoding="utf-8")
    (pack / "README.md").write_text("# x\n", encoding="utf-8")
    rep = validate_extension_file(pack / "extension.yaml", strict=True)
    classes = {e["error_class"] for e in rep.errors}
    # strict で error 化
    assert "strict_missing_catalog_summary" in classes
    assert "strict_missing_catalog_license" in classes


def test_validate_extension_with_catalog_passes_strict(tmp_path):
    pack = tmp_path / "okcat_pack"
    pack.mkdir()
    (pack / "extension.yaml").write_text(
        "extension_id: t.okcat\nname: x\nversion: 0.1.0\n"
        "type: definition_pack\n"
        "stability: { support_level: tested, executable_code: false }\n"
        "contents:\n  instruments: [ instruments/x.yaml ]\n"
        "catalog:\n  summary: ok pack\n  license: MIT\n",
        encoding="utf-8",
    )
    (pack / "instruments").mkdir()
    (pack / "instruments" / "x.yaml").write_text(
        "metadata: { manufacturer: A, model: M, category: dmm,\n"
        "  support_level: tested }\ncommands: {}\n",
        encoding="utf-8")
    (pack / "README.md").write_text("# x\n", encoding="utf-8")
    rep = validate_extension_file(pack / "extension.yaml", strict=True)
    classes = {e["error_class"] for e in rep.errors}
    assert "strict_missing_catalog_summary" not in classes
    assert "strict_missing_catalog_license" not in classes


# =========================================================
# CLI
# =========================================================


def _run_cli(*args: str) -> tuple[int, str, str]:
    r = subprocess.run(
        [sys.executable, "-m", "lab_visa_mcp.cli", *args],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    return r.returncode, r.stdout, r.stderr


def test_cli_extension_catalog_help():
    rc, out, err = _run_cli("extension", "catalog", "--help")
    text = out + err
    assert "catalog" in text
    assert "--installed" in text
    assert "--packages" in text


def test_cli_extension_inspect_package_help():
    rc, out, err = _run_cli("extension", "inspect-package", "--help")
    text = out + err
    assert "inspect-package" in text


def test_cli_extension_catalog_packages_runs(temp_pack):
    pres = package_definition_pack(
        temp_pack["pack_yaml"], output_dir=temp_pack["out_dir"],
    )
    assert pres.status == "ok"
    rc, out, err = _run_cli(
        "extension", "catalog", "--packages", str(temp_pack["out_dir"]),
        "--json",
    )
    assert rc == 0, err
    data = json.loads(out)
    assert data["catalog"]["count"] >= 1


def test_cli_extension_inspect_package_runs(temp_pack):
    pres = package_definition_pack(
        temp_pack["pack_yaml"], output_dir=temp_pack["out_dir"],
    )
    rc, out, err = _run_cli(
        "extension", "inspect-package", pres.package_path, "--json",
    )
    assert rc == 0, err
    data = json.loads(out)
    assert data["inspect_package"]["status"] == "ok"
    assert data["inspect_package"]["entry"]["extension_id"] == (
        "visa-mcp.mock.basic"
    )


# =========================================================
# Repo format
# =========================================================


V16_FILES = [
    "src/lab_executor/extension_catalog.py",
    "src/lab_executor/extension.py",
    "src/lab_executor/cli.py",
    "docs/extension_catalog.md",
    "tests/test_v16_catalog.py",
    "CHANGELOG.md",
]


@pytest.mark.parametrize("rel", V16_FILES)
def test_v16_catalog_files_lf_only(rel):
    p = ROOT / rel
    assert p.exists()
    text = p.read_text(encoding="utf-8")
    assert "\r" not in text


@pytest.mark.parametrize("rel", V16_FILES)
def test_v16_catalog_files_multiline(rel):
    p = ROOT / rel
    text = p.read_text(encoding="utf-8")
    assert text.count("\n") + 1 >= 5


# =========================================================
# docs / CHANGELOG
# =========================================================


def test_catalog_doc_keywords():
    text = (ROOT / "docs" / "extension_catalog.md").read_text(
        encoding="utf-8")
    for kw in (
        "extension catalog", "extension inspect-package",
        "support_level_summary", "quality_signals",
        "installed_from", "score 化しない", "remote registry",
        "catalog:", "summary",
    ):
        assert kw in text, f"extension_catalog.md に {kw!r} 無し"

