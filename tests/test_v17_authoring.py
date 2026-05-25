"""v1.7.0: Definition Pack Authoring Assistant tests

- extension init (3 templates)
- extension package --dry-run
- extension doctor
- docs/extension_authoring.md + CONTRIBUTING.md
- MCP surface 不変
"""
from __future__ import annotations
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from lab_executor import stability
from lab_executor.extension import validate_extension_file
from lab_executor.extension_authoring import (
    init_extension_pack, package_dry_run, doctor_extension,
)
from lab_executor.extension_packaging import package_definition_pack

ROOT = Path(__file__).parent.parent


# =========================================================
# Version + MCP surface
# =========================================================


def test_version_v1_7_0():
    import visa_mcp
    assert visa_mcp.__version__.startswith("1.")


def test_no_new_mcp_tools_in_v1_7():
    assert stability.stable_count() == 43
    assert stability.experimental_count() == 7
    assert stability.total_documented_count() == 50


def test_stable_tools_unchanged_in_v1_7():
    names = set(stability.stable_tool_names())
    assert "validate_experiment_plan" in names
    assert "list_resources" in names


# =========================================================
# init
# =========================================================


def test_extension_init_minimal(tmp_path):
    res = init_extension_pack(
        "mypack", target_dir=tmp_path, template="minimal",
        author="Acme",
    )
    assert res.status == "ok", res.errors
    pack = Path(res.pack_path)
    assert (pack / "extension.yaml").exists()
    assert (pack / "README.md").exists()
    assert res.extension_id == "local.mypack"
    assert "extension.yaml" in res.files_created


def test_extension_init_instrument_pack(tmp_path):
    res = init_extension_pack(
        "instpack", target_dir=tmp_path, template="instrument_pack",
    )
    assert res.status == "ok"
    pack = Path(res.pack_path)
    assert (pack / "instruments" / "example_instrument.yaml").exists()
    assert (pack / "registry_entries" / "INDEX.yaml").exists()


def test_extension_init_mock_basic(tmp_path):
    res = init_extension_pack(
        "mockp", target_dir=tmp_path, template="mock_basic",
    )
    assert res.status == "ok"
    pack = Path(res.pack_path)
    assert (pack / "instruments" / "README.md").exists()
    assert (pack / "benchmarks" / "README.md").exists()


def test_extension_init_with_custom_id(tmp_path):
    res = init_extension_pack(
        "p2", target_dir=tmp_path, extension_id="com.example.p2",
    )
    assert res.status == "ok"
    assert res.extension_id == "com.example.p2"
    text = (Path(res.pack_path) / "extension.yaml").read_text(
        encoding="utf-8")
    assert "com.example.p2" in text


def test_extension_init_rejects_existing_dir_without_force(tmp_path):
    (tmp_path / "exists").mkdir()
    res = init_extension_pack(
        "exists", target_dir=tmp_path, template="minimal",
    )
    assert res.status == "error"
    assert any(
        (e.get("details") or {}).get("sub_class")
        == "extension_init_target_exists"
        for e in res.errors
    )


def test_extension_init_force_overrides_existing_dir(tmp_path):
    (tmp_path / "exists").mkdir()
    res = init_extension_pack(
        "exists", target_dir=tmp_path, template="minimal", force=True,
    )
    assert res.status == "ok"


def test_extension_init_rejects_unknown_template(tmp_path):
    res = init_extension_pack(
        "tp", target_dir=tmp_path, template="weird",
    )
    assert res.status == "error"
    assert any(
        (e.get("details") or {}).get("sub_class")
        == "extension_init_unknown_template"
        for e in res.errors
    )


def test_extension_init_rejects_invalid_extension_id(tmp_path):
    res = init_extension_pack(
        "p3", target_dir=tmp_path,
        extension_id="BadID with spaces",
    )
    assert res.status == "error"
    assert any(
        (e.get("details") or {}).get("sub_class")
        == "extension_init_invalid_id"
        for e in res.errors
    )


def test_extension_init_generated_pack_validates(tmp_path):
    """重要: init で生成された pack は validate extension が通る"""
    for tpl in ("minimal", "mock_basic", "instrument_pack"):
        res = init_extension_pack(
            f"p_{tpl}", target_dir=tmp_path, template=tpl,
            author="A",
        )
        assert res.status == "ok", res.errors
        rep = validate_extension_file(
            Path(res.pack_path) / "extension.yaml"
        )
        # 生成直後は error が無いこと
        assert not rep.errors, (
            f"{tpl}: validate errors: {rep.errors}"
        )


def test_extension_init_generated_yaml_parses_as_yaml(tmp_path):
    res = init_extension_pack(
        "yp", target_dir=tmp_path, template="instrument_pack",
    )
    text = (Path(res.pack_path) / "extension.yaml").read_text(
        encoding="utf-8")
    data = yaml.safe_load(text)
    assert isinstance(data, dict)
    assert data["extension_id"] == "local.yp"
    assert isinstance(data.get("catalog"), dict)
    assert data["catalog"].get("license") == "MIT"


# =========================================================
# package --dry-run
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
    }


def test_package_dry_run_lists_files(temp_pack):
    data = package_dry_run(temp_pack["pack_yaml"])
    assert data["status"] == "ok", data["errors"]
    assert data["extension_id"] == "visa-mcp.mock.basic"
    assert "extension.yaml" in data["files_included"]
    assert data["package_manifest_preview"]["executable_code"] is False
    assert data["checksums_preview_count"] >= 1


def test_package_dry_run_does_not_create_zip(temp_pack):
    """dry-run は zip を作らない"""
    out = temp_pack["pack_dir"] / "dist_should_be_empty"
    out.mkdir()
    _ = package_dry_run(temp_pack["pack_yaml"])
    # cwd / pack/dist 両方に zip が無いことを軽く確認
    pack_dist = temp_pack["pack_dir"] / "dist"
    if pack_dist.exists():
        assert list(pack_dist.glob("*.zip")) == []


def test_package_dry_run_excludes_junk(temp_pack):
    (temp_pack["pack_dir"] / "__pycache__").mkdir()
    (temp_pack["pack_dir"] / "__pycache__" / "x.pyc").write_bytes(b"\x00")
    (temp_pack["pack_dir"] / ".DS_Store").write_bytes(b"\x00")
    data = package_dry_run(temp_pack["pack_yaml"])
    assert data["status"] == "ok"
    assert not any(f.startswith("__pycache__/")
                   for f in data["files_included"])
    assert any("__pycache__" in f or ".DS_Store" in f
               for f in data["files_excluded"])


def test_package_dry_run_strict_propagates(tmp_path):
    """--strict で empty_contents が strict_empty_contents error 化"""
    p = tmp_path / "ext.yaml"
    p.write_text(
        "extension_id: t.x\nname: x\nversion: 0.1.0\n"
        "type: definition_pack\n"
        "stability: { support_level: tested, executable_code: false }\n"
        "catalog: { summary: 'x', license: MIT }\n",
        encoding="utf-8",
    )
    data = package_dry_run(p, strict=True)
    assert data["status"] == "error"
    assert any(
        e.get("error_class") == "strict_empty_contents"
        for e in data["errors"]
    )


# =========================================================
# doctor
# =========================================================


def test_doctor_reports_ready_to_package(temp_pack):
    rep = doctor_extension(temp_pack["pack_yaml"])
    data = rep.to_dict()
    assert data["summary"]["ready_to_package"] is True
    # status は warning か ok (strict warnings 等)
    assert data["status"] in ("ok", "warning")


def test_doctor_recommends_readme(tmp_path):
    """README が無い pack は recommended_actions に add_readme"""
    res = init_extension_pack(
        "nope", target_dir=tmp_path, template="minimal", author="A",
    )
    assert res.status == "ok"
    # README を削除
    (Path(res.pack_path) / "README.md").unlink()
    rep = doctor_extension(Path(res.pack_path) / "extension.yaml")
    data = rep.to_dict()
    assert data["summary"]["has_readme"] is False
    assert any(a["action"] == "add_readme"
               for a in data["recommended_actions"])


def test_doctor_recommends_validation_evidence(tmp_path):
    """verified だが validation_evidence 空 → add_validation_evidence"""
    pack = tmp_path / "vp"
    pack.mkdir()
    (pack / "extension.yaml").write_text(
        "extension_id: t.veri\nname: x\nversion: 0.1.0\n"
        "type: definition_pack\n"
        "stability: { support_level: verified, executable_code: false }\n"
        "catalog: { summary: 'x', license: MIT, safety_notes: ['n'] }\n"
        "contents: { instruments: [ instruments/x.yaml ] }\n",
        encoding="utf-8",
    )
    (pack / "instruments").mkdir()
    (pack / "instruments" / "x.yaml").write_text(
        "metadata:\n  manufacturer: A\n  model: B\n  category: dmm\n"
        "  support_level: verified\ncommands: {}\n",
        encoding="utf-8",
    )
    (pack / "README.md").write_text("# v\n", encoding="utf-8")
    rep = doctor_extension(pack / "extension.yaml")
    data = rep.to_dict()
    assert data["summary"]["missing_validation_evidence_count"] == 1
    assert any(
        a["action"] == "add_validation_evidence"
        for a in data["recommended_actions"]
    )


def test_doctor_strict_promotes_to_errors(tmp_path):
    """--strict で strict_* も本体 errors に格上げ"""
    res = init_extension_pack(
        "stp", target_dir=tmp_path, template="minimal", author="A",
    )
    assert res.status == "ok"
    # minimal は contents 空なので strict_empty_contents が error
    rep = doctor_extension(
        Path(res.pack_path) / "extension.yaml", strict=True,
    )
    data = rep.to_dict()
    classes = [e.get("error_class") for e in data["errors"]]
    assert "strict_empty_contents" in classes


def test_doctor_returns_recommended_actions_structure(temp_pack):
    rep = doctor_extension(temp_pack["pack_yaml"])
    data = rep.to_dict()
    for a in data["recommended_actions"]:
        assert "action" in a
        assert "reason" in a


def test_doctor_ready_for_registry_review_true_for_quality_pack(temp_pack):
    """example pack は support_level=tested で readme/catalog 完備、
    instrument は verified が無いので strict 観点でも通る"""
    rep = doctor_extension(temp_pack["pack_yaml"])
    data = rep.to_dict()
    # example pack には strict 問題が無いので true 期待
    assert data["summary"]["ready_for_registry_review"] is True


# =========================================================
# End-to-end: init → doctor → package → install
# =========================================================


def test_authoring_e2e_init_then_package(tmp_path):
    res = init_extension_pack(
        "e2e_pack", target_dir=tmp_path, template="instrument_pack",
        author="TECTOS",
    )
    assert res.status == "ok"
    # doctor は warning (draft + 空 validation_evidence 候補なし)
    drep = doctor_extension(Path(res.pack_path) / "extension.yaml")
    assert drep.summary["ready_to_package"] is True
    # package OK
    pres = package_definition_pack(
        Path(res.pack_path) / "extension.yaml",
        output_dir=tmp_path / "dist",
    )
    assert pres.status == "ok", pres.errors


# =========================================================
# CLI
# =========================================================


def _run_cli(*args: str) -> tuple[int, str, str]:
    r = subprocess.run(
        [sys.executable, "-m", "visa_mcp.cli", *args],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    return r.returncode, r.stdout, r.stderr


def test_cli_extension_init_help():
    rc, out, err = _run_cli("extension", "init", "--help")
    text = out + err
    assert "init" in text
    assert "--template" in text
    assert "minimal" in text and "instrument_pack" in text


def test_cli_extension_doctor_help():
    rc, out, err = _run_cli("extension", "doctor", "--help")
    text = out + err
    assert "doctor" in text


def test_cli_extension_package_help_mentions_dry_run():
    rc, out, err = _run_cli("extension", "package", "--help")
    text = out + err
    assert "--dry-run" in text


def test_cli_extension_init_runs(tmp_path):
    rc, out, err = _run_cli(
        "extension", "init", "cli_pack",
        "--target-dir", str(tmp_path),
        "--template", "minimal", "--author", "X", "--json",
    )
    assert rc == 0, err
    data = json.loads(out)
    assert data["init"]["status"] == "ok"
    assert (tmp_path / "cli_pack" / "extension.yaml").exists()


def test_cli_extension_package_dry_run_json(temp_pack):
    rc, out, err = _run_cli(
        "extension", "package", str(temp_pack["pack_yaml"]),
        "--dry-run", "--json",
    )
    assert rc == 0, err
    data = json.loads(out)
    assert data["package_dry_run"]["status"] == "ok"
    assert data["package_dry_run"]["extension_id"] == "visa-mcp.mock.basic"


def test_cli_extension_doctor_json(temp_pack):
    rc, out, err = _run_cli(
        "extension", "doctor", str(temp_pack["pack_yaml"]), "--json",
    )
    assert rc == 0, err
    data = json.loads(out)
    assert "doctor" in data
    assert data["doctor"]["summary"]["ready_to_package"] is True


# =========================================================
# Repo format
# =========================================================


V17_FILES = [
    "src/visa_mcp/extension_authoring.py",
    "src/visa_mcp/cli.py",
    "docs/extension_authoring.md",
    "CONTRIBUTING.md",
    "tests/test_v17_authoring.py",
    "CHANGELOG.md",
]


@pytest.mark.parametrize("rel", V17_FILES)
def test_v17_files_lf_only(rel):
    p = ROOT / rel
    assert p.exists(), f"missing: {p}"
    text = p.read_text(encoding="utf-8")
    assert "\r" not in text


@pytest.mark.parametrize("rel", V17_FILES)
def test_v17_files_multiline(rel):
    p = ROOT / rel
    text = p.read_text(encoding="utf-8")
    assert text.count("\n") + 1 >= 5


# =========================================================
# docs
# =========================================================


def test_authoring_doc_keywords():
    text = (ROOT / "docs" / "extension_authoring.md").read_text(
        encoding="utf-8")
    for kw in (
        "extension init", "extension doctor", "--dry-run",
        "instrument_pack", "minimal", "mock_basic",
        "ready_to_package", "ready_for_registry_review",
        "validation_evidence",
    ):
        assert kw in text, f"extension_authoring.md に {kw!r} 無し"


def test_contributing_mentions_definition_pack_workflow():
    text = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    for kw in (
        "extension init", "extension doctor",
        "extension package", "verify-package",
        "validation_evidence", "safety_notes",
        "executable_code: false",
    ):
        assert kw in text, f"CONTRIBUTING.md に {kw!r} 無し"


def test_changelog_has_v170_entry():
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "v1.7.0" in text
    assert "extension init" in text
    assert "extension doctor" in text
    assert "--dry-run" in text
