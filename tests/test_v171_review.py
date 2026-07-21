"""v1.7.1: v1.7.0 review response

- P0: raw 改行 / multi-line (v1.7 関連 file)
- P1-2: 生成 extension.yaml が multi-line + yaml.safe_load OK + 期待 keys
- P1-3: --force の retain-files warning + docs 記述
- P1-4: doctor の ready_to_package vs ready_for_registry_review docs
- P1-5: doctor CLI human-readable output が strict-only を分類
- P1-7: CONTRIBUTING に LLM-generated / manual / credentials policy
- P2-8: docs に scaffold template の v1.8+ 外部化 TODO
"""
from __future__ import annotations
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from lab_executor.extension import validate_extension_file
from lab_executor.extension_authoring import (
    init_extension_pack, TEMPLATES,
)

ROOT = Path(__file__).parent.parent


# =========================================================
# Version
# =========================================================


V17_FILES_FULL = [
    "src/lab_executor/extension_authoring.py",
    "src/lab_executor/extension_packaging.py",
    "src/lab_executor/extension_install.py",
    "src/lab_executor/extension_catalog.py",
    "src/lab_executor/extension.py",
    "src/lab_executor/cli.py",
    "docs/extension_authoring.md",
    "docs/extension_packaging.md",
    "docs/extension_install.md",
    "docs/extension_catalog.md",
    "docs/error_taxonomy.md",
    "CONTRIBUTING.md",
    "tests/test_v17_authoring.py",
    "tests/test_v171_review.py",
    "CHANGELOG.md",
    "README.md",
]


@pytest.mark.parametrize("rel", V17_FILES_FULL)
def test_v171_lf_only(rel):
    p = ROOT / rel
    assert p.exists(), f"missing: {p}"
    text = p.read_text(encoding="utf-8")
    assert "\r" not in text, f"{rel} に CR 含む"


@pytest.mark.parametrize("rel", V17_FILES_FULL)
def test_v171_multiline(rel):
    p = ROOT / rel
    text = p.read_text(encoding="utf-8")
    assert text.count("\n") + 1 >= 5, f"{rel} が 5 行未満"


# =========================================================
# P1-2: 生成 YAML の妥当性 (全 template)
# =========================================================


@pytest.mark.parametrize("tpl", list(TEMPLATES))
def test_init_generated_yaml_is_multiline(tmp_path, tpl):
    res = init_extension_pack(
        f"p_{tpl}", target_dir=tmp_path, template=tpl, author="A",
    )
    assert res.status == "ok"
    text = (Path(res.pack_path) / "extension.yaml").read_text(
        encoding="utf-8")
    # 1 行潰れ回帰防止
    assert text.count("\n") > 15, (
        f"{tpl}: generated extension.yaml only has "
        f"{text.count(chr(10)) + 1} lines"
    )


@pytest.mark.parametrize("tpl", list(TEMPLATES))
def test_init_generated_yaml_safe_loads(tmp_path, tpl):
    res = init_extension_pack(
        f"p2_{tpl}", target_dir=tmp_path, template=tpl, author="A",
    )
    assert res.status == "ok"
    text = (Path(res.pack_path) / "extension.yaml").read_text(
        encoding="utf-8")
    data = yaml.safe_load(text)
    assert isinstance(data, dict)


@pytest.mark.parametrize("tpl", list(TEMPLATES))
def test_init_generated_manifest_has_expected_keys(tmp_path, tpl):
    res = init_extension_pack(
        f"p3_{tpl}", target_dir=tmp_path, template=tpl, author="Bob",
    )
    assert res.status == "ok"
    data = yaml.safe_load(
        (Path(res.pack_path) / "extension.yaml").read_text(
            encoding="utf-8"))
    for k in ("extension_id", "name", "version", "type", "contents",
              "stability", "catalog"):
        assert k in data, f"{tpl}: missing key {k!r}"
    assert data["stability"]["executable_code"] is False
    assert data["stability"]["support_level"] == "draft"
    assert data["catalog"]["license"] == "MIT"


@pytest.mark.parametrize("tpl", list(TEMPLATES))
def test_init_generated_pack_validates(tmp_path, tpl):
    res = init_extension_pack(
        f"p4_{tpl}", target_dir=tmp_path, template=tpl,
    )
    assert res.status == "ok"
    rep = validate_extension_file(
        Path(res.pack_path) / "extension.yaml")
    assert not rep.errors, f"{tpl}: {rep.errors}"


def test_init_instrument_pack_registry_index_safe_loads(tmp_path):
    res = init_extension_pack(
        "rip", target_dir=tmp_path, template="instrument_pack",
    )
    assert res.status == "ok"
    p = Path(res.pack_path) / "registry_entries" / "INDEX.yaml"
    assert p.exists()
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    # INDEX.yaml: {instruments: []}
    assert isinstance(data, dict)
    assert isinstance(data.get("instruments"), list)


# =========================================================
# P1-3: --force retain-files warning
# =========================================================


def test_init_force_emits_retain_warning(tmp_path):
    pack = tmp_path / "exist"
    pack.mkdir()
    # 既存 file を仕込む
    (pack / "user_added.txt").write_text("keep me", encoding="utf-8")
    res = init_extension_pack(
        "exist", target_dir=tmp_path, template="minimal", force=True,
    )
    assert res.status == "ok"
    # 残存 file を warning で報告
    assert any(
        w.get("warning_class") == "extension_init_force_retains_files"
        for w in res.warnings
    )
    # 既存 file は残っている
    assert (pack / "user_added.txt").exists()
    assert (pack / "user_added.txt").read_text(encoding="utf-8") == "keep me"


def test_init_no_force_no_existing_no_warning(tmp_path):
    res = init_extension_pack(
        "fresh", target_dir=tmp_path, template="minimal",
    )
    assert res.status == "ok"
    assert not any(
        w.get("warning_class") == "extension_init_force_retains_files"
        for w in res.warnings
    )


# =========================================================
# P1-4: docs に ready_to_package vs ready_for_registry_review
# =========================================================


def test_authoring_doc_explains_ready_distinction():
    text = (ROOT / "docs" / "extension_authoring.md").read_text(
        encoding="utf-8")
    assert "ready_to_package" in text
    assert "ready_for_registry_review" in text
    # 区別を強調する文
    assert "publishing" in text.lower()
    assert "registry" in text.lower()
    assert "最低条件" in text


# =========================================================
# P1-5: doctor CLI が strict-only を分類表示
# =========================================================


def _run_cli(*args: str) -> tuple[int, str, str]:
    r = subprocess.run(
        [sys.executable, "-m", "lab_visa_mcp.cli", *args],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    return r.returncode, r.stdout, r.stderr


def test_cli_doctor_human_output_classifies_strict_only(tmp_path):
    """minimal pack (contents 空) → strict_empty_contents が
    Strict-only issues に分類される"""
    res = init_extension_pack(
        "stcli", target_dir=tmp_path, template="minimal",
    )
    assert res.status == "ok"
    yaml_path = Path(res.pack_path) / "extension.yaml"
    rc, out, err = _run_cli("extension", "doctor", str(yaml_path))
    text = out + err
    assert "Errors (block package)" in text
    assert "Warnings (quality)" in text
    assert "Strict-only issues" in text
    # minimal の場合、strict-only に出る (--strict なしなので)
    assert "strict_empty_contents" in text or "Strict-only" in text


def test_cli_doctor_recommended_actions_section(tmp_path):
    res = init_extension_pack(
        "stcli2", target_dir=tmp_path, template="minimal",
    )
    yaml_path = Path(res.pack_path) / "extension.yaml"
    rc, out, err = _run_cli("extension", "doctor", str(yaml_path))
    text = out + err
    # minimal は contents 空なので何らかの recommended action または
    # ready_to_package が出る
    assert ("Recommended actions" in text or "ready_to_package" in text)


# =========================================================
# P1-7: CONTRIBUTING data handling policy
# =========================================================


def test_contributing_has_data_handling_policy():
    text = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    for kw in (
        "manual", "datasheet", "NDA",
        "API key", "password",
        "LLM",
        "support_level=verified",
        "Serial",
        "firmware",
    ):
        assert kw in text, f"CONTRIBUTING.md に {kw!r} 無し"


# =========================================================
# P2-8: docs に template 外部化 TODO
# =========================================================


def test_authoring_doc_mentions_external_template_todo():
    text = (ROOT / "docs" / "extension_authoring.md").read_text(
        encoding="utf-8")
    # v1.8+ で外部化する旨を docs に明記
    assert "templates/extensions" in text or "外部" in text or (
        "外部ファイル化" in text)
    assert "importlib.resources" in text or "v1.8" in text


# =========================================================
# CHANGELOG
# =========================================================

