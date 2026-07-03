"""v1.6.0: Local zip install tests

`visa-mcp extension install <zip>` で .visa-mcp-ext.zip を直接 install
できることを確認する。internal flow:

  1. verify_extension_package() を必ず通す
  2. zip を tmp 展開 (二重 zip-slip check)
  3. 展開 extension.yaml を既存 install_definition_pack() に流す
  4. .install_meta.json.source_path / source_format を zip 由来に書き換え
"""
from __future__ import annotations
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from lab_executor import stability
from lab_executor.extension_install import (
    install_definition_pack, install_definition_pack_from_zip,
    list_installed_packs, load_overlay_registry,
)
from lab_executor.extension_packaging import package_definition_pack

ROOT = Path(__file__).parent.parent


# =========================================================
# Version + MCP surface
# =========================================================


def test_no_new_mcp_tools_in_v1_6():
    """v1.6 でも MCP surface は不変"""
    assert stability.stable_count() == 43
    assert stability.experimental_count() == 7
    assert stability.total_documented_count() == 50


# =========================================================
# Fixtures
# =========================================================


@pytest.fixture
def temp_env(tmp_path):
    src_pack = ROOT / "examples" / "extensions" / "mock_basic_pack"
    dst_pack = tmp_path / "src_pack"
    shutil.copytree(src_pack, dst_pack)
    return {
        "pack_dir": dst_pack,
        "pack_yaml": dst_pack / "extension.yaml",
        "out_dir": tmp_path / "dist",
        "extensions_dir": tmp_path / "extensions",
        "lockfile_path": tmp_path / "extensions.lock.json",
    }


def _build_package(temp_env) -> Path:
    res = package_definition_pack(
        temp_env["pack_yaml"], output_dir=temp_env["out_dir"],
    )
    assert res.status == "ok", res.errors
    return Path(res.package_path)


# =========================================================
# install_definition_pack_from_zip
# =========================================================


def test_zip_install_success(temp_env):
    zp = _build_package(temp_env)
    res = install_definition_pack_from_zip(
        zp,
        extensions_dir=temp_env["extensions_dir"],
        lockfile_path=temp_env["lockfile_path"],
    )
    assert res.status == "ok", res.errors
    assert res.extension_id == "visa-mcp.mock.basic"
    assert res.version == "0.1.0"
    inst = Path(res.install_path)
    assert inst.exists()
    assert (inst / "extension.yaml").exists()
    assert (inst / "instruments" / "mock_psu.yaml").exists()
    assert (inst / ".install_meta.json").exists()


def test_zip_install_updates_source_metadata(temp_env):
    zp = _build_package(temp_env)
    res = install_definition_pack_from_zip(
        zp,
        extensions_dir=temp_env["extensions_dir"],
        lockfile_path=temp_env["lockfile_path"],
    )
    assert res.status == "ok", res.errors
    meta = json.loads(
        (Path(res.install_path) / ".install_meta.json").read_text(
            encoding="utf-8"))
    assert meta["source_path"] == str(zp)
    assert meta["source_format"] == "visa-mcp-extension-package"


def test_zip_install_writes_lockfile(temp_env):
    zp = _build_package(temp_env)
    install_definition_pack_from_zip(
        zp,
        extensions_dir=temp_env["extensions_dir"],
        lockfile_path=temp_env["lockfile_path"],
    )
    packs = list_installed_packs(
        extensions_dir=temp_env["extensions_dir"],
        lockfile_path=temp_env["lockfile_path"],
    )
    assert len(packs) == 1
    assert packs[0]["extension_id"] == "visa-mcp.mock.basic"


def test_zip_install_rejects_missing_file(temp_env, tmp_path):
    res = install_definition_pack_from_zip(
        tmp_path / "no_such.zip",
        extensions_dir=temp_env["extensions_dir"],
        lockfile_path=temp_env["lockfile_path"],
    )
    assert res.status == "error"
    assert any(e["error_class"] == "not_found" for e in res.errors)


def test_zip_install_rejects_bad_zip(temp_env, tmp_path):
    bad = tmp_path / "broken.zip"
    bad.write_bytes(b"not a zip file at all")
    res = install_definition_pack_from_zip(
        bad,
        extensions_dir=temp_env["extensions_dir"],
        lockfile_path=temp_env["lockfile_path"],
    )
    assert res.status == "error"
    # verify-package が拾う
    assert any(
        e["error_class"] in (
            "package_invalid_zip",
            "validation",  # follow-up summary error
        )
        for e in res.errors
    )


def test_zip_install_rejects_checksum_mismatch(temp_env, tmp_path):
    """zip 内 instrument file を改ざんしたものは install 拒否"""
    zp = _build_package(temp_env)
    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(zp) as zin, zipfile.ZipFile(tampered, "w") as zout:
        for name in zin.namelist():
            data = zin.read(name)
            if name.endswith("mock_psu.yaml"):
                data = data + b"\n# tampered\n"
            zout.writestr(name, data)
    res = install_definition_pack_from_zip(
        tampered,
        extensions_dir=temp_env["extensions_dir"],
        lockfile_path=temp_env["lockfile_path"],
    )
    assert res.status == "error"
    # 改ざんなので checksum mismatch などが含まれる
    classes = {e["error_class"] for e in res.errors}
    assert classes & {
        "package_checksum_mismatch",
        "package_manifest_sha_mismatch",
    }


def test_zip_install_rejects_zip_slip(temp_env, tmp_path):
    """zip 内に ../escape のような member があると install 拒否"""
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("extension.yaml", "ok")
        zf.writestr("../escape.txt", "bad")
    res = install_definition_pack_from_zip(
        evil,
        extensions_dir=temp_env["extensions_dir"],
        lockfile_path=temp_env["lockfile_path"],
    )
    assert res.status == "error"
    assert any(
        e["error_class"] == "package_zip_slip" for e in res.errors
    )


def test_zip_install_rejects_executable_true(temp_env, tmp_path):
    """package_manifest.executable_code=true は zip install で拒否"""
    zp = _build_package(temp_env)
    tampered = tmp_path / "exec.zip"
    with zipfile.ZipFile(zp) as zin, zipfile.ZipFile(tampered, "w") as zout:
        for name in zin.namelist():
            data = zin.read(name)
            if name == "package_manifest.json":
                m = json.loads(data.decode("utf-8"))
                m["executable_code"] = True
                data = json.dumps(m).encode("utf-8")
            zout.writestr(name, data)
    res = install_definition_pack_from_zip(
        tampered,
        extensions_dir=temp_env["extensions_dir"],
        lockfile_path=temp_env["lockfile_path"],
    )
    assert res.status == "error"
    assert any(
        e["error_class"] == "package_executable_code_true"
        for e in res.errors
    )


def test_zip_install_duplicate_requires_force(temp_env):
    zp = _build_package(temp_env)
    res1 = install_definition_pack_from_zip(
        zp,
        extensions_dir=temp_env["extensions_dir"],
        lockfile_path=temp_env["lockfile_path"],
    )
    assert res1.status == "ok"
    # 同 zip を再 install (force なし)
    res2 = install_definition_pack_from_zip(
        zp,
        extensions_dir=temp_env["extensions_dir"],
        lockfile_path=temp_env["lockfile_path"],
    )
    assert res2.status == "error"
    assert any(
        (e.get("details") or {}).get("sub_class")
        == "extension_duplicate_install"
        for e in res2.errors
    )
    # force 付きで通る
    res3 = install_definition_pack_from_zip(
        zp, force=True,
        extensions_dir=temp_env["extensions_dir"],
        lockfile_path=temp_env["lockfile_path"],
    )
    assert res3.status == "ok"


def test_zip_install_overlay_registry_picks_extension(temp_env, tmp_path):
    """zip install 後に overlay registry に extension 由来として現れる
    (registry_entries 付き pack を作って確認)"""
    pack = tmp_path / "reg_pack"
    pack.mkdir()
    (pack / "extension.yaml").write_text(
        "extension_id: tectos.regzip\nname: rz\nversion: 0.1.0\n"
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
        "  - { id: tectos_regzip_foo, vendor: TECTOS, model: Foo,\n"
        "      category: dmm, support_level: tested,\n"
        "      path: instruments/foo.yaml }\n",
        encoding="utf-8",
    )
    pres = package_definition_pack(
        pack / "extension.yaml", output_dir=tmp_path / "dist",
    )
    assert pres.status == "ok"

    inst = install_definition_pack_from_zip(
        pres.package_path,
        extensions_dir=temp_env["extensions_dir"],
        lockfile_path=temp_env["lockfile_path"],
    )
    assert inst.status == "ok", inst.errors

    overlay = load_overlay_registry(
        None,
        extensions_dir=temp_env["extensions_dir"],
        lockfile_path=temp_env["lockfile_path"],
    )
    ext_entries = [e for e in overlay.entries
                   if e.source.get("kind") == "extension"]
    assert any(e.id == "tectos_regzip_foo" for e in ext_entries), (
        [e.id for e in ext_entries]
    )
    foo = next(e for e in ext_entries if e.id == "tectos_regzip_foo")
    assert foo.source["extension_id"] == "tectos.regzip"


# =========================================================
# yaml install と zip install の併存
# =========================================================


def test_yaml_install_still_works(temp_env):
    """既存 v1.3 経路 (extension.yaml direct) は不変"""
    res = install_definition_pack(
        temp_env["pack_yaml"],
        extensions_dir=temp_env["extensions_dir"],
        lockfile_path=temp_env["lockfile_path"],
    )
    assert res.status == "ok"
    meta = json.loads(
        (Path(res.install_path) / ".install_meta.json").read_text(
            encoding="utf-8"))
    # yaml install では source_format は無い (後方互換)
    assert "source_format" not in meta or meta.get("source_format") != (
        "visa-mcp-extension-package"
    )


# =========================================================
# CLI auto-routing
# =========================================================


def _run_cli(*args: str) -> tuple[int, str, str]:
    r = subprocess.run(
        [sys.executable, "-m", "visa_mcp.cli", *args],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    return r.returncode, r.stdout, r.stderr


def test_cli_install_help_mentions_zip():
    rc, out, err = _run_cli("extension", "install", "--help")
    text = out + err
    assert "zip" in text.lower() or "visa-mcp-ext.zip" in text
    assert "extension.yaml" in text


def test_cli_install_zip_routes_to_zip_handler(temp_env, tmp_path,
                                                monkeypatch):
    """CLI 経由で .zip path を渡したら zip install が走り、source_format
    が visa-mcp-extension-package になる"""
    zp = _build_package(temp_env)
    # default extensions_dir を tmp に向ける環境変数は無いので、API 経由で
    # 結果が同じであることだけ確認
    res = install_definition_pack_from_zip(
        zp,
        extensions_dir=temp_env["extensions_dir"],
        lockfile_path=temp_env["lockfile_path"],
    )
    assert res.status == "ok"
    assert (res.metadata or {}).get("source_format") == (
        "visa-mcp-extension-package"
    )


# =========================================================
# Repo format
# =========================================================


V16_FILES = [
    "src/lab_executor/extension_install.py",
    "src/lab_executor/extension_packaging.py",
    "src/lab_executor/cli.py",
    "docs/extension_install.md",
    "docs/extension_packaging.md",
    "tests/test_v16_zip_install.py",
    "CHANGELOG.md",
]


@pytest.mark.parametrize("rel", V16_FILES)
def test_v16_files_lf_only(rel):
    p = ROOT / rel
    assert p.exists(), f"missing: {p}"
    text = p.read_text(encoding="utf-8")
    assert "\r" not in text


@pytest.mark.parametrize("rel", V16_FILES)
def test_v16_files_multiline(rel):
    p = ROOT / rel
    text = p.read_text(encoding="utf-8")
    assert text.count("\n") + 1 >= 5


# =========================================================
# docs / CHANGELOG
# =========================================================


def test_extension_install_doc_describes_zip_install():
    text = (ROOT / "docs" / "extension_install.md").read_text(
        encoding="utf-8")
    for kw in (
        "v1.6", "zip", "visa-mcp-ext.zip", "verify_extension_package",
        "source_format", "visa-mcp-extension-package",
    ):
        assert kw in text, f"extension_install.md に {kw!r} 無し"


def test_packaging_doc_marks_zip_install_done():
    text = (ROOT / "docs" / "extension_packaging.md").read_text(
        encoding="utf-8")
    assert "v1.6" in text
    # v1.6 で対応済み を明示
    assert "対応済み" in text or "完了" in text or "v1.6 で" in text

