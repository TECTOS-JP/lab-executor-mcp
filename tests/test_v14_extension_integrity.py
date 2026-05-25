"""v1.4.0: Installed Definition Pack Integrity tests

提供機能:
  - visa-mcp extension check [extension_id] [--strict]
  - visa-mcp extension inspect <extension_id>
  - visa-mcp extension uninstall <id> --dry-run
  - visa-mcp registry overlay [--source builtin|extension]
  - visa-mcp validate extension <path> --strict
  - metadata.validation_evidence (optional dict)
"""
from __future__ import annotations
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lab_executor import stability
from lab_executor.extension import validate_extension_file
from lab_executor.extension_install import install_definition_pack
from lab_executor.extension_integrity import (
    check_installed_extension, check_all_installed_extensions,
    inspect_installed_extension, uninstall_dry_run,
)

ROOT = Path(__file__).parent.parent


# =========================================================
# Version + MCP surface
# =========================================================


def test_version_v1_4_0():
    import visa_mcp
    assert visa_mcp.__version__.startswith("1.")


def test_no_new_mcp_tools_in_v1_4():
    """v1.4 でも MCP surface は不変"""
    assert stability.stable_count() == 43
    assert stability.experimental_count() == 7
    assert stability.total_documented_count() == 50


def test_stable_tools_unchanged_in_v1_4():
    names = set(stability.stable_tool_names())
    assert "validate_experiment_plan" in names
    assert "list_resources" in names


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
        "src_pack": dst_pack,
    }


def _install(temp_env):
    res = install_definition_pack(
        temp_env["pack_yaml"],
        extensions_dir=temp_env["extensions_dir"],
        lockfile_path=temp_env["lockfile_path"],
    )
    assert res.status == "ok", res.errors
    return res


# =========================================================
# check
# =========================================================


def test_extension_check_ok(temp_env):
    res = _install(temp_env)
    rep = check_installed_extension(
        res.extension_id,
        extensions_dir=temp_env["extensions_dir"],
        lockfile_path=temp_env["lockfile_path"],
    )
    assert rep.integrity == "ok", rep.errors
    assert rep.status == "ok"
    assert rep.files_checked >= 1
    assert not rep.errors


def test_extension_check_checksum_mismatch(temp_env):
    """sha256 drift を検出"""
    res = _install(temp_env)
    target = Path(res.install_path) / "extension.yaml"
    # 内容を変更 (1 byte 追加)
    target.write_text(
        target.read_text(encoding="utf-8") + "\n# drift\n",
        encoding="utf-8",
    )
    rep = check_installed_extension(
        res.extension_id,
        extensions_dir=temp_env["extensions_dir"],
        lockfile_path=temp_env["lockfile_path"],
    )
    assert rep.integrity == "modified"
    assert any(
        e["error_class"] == "extension_checksum_mismatch"
        for e in rep.errors
    )
    # recommended_actions が出る
    assert any(a["action"] == "reinstall" for a in rep.recommended_actions)


def test_extension_check_missing_file(temp_env):
    """記録された file が消えると missing_file"""
    res = _install(temp_env)
    # 任意の instrument file を削除
    inst = Path(res.install_path) / "instruments" / "mock_psu.yaml"
    inst.unlink()
    rep = check_installed_extension(
        res.extension_id,
        extensions_dir=temp_env["extensions_dir"],
        lockfile_path=temp_env["lockfile_path"],
    )
    assert rep.integrity == "missing_file"
    assert any(
        e["error_class"] == "extension_file_missing"
        for e in rep.errors
    )


def test_extension_check_extra_file_warns(temp_env):
    """ユーザーが install path にファイルを足すと warning"""
    res = _install(temp_env)
    extra = Path(res.install_path) / "user_added.txt"
    extra.write_text("hi", encoding="utf-8")
    rep = check_installed_extension(
        res.extension_id,
        extensions_dir=temp_env["extensions_dir"],
        lockfile_path=temp_env["lockfile_path"],
    )
    # extra_file は warning レベル
    assert any(
        w["warning_class"] == "extension_extra_file"
        for w in rep.warnings
    )
    assert rep.integrity in ("extra_file", "ok")
    # 他に error が無ければ status は warning
    assert rep.status in ("warning", "ok")


def test_extension_check_missing_install_meta(temp_env):
    res = _install(temp_env)
    (Path(res.install_path) / ".install_meta.json").unlink()
    rep = check_installed_extension(
        res.extension_id,
        extensions_dir=temp_env["extensions_dir"],
        lockfile_path=temp_env["lockfile_path"],
    )
    assert rep.integrity == "invalid"
    assert any(
        e["error_class"] == "extension_install_meta_missing"
        for e in rep.errors
    )


def test_extension_check_unknown_id(temp_env):
    rep = check_installed_extension(
        "no.such.pack",
        extensions_dir=temp_env["extensions_dir"],
        lockfile_path=temp_env["lockfile_path"],
    )
    assert rep.integrity == "invalid"
    assert any(e["error_class"] == "not_found" for e in rep.errors)


def test_extension_check_all_returns_list(temp_env):
    _install(temp_env)
    reps = check_all_installed_extensions(
        extensions_dir=temp_env["extensions_dir"],
        lockfile_path=temp_env["lockfile_path"],
    )
    assert len(reps) == 1
    assert reps[0].integrity == "ok"


def test_extension_check_strict_promotes_extra_file_to_error(temp_env):
    res = _install(temp_env)
    (Path(res.install_path) / "extra.txt").write_text("x", encoding="utf-8")
    rep = check_installed_extension(
        res.extension_id, strict=True,
        extensions_dir=temp_env["extensions_dir"],
        lockfile_path=temp_env["lockfile_path"],
    )
    assert rep.status == "error"
    assert any(
        e["error_class"].startswith("strict_") for e in rep.errors
    )


# =========================================================
# inspect
# =========================================================


def test_extension_inspect_outputs_contents_summary(temp_env):
    res = _install(temp_env)
    rep = inspect_installed_extension(
        res.extension_id,
        extensions_dir=temp_env["extensions_dir"],
        lockfile_path=temp_env["lockfile_path"],
    )
    d = rep.to_dict()
    assert d["extension_id"] == res.extension_id
    assert d["version"] == "0.1.0"
    assert d["installed_at"]
    assert d["install_path"] == res.install_path
    # contents_summary に instruments の数が出る
    assert d["contents_summary"].get("instruments", 0) >= 1
    assert d["integrity"] == "ok"


def test_extension_inspect_unknown_id(temp_env):
    rep = inspect_installed_extension(
        "no.such.pack",
        extensions_dir=temp_env["extensions_dir"],
        lockfile_path=temp_env["lockfile_path"],
    )
    assert rep.integrity == "invalid"
    assert any(w["warning_class"] == "not_found" for w in rep.warnings)


# =========================================================
# uninstall --dry-run
# =========================================================


def test_uninstall_dry_run_does_not_delete(temp_env):
    res = _install(temp_env)
    data = uninstall_dry_run(
        res.extension_id,
        extensions_dir=temp_env["extensions_dir"],
        lockfile_path=temp_env["lockfile_path"],
    )
    assert data["status"] == "ok"
    assert data["dry_run"] is True
    assert data["extension_id"] == res.extension_id
    assert data["would_remove_path"] == res.install_path
    assert data["would_remove_file_count"] >= 1
    # 実体は残っている
    assert Path(res.install_path).exists()
    # lockfile も残っている
    lock = json.loads(
        temp_env["lockfile_path"].read_text(encoding="utf-8")
    )
    assert len(lock["installed_extensions"]) == 1


def test_uninstall_dry_run_unknown_id(temp_env):
    data = uninstall_dry_run(
        "no.such.pack",
        extensions_dir=temp_env["extensions_dir"],
        lockfile_path=temp_env["lockfile_path"],
    )
    assert data["status"] == "error"


# =========================================================
# --strict validate extension
# =========================================================


def test_validate_extension_strict_empty_contents_error(tmp_path):
    p = tmp_path / "ext.yaml"
    p.write_text(
        "extension_id: a.b\nname: x\nversion: 0.1.0\ntype: definition_pack\n"
        "stability: { support_level: tested, executable_code: false }\n",
        encoding="utf-8",
    )
    # normal: warning
    rep_normal = validate_extension_file(p)
    assert any(w["warning_class"] == "empty_contents"
               for w in rep_normal.warnings)
    assert rep_normal.status == "warning"

    # strict: error
    rep_strict = validate_extension_file(p, strict=True)
    assert rep_strict.status == "error"
    assert any(
        e["error_class"] == "strict_empty_contents"
        for e in rep_strict.errors
    )


def test_validate_extension_strict_verified_requires_evidence(tmp_path):
    """instrument が support_level=verified なのに
    validation_evidence が無いと strict で error"""
    pack = tmp_path / "verified_pack"
    pack.mkdir()
    (pack / "extension.yaml").write_text(
        "extension_id: tectos.verified\nname: x\nversion: 0.1.0\n"
        "type: definition_pack\n"
        "stability: { support_level: verified, executable_code: false }\n"
        "contents:\n  instruments: [ instruments/x.yaml ]\n",
        encoding="utf-8",
    )
    (pack / "instruments").mkdir()
    (pack / "instruments" / "x.yaml").write_text(
        "metadata:\n"
        "  manufacturer: Acme\n  model: X1\n  category: dmm\n"
        "  support_level: verified\n"  # evidence 無し
        "commands: {}\n",
        encoding="utf-8",
    )
    rep = validate_extension_file(pack / "extension.yaml", strict=True)
    assert rep.status == "error"
    assert any(
        e["error_class"] == "strict_verified_requires_evidence"
        for e in rep.errors
    )


def test_validate_extension_strict_draft_is_error(tmp_path):
    """instrument が support_level=draft の場合 strict で error"""
    pack = tmp_path / "draft_pack"
    pack.mkdir()
    (pack / "extension.yaml").write_text(
        "extension_id: tectos.draft\nname: x\nversion: 0.1.0\n"
        "type: definition_pack\n"
        "stability: { support_level: tested, executable_code: false }\n"
        "contents:\n  instruments: [ instruments/x.yaml ]\n",
        encoding="utf-8",
    )
    (pack / "instruments").mkdir()
    (pack / "instruments" / "x.yaml").write_text(
        "metadata:\n"
        "  manufacturer: Acme\n  model: X1\n  category: dmm\n"
        "  support_level: draft\n"
        "commands: {}\n",
        encoding="utf-8",
    )
    rep = validate_extension_file(pack / "extension.yaml", strict=True)
    assert rep.status == "error"
    assert any(
        e["error_class"] == "strict_support_level_draft"
        for e in rep.errors
    )


def test_validate_extension_strict_verified_with_evidence_ok(tmp_path):
    """validation_evidence があれば strict でも error にならない"""
    pack = tmp_path / "ok_pack"
    pack.mkdir()
    (pack / "extension.yaml").write_text(
        "extension_id: tectos.ok\nname: x\nversion: 0.1.0\n"
        "type: definition_pack\n"
        "stability: { support_level: verified, executable_code: false }\n"
        "contents:\n  instruments: [ instruments/x.yaml ]\n",
        encoding="utf-8",
    )
    (pack / "instruments").mkdir()
    (pack / "instruments" / "x.yaml").write_text(
        "metadata:\n"
        "  manufacturer: Acme\n  model: X1\n  category: dmm\n"
        "  support_level: verified\n"
        "  validation_evidence:\n"
        "    tested_by: TECTOS\n"
        "    tested_at: '2026-05-23'\n"
        "    interface: USB\n"
        "    tested_items: [identify]\n"
        "commands: {}\n",
        encoding="utf-8",
    )
    rep = validate_extension_file(pack / "extension.yaml", strict=True)
    assert not any(
        e["error_class"] == "strict_verified_requires_evidence"
        for e in rep.errors
    )


# =========================================================
# validation_evidence field
# =========================================================


def test_metadata_validation_evidence_optional_defaults_empty(tmp_path):
    """metadata.validation_evidence は任意 (default {})"""
    from lab_executor.models.instrument_def import MetadataConfig
    md = MetadataConfig(manufacturer="A", model="B")
    assert md.validation_evidence == {}


def test_metadata_validation_evidence_accepts_dict():
    from lab_executor.models.instrument_def import MetadataConfig
    md = MetadataConfig(
        manufacturer="A", model="B",
        validation_evidence={
            "tested_by": "TECTOS",
            "tested_at": "2026-05-23",
            "interface": "USB",
            "tested_items": ["identify", "set_voltage"],
        },
    )
    assert md.validation_evidence["tested_by"] == "TECTOS"
    assert md.validation_evidence["tested_items"] == [
        "identify", "set_voltage",
    ]


# =========================================================
# CLI integration
# =========================================================


def _run_cli(*args: str, cwd: str | None = None) -> tuple[int, str, str]:
    result = subprocess.run(
        [sys.executable, "-m", "visa_mcp.cli", *args],
        capture_output=True, text=True,
        cwd=cwd or str(ROOT),
    )
    return result.returncode, result.stdout, result.stderr


def test_cli_extension_check_help():
    rc, out, err = _run_cli("extension", "check", "--help")
    text = out + err
    assert "check" in text
    assert "--strict" in text


def test_cli_extension_inspect_help():
    rc, out, err = _run_cli("extension", "inspect", "--help")
    assert "inspect" in (out + err)


def test_cli_extension_uninstall_dry_run_help():
    rc, out, err = _run_cli("extension", "uninstall", "--help")
    assert "--dry-run" in (out + err)


def test_cli_registry_overlay_help():
    rc, out, err = _run_cli("registry", "overlay", "--help")
    text = out + err
    assert "overlay" in text
    assert "--source" in text


def test_cli_registry_overlay_runs_json():
    """visa-mcp registry overlay --json は repo INDEX.yaml を読んで返す"""
    rc, out, err = _run_cli("registry", "overlay", "--json")
    assert rc == 0, err
    data = json.loads(out)
    assert "entries" in data
    assert "builtin_count" in data
    assert "extension_count" in data


def test_cli_validate_extension_strict_help():
    rc, out, err = _run_cli("validate", "--help")
    assert "--strict" in (out + err)


# =========================================================
# Repo format guard
# =========================================================


V14_FILES = [
    "src/visa_mcp/extension_integrity.py",
    "src/visa_mcp/cli.py",
    "src/visa_mcp/extension.py",
    "src/visa_mcp/models/instrument_def.py",
    "docs/extension_integrity.md",
    "tests/test_v14_extension_integrity.py",
    "CHANGELOG.md",
]


@pytest.mark.parametrize("rel", V14_FILES)
def test_v14_files_lf_only(rel):
    p = ROOT / rel
    assert p.exists(), f"missing: {p}"
    text = p.read_text(encoding="utf-8")
    assert "\r" not in text, f"{rel} に CR が含まれる"


@pytest.mark.parametrize("rel", V14_FILES)
def test_v14_files_multiline(rel):
    p = ROOT / rel
    text = p.read_text(encoding="utf-8")
    assert text.count("\n") + 1 >= 5, f"{rel} が 5 行未満"


# =========================================================
# docs
# =========================================================


def test_extension_integrity_doc_keywords():
    text = (ROOT / "docs" / "extension_integrity.md").read_text(
        encoding="utf-8")
    for kw in (
        "extension check", "extension inspect", "registry overlay",
        "validation_evidence", "strict", "sha256",
        "extension_checksum_mismatch", "missing_file", "extra_file",
        "modified", "recommended_actions",
    ):
        assert kw in text, f"extension_integrity.md に {kw!r} 無し"


def test_changelog_has_v140_entry():
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "v1.4.0" in text
    assert "extension_integrity" in text or "extension check" in text
    assert "validation_evidence" in text
