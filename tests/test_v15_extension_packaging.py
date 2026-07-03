"""v1.5.0: Definition Pack Packaging / verify-package tests"""
from __future__ import annotations
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from lab_executor import stability
from lab_executor.extension_packaging import (
    package_definition_pack, verify_extension_package,
    PACKAGE_FORMAT, PACKAGE_FORMAT_VERSION, PACKAGE_SUFFIX,
)

ROOT = Path(__file__).parent.parent


# =========================================================
# Version + MCP surface
# =========================================================


def test_no_new_mcp_tools_in_v1_5():
    """v1.5 でも MCP surface は不変"""
    assert stability.stable_count() == 43
    assert stability.experimental_count() == 7
    assert stability.total_documented_count() == 50


def test_stable_tools_unchanged_in_v1_5():
    names = set(stability.stable_tool_names())
    assert "validate_experiment_plan" in names
    assert "list_resources" in names


# =========================================================
# Fixtures
# =========================================================


@pytest.fixture
def temp_pack(tmp_path):
    """example pack を tmp に copy"""
    src_pack = ROOT / "examples" / "extensions" / "mock_basic_pack"
    dst = tmp_path / "src_pack"
    shutil.copytree(src_pack, dst)
    return {
        "pack_dir": dst,
        "pack_yaml": dst / "extension.yaml",
        "out_dir": tmp_path / "dist",
    }


# =========================================================
# package
# =========================================================


def test_extension_package_success(temp_pack):
    res = package_definition_pack(
        temp_pack["pack_yaml"],
        output_dir=temp_pack["out_dir"],
    )
    assert res.status == "ok", res.errors
    zip_path = Path(res.package_path)
    assert zip_path.exists()
    assert zip_path.name.endswith(PACKAGE_SUFFIX)
    assert zip_path.name.startswith("visa-mcp.mock.basic-0.1.0")
    assert res.file_count >= 1
    assert len(res.package_sha256) == 64


def test_extension_package_writes_package_manifest(temp_pack):
    res = package_definition_pack(
        temp_pack["pack_yaml"], output_dir=temp_pack["out_dir"],
    )
    assert res.status == "ok"
    with zipfile.ZipFile(res.package_path) as zf:
        manifest = json.loads(zf.read("package_manifest.json"))
    assert manifest["package_format"] == PACKAGE_FORMAT
    assert manifest["package_format_version"] == PACKAGE_FORMAT_VERSION
    assert manifest["extension_id"] == "visa-mcp.mock.basic"
    assert manifest["extension_version"] == "0.1.0"
    assert manifest["executable_code"] is False
    assert manifest["file_count"] >= 1
    assert "files" in manifest and len(manifest["files"]) >= 1
    for fi in manifest["files"]:
        assert "path" in fi and "sha256" in fi
        assert len(fi["sha256"]) == 64


def test_extension_package_writes_checksums(temp_pack):
    res = package_definition_pack(
        temp_pack["pack_yaml"], output_dir=temp_pack["out_dir"],
    )
    assert res.status == "ok"
    with zipfile.ZipFile(res.package_path) as zf:
        cs = zf.read("checksums.sha256").decode("utf-8")
    lines = [l for l in cs.splitlines() if l.strip()]
    assert lines
    for line in lines:
        sha, _sep, rel = line.partition("  ")
        assert len(sha) == 64
        assert rel


def test_extension_package_requires_valid_manifest(tmp_path):
    """invalid extension.yaml (executable_code=true) は package 化できない"""
    p = tmp_path / "ext.yaml"
    p.write_text(
        "extension_id: a.b\nname: x\nversion: 0.1.0\ntype: definition_pack\n"
        "stability: { support_level: tested, executable_code: true }\n",
        encoding="utf-8",
    )
    res = package_definition_pack(p, output_dir=tmp_path / "out")
    assert res.status == "error"


def test_extension_package_rejects_executable_code(tmp_path):
    """schema レベルで弾かれることを再確認"""
    p = tmp_path / "ext.yaml"
    p.write_text(
        "extension_id: a.b\nname: x\nversion: 0.1.0\ntype: definition_pack\n"
        "stability: { support_level: tested, executable_code: true }\n",
        encoding="utf-8",
    )
    res = package_definition_pack(p, output_dir=tmp_path / "out")
    assert res.status == "error"
    # zip は作られない
    out_files = list((tmp_path / "out").glob("*.zip")) if (
        tmp_path / "out"
    ).exists() else []
    assert out_files == []


def test_extension_package_rejects_path_traversal(tmp_path):
    """contents path に .. を含むと package 化されない (validate 段階で)"""
    pack = tmp_path / "bad_pack"
    pack.mkdir()
    (pack / "extension.yaml").write_text(
        "extension_id: a.b\nname: x\nversion: 0.1.0\ntype: definition_pack\n"
        "stability: { support_level: tested, executable_code: false }\n"
        "contents:\n  instruments: ['../outside.yaml']\n",
        encoding="utf-8",
    )
    res = package_definition_pack(
        pack / "extension.yaml", output_dir=tmp_path / "out",
    )
    assert res.status == "error"
    assert any(
        (e.get("details") or {}).get("sub_class")
        in ("extension_path_outside_pack", "extension_validation_failed")
        for e in res.errors
    )


def test_extension_package_excludes_junk(temp_pack):
    """.git / __pycache__ / *.tmp / .DS_Store は package に含めない"""
    pack = temp_pack["pack_dir"]
    (pack / ".git").mkdir()
    (pack / ".git" / "HEAD").write_text("ref: x\n", encoding="utf-8")
    (pack / "__pycache__").mkdir()
    (pack / "__pycache__" / "x.pyc").write_bytes(b"\x00")
    (pack / ".DS_Store").write_bytes(b"\x00")
    (pack / "scratch.tmp").write_text("x", encoding="utf-8")
    res = package_definition_pack(
        temp_pack["pack_yaml"], output_dir=temp_pack["out_dir"],
    )
    assert res.status == "ok"
    with zipfile.ZipFile(res.package_path) as zf:
        names = set(zf.namelist())
    assert not any(n.startswith(".git/") for n in names)
    assert not any(n.startswith("__pycache__/") for n in names)
    assert ".DS_Store" not in names
    assert "scratch.tmp" not in names


def test_extension_package_strict_rejects_verified_without_evidence(tmp_path):
    """instrument が verified なのに validation_evidence 空 + --strict → error"""
    pack = tmp_path / "v_pack"
    pack.mkdir()
    (pack / "extension.yaml").write_text(
        "extension_id: t.v\nname: x\nversion: 0.1.0\ntype: definition_pack\n"
        "stability: { support_level: verified, executable_code: false }\n"
        "contents:\n  instruments: [ instruments/x.yaml ]\n",
        encoding="utf-8",
    )
    (pack / "instruments").mkdir()
    (pack / "instruments" / "x.yaml").write_text(
        "metadata:\n  manufacturer: A\n  model: B\n  category: dmm\n"
        "  support_level: verified\ncommands: {}\n",
        encoding="utf-8",
    )
    # normal: 通る
    res_n = package_definition_pack(
        pack / "extension.yaml", output_dir=tmp_path / "out_n",
    )
    assert res_n.status == "ok"
    # strict: 弾く
    res_s = package_definition_pack(
        pack / "extension.yaml", output_dir=tmp_path / "out_s", strict=True,
    )
    assert res_s.status == "error"
    assert any(
        e["error_class"] == "strict_verified_requires_evidence"
        for e in res_s.errors
    )


def test_extension_package_strict_rejects_missing_readme(tmp_path):
    """pack に README.md が無くて --strict → strict_missing_pack_readme"""
    pack = tmp_path / "noreadme"
    pack.mkdir()
    # v1.6: catalog があるが README.md が無い状態を作る
    # (catalog が無いと strict_missing_catalog_* errors が先に出るため)
    (pack / "extension.yaml").write_text(
        "extension_id: t.nr\nname: x\nversion: 0.1.0\ntype: definition_pack\n"
        "stability: { support_level: tested, executable_code: false }\n"
        "contents:\n  instruments: [ instruments/x.yaml ]\n"
        "catalog:\n  summary: nr pack\n  license: MIT\n",
        encoding="utf-8",
    )
    (pack / "instruments").mkdir()
    (pack / "instruments" / "x.yaml").write_text(
        "metadata:\n  manufacturer: A\n  model: B\n  category: dmm\n"
        "  support_level: tested\ncommands: {}\n",
        encoding="utf-8",
    )
    res = package_definition_pack(
        pack / "extension.yaml", output_dir=tmp_path / "out", strict=True,
    )
    assert res.status == "error"
    assert any(
        e["error_class"] == "strict_missing_pack_readme"
        for e in res.errors
    )


def test_extension_package_normal_warns_missing_readme(tmp_path):
    """normal 時は missing_pack_readme warning"""
    pack = tmp_path / "noreadme"
    pack.mkdir()
    (pack / "extension.yaml").write_text(
        "extension_id: t.nr2\nname: x\nversion: 0.1.0\ntype: definition_pack\n"
        "stability: { support_level: tested, executable_code: false }\n"
        "contents:\n  instruments: [ instruments/x.yaml ]\n",
        encoding="utf-8",
    )
    (pack / "instruments").mkdir()
    (pack / "instruments" / "x.yaml").write_text(
        "metadata:\n  manufacturer: A\n  model: B\n  category: dmm\n"
        "  support_level: tested\ncommands: {}\n",
        encoding="utf-8",
    )
    res = package_definition_pack(
        pack / "extension.yaml", output_dir=tmp_path / "out",
    )
    assert res.status == "ok"
    assert any(
        w["warning_class"] == "missing_pack_readme" for w in res.warnings
    )


# =========================================================
# verify-package
# =========================================================


def test_extension_verify_package_success(temp_pack):
    res = package_definition_pack(
        temp_pack["pack_yaml"], output_dir=temp_pack["out_dir"],
    )
    assert res.status == "ok"
    vrep = verify_extension_package(res.package_path)
    assert vrep.status in ("ok", "warning"), vrep.errors
    assert vrep.extension_id == "visa-mcp.mock.basic"
    assert vrep.version == "0.1.0"
    assert vrep.file_count >= 1
    assert vrep.manifest is not None
    assert vrep.manifest["executable_code"] is False


def test_extension_verify_package_not_found(tmp_path):
    rep = verify_extension_package(tmp_path / "no.zip")
    assert rep.status == "error"
    assert any(e["error_class"] == "not_found" for e in rep.errors)


def test_extension_verify_package_invalid_zip(tmp_path):
    p = tmp_path / "not_a_zip.zip"
    p.write_bytes(b"not a zip file")
    rep = verify_extension_package(p)
    assert rep.status == "error"
    assert any(e["error_class"] == "package_invalid_zip" for e in rep.errors)


def test_extension_verify_package_missing_manifest(temp_pack, tmp_path):
    """package_manifest.json を消したら error"""
    res = package_definition_pack(
        temp_pack["pack_yaml"], output_dir=temp_pack["out_dir"],
    )
    # rebuild zip without package_manifest.json
    broken = tmp_path / "broken.zip"
    with zipfile.ZipFile(res.package_path) as zin, \
         zipfile.ZipFile(broken, "w") as zout:
        for name in zin.namelist():
            if name == "package_manifest.json":
                continue
            zout.writestr(name, zin.read(name))
    rep = verify_extension_package(broken)
    assert rep.status == "error"
    assert any(
        e["error_class"] == "package_missing_required_file"
        for e in rep.errors
    )


def test_extension_verify_package_checksum_mismatch(temp_pack, tmp_path):
    """zip 内 instrument file を改ざんすると mismatch error"""
    res = package_definition_pack(
        temp_pack["pack_yaml"], output_dir=temp_pack["out_dir"],
    )
    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(res.package_path) as zin, \
         zipfile.ZipFile(tampered, "w") as zout:
        for name in zin.namelist():
            data = zin.read(name)
            if name.endswith("mock_psu.yaml"):
                data = data + b"\n# tampered\n"
            zout.writestr(name, data)
    rep = verify_extension_package(tampered)
    assert rep.status == "error"
    assert any(
        e["error_class"] in (
            "package_checksum_mismatch",
            "package_manifest_sha_mismatch",
        )
        for e in rep.errors
    )


def test_extension_verify_package_rejects_zip_slip(tmp_path):
    """zip 内に ../../escape.txt のようなメンバーがあると拒否"""
    p = tmp_path / "evil.zip"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("extension.yaml", "ok")
        zf.writestr("../../escape.txt", "bad")
    rep = verify_extension_package(p)
    assert rep.status == "error"
    assert any(
        e["error_class"] == "package_zip_slip" for e in rep.errors
    )


def test_extension_verify_package_rejects_absolute_path(tmp_path):
    """zip 内に絶対 path member があると拒否"""
    p = tmp_path / "evil2.zip"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("/etc/passwd", "bad")
        zf.writestr("extension.yaml", "ok")
    rep = verify_extension_package(p)
    assert rep.status == "error"
    assert any(
        e["error_class"] == "package_zip_slip" for e in rep.errors
    )


def test_extension_verify_package_rejects_executable_true(temp_pack,
                                                          tmp_path):
    """package_manifest.executable_code=true を error 化"""
    res = package_definition_pack(
        temp_pack["pack_yaml"], output_dir=temp_pack["out_dir"],
    )
    tampered = tmp_path / "exec_true.zip"
    with zipfile.ZipFile(res.package_path) as zin, \
         zipfile.ZipFile(tampered, "w") as zout:
        for name in zin.namelist():
            data = zin.read(name)
            if name == "package_manifest.json":
                m = json.loads(data.decode("utf-8"))
                m["executable_code"] = True
                data = json.dumps(m).encode("utf-8")
            zout.writestr(name, data)
    rep = verify_extension_package(tampered)
    assert rep.status == "error"
    assert any(
        e["error_class"] == "package_executable_code_true"
        for e in rep.errors
    )


# =========================================================
# CLI integration
# =========================================================


def _run_cli(*args: str) -> tuple[int, str, str]:
    result = subprocess.run(
        [sys.executable, "-m", "visa_mcp.cli", *args],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    return result.returncode, result.stdout, result.stderr


def test_cli_extension_package_help():
    rc, out, err = _run_cli("extension", "package", "--help")
    text = out + err
    assert "package" in text
    assert "--strict" in text
    assert "--output" in text


def test_cli_extension_verify_package_help():
    rc, out, err = _run_cli("extension", "verify-package", "--help")
    text = out + err
    assert "verify-package" in text


def test_cli_extension_package_runs(temp_pack):
    rc, out, err = _run_cli(
        "extension", "package",
        str(temp_pack["pack_yaml"]),
        "--output", str(temp_pack["out_dir"]),
        "--json",
    )
    assert rc == 0, err
    data = json.loads(out)
    assert data["package"]["status"] == "ok"
    assert data["package"]["extension_id"] == "visa-mcp.mock.basic"


def test_cli_extension_verify_package_runs(temp_pack):
    res = package_definition_pack(
        temp_pack["pack_yaml"], output_dir=temp_pack["out_dir"],
    )
    rc, out, err = _run_cli(
        "extension", "verify-package", res.package_path, "--json",
    )
    assert rc == 0, err
    data = json.loads(out)
    assert data["verify"]["status"] in ("ok", "warning")
    assert data["verify"]["extension_id"] == "visa-mcp.mock.basic"


# =========================================================
# Repo format
# =========================================================


V15_FILES = [
    "src/lab_executor/extension_packaging.py",
    "src/lab_executor/cli.py",
    "docs/extension_packaging.md",
    "docs/extension_publishing_checklist.md",
    "tests/test_v15_extension_packaging.py",
    "CHANGELOG.md",
]


@pytest.mark.parametrize("rel", V15_FILES)
def test_v15_files_lf_only(rel):
    p = ROOT / rel
    assert p.exists(), f"missing: {p}"
    text = p.read_text(encoding="utf-8")
    assert "\r" not in text


@pytest.mark.parametrize("rel", V15_FILES)
def test_v15_files_multiline(rel):
    p = ROOT / rel
    text = p.read_text(encoding="utf-8")
    assert text.count("\n") + 1 >= 5


# =========================================================
# docs
# =========================================================


def test_extension_packaging_doc_keywords():
    text = (ROOT / "docs" / "extension_packaging.md").read_text(
        encoding="utf-8")
    for kw in (
        "extension package", "verify-package", "package_manifest.json",
        "checksums.sha256", "visa-mcp-extension-package",
        "executable_code", "zip slip", "strict",
        ".visa-mcp-ext.zip",
    ):
        assert kw in text, f"extension_packaging.md に {kw!r} 無し"


def test_publishing_checklist_keywords():
    text = (ROOT / "docs"
            / "extension_publishing_checklist.md").read_text(encoding="utf-8")
    for kw in (
        "reverse-DNS", "SemVer", "executable_code: false",
        "validate extension", "extension package",
        "verify-package", "README.md", "validation_evidence",
    ):
        assert kw in text, f"checklist に {kw!r} 無し"

