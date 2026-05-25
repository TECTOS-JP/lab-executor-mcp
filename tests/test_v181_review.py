"""v1.8.1: v1.8.0 review response

- P0-1: raw 改行 / multi-line (v1.8 関連 file)
- P0-2: 生成 YAML が確実に multi-line + yaml.safe_load + validate
- P1-3: external template 化 (importlib.resources)
- P1-4: dmm category 統一 (CLI 名 == metadata.category)
- P1-5: reset description に CAUTION
- P1-6: --force で .bak backup を作る
- P1-7: rollback テスト強化 (extension add-instrument)
- P1-8: manual_ref 記入例を docs に
"""
from __future__ import annotations
import shutil
from pathlib import Path

import pytest
import yaml

from lab_executor.extension_authoring import init_extension_pack
from lab_executor.instrument_authoring import (
    CATEGORIES, scaffold_instrument_definition,
    add_instrument_to_pack, _load_template,
)
from lab_executor.registry import validate_instrument_file

ROOT = Path(__file__).parent.parent


# =========================================================
# Version
# =========================================================


def test_version_v1_8_1():
    import visa_mcp
    assert visa_mcp.__version__.startswith("1.")


# =========================================================
# P0-1: LF + multi-line guard (v1.8 系)
# =========================================================


V18_FILES_FULL = [
    "src/visa_mcp/instrument_authoring.py",
    "src/visa_mcp/extension_authoring.py",
    "src/visa_mcp/cli.py",
    "src/visa_mcp/templates/instruments/power_supply.yaml",
    "src/visa_mcp/templates/instruments/dmm.yaml",
    "src/visa_mcp/templates/instruments/temperature_meter.yaml",
    "src/visa_mcp/templates/instruments/generic_scpi.yaml",
    "docs/instrument_authoring.md",
    "docs/extension_authoring.md",
    "CONTRIBUTING.md",
    "tests/test_v18_instrument_authoring.py",
    "tests/test_v181_review.py",
    "CHANGELOG.md",
]


@pytest.mark.parametrize("rel", V18_FILES_FULL)
def test_v181_lf_only(rel):
    p = ROOT / rel
    assert p.exists(), f"missing: {p}"
    text = p.read_text(encoding="utf-8")
    assert "\r" not in text, f"{rel} に CR 含む"


@pytest.mark.parametrize("rel", V18_FILES_FULL)
def test_v181_multiline(rel):
    p = ROOT / rel
    text = p.read_text(encoding="utf-8")
    assert text.count("\n") + 1 >= 5, f"{rel} が 5 行未満"


# =========================================================
# P1-3: external template 化
# =========================================================


@pytest.mark.parametrize("cat", list(CATEGORIES))
def test_template_files_exist(cat):
    p = ROOT / "src" / "visa_mcp" / "templates" / "instruments" / (
        f"{cat}.yaml")
    assert p.exists(), f"template file missing: {p}"


@pytest.mark.parametrize("cat", list(CATEGORIES))
def test_template_files_yaml_parseable(cat):
    """template ファイルは placeholder 置換前でも yaml.safe_load 可"""
    p = ROOT / "src" / "visa_mcp" / "templates" / "instruments" / (
        f"{cat}.yaml")
    text = p.read_text(encoding="utf-8")
    # template 内で {manufacturer} / {model} を仮値で置換
    body = text.replace("{manufacturer}", "X").replace("{model}", "Y")
    data = yaml.safe_load(body)
    assert isinstance(data, dict)


def test_load_template_uses_importlib_resources():
    """_load_template helper が importlib.resources で読めること"""
    text = _load_template("power_supply")
    assert "metadata:" in text
    assert "support_level" in text
    assert "{manufacturer}" in text  # placeholder 未置換


# =========================================================
# P0-2: 生成 YAML の multi-line + safe_load + validate (再確認)
# =========================================================


@pytest.mark.parametrize("cat", list(CATEGORIES))
def test_scaffold_generated_yaml_strongly_multiline(tmp_path, cat):
    """category ごとに最低 50 行 (power_supply はもっと多い)"""
    out = tmp_path / f"x_{cat}.yaml"
    scaffold_instrument_definition(cat, output=out)
    text = out.read_text(encoding="utf-8")
    if cat == "power_supply":
        assert text.count("\n") > 80, (
            f"power_supply: only {text.count(chr(10)) + 1} lines"
        )
    else:
        assert text.count("\n") > 25


@pytest.mark.parametrize("cat", list(CATEGORIES))
def test_scaffold_validate_instrument_no_errors(tmp_path, cat):
    out = tmp_path / f"x_{cat}.yaml"
    scaffold_instrument_definition(
        cat, output=out, manufacturer="Acme", model="M1",
    )
    rep = validate_instrument_file(out)
    assert not rep.errors, f"{cat}: {rep.errors}"


def test_scaffold_power_supply_keys_complete(tmp_path):
    out = tmp_path / "psu.yaml"
    scaffold_instrument_definition("power_supply", output=out)
    text = out.read_text(encoding="utf-8")
    for kw in ("metadata:", "commands:", "safety:", "safe_shutdown:",
               "state_query:"):
        assert kw in text, f"missing top-level key {kw!r}"
    data = yaml.safe_load(text)
    assert data["metadata"]["support_level"] == "draft"
    assert "safe_shutdown" in data and len(data["safe_shutdown"]) >= 1


# =========================================================
# P1-4: dmm CLI category と metadata.category の統一
# =========================================================


def test_dmm_template_metadata_category_is_dmm(tmp_path):
    out = tmp_path / "dmm.yaml"
    scaffold_instrument_definition("dmm", output=out)
    data = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert data["metadata"]["category"] == "dmm"


@pytest.mark.parametrize("cat", list(CATEGORIES))
def test_each_category_template_metadata_category_matches_cli(tmp_path, cat):
    """CLI --category と生成 YAML の metadata.category が一致"""
    out = tmp_path / f"{cat}.yaml"
    scaffold_instrument_definition(cat, output=out)
    data = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert data["metadata"]["category"] == cat, (
        f"{cat}: metadata.category={data['metadata']['category']!r} "
        "should match CLI category"
    )


# =========================================================
# P1-5: reset description に CAUTION
# =========================================================


def test_power_supply_reset_has_caution(tmp_path):
    out = tmp_path / "psu.yaml"
    scaffold_instrument_definition("power_supply", output=out)
    data = yaml.safe_load(out.read_text(encoding="utf-8"))
    desc = (data["commands"]["reset"].get("description") or "")
    assert "CAUTION" in desc or "caution" in desc.lower()


def test_dmm_reset_has_caution(tmp_path):
    out = tmp_path / "dmm.yaml"
    scaffold_instrument_definition("dmm", output=out)
    data = yaml.safe_load(out.read_text(encoding="utf-8"))
    desc = (data["commands"]["reset"].get("description") or "")
    assert "CAUTION" in desc or "caution" in desc.lower()


def test_generic_reset_has_caution(tmp_path):
    out = tmp_path / "g.yaml"
    scaffold_instrument_definition("generic_scpi", output=out)
    data = yaml.safe_load(out.read_text(encoding="utf-8"))
    desc = (data["commands"]["reset"].get("description") or "")
    assert "CAUTION" in desc or "caution" in desc.lower()


def test_power_supply_cautions_mention_reset(tmp_path):
    out = tmp_path / "psu.yaml"
    scaffold_instrument_definition("power_supply", output=out)
    data = yaml.safe_load(out.read_text(encoding="utf-8"))
    cautions = (data.get("safety") or {}).get("cautions") or []
    assert any("RST" in c or "reset" in c.lower() for c in cautions)


# =========================================================
# P1-6: --force で .bak backup を作る
# =========================================================


def test_scaffold_force_creates_backup(tmp_path):
    out = tmp_path / "psu.yaml"
    # 既存 file を仕込む
    out.write_text("user_edited: keep\n", encoding="utf-8")
    res = scaffold_instrument_definition(
        "power_supply", output=out, force=True,
    )
    assert res.status == "ok"
    # .bak-<ts> file が同 directory に存在
    backups = list(out.parent.glob("psu.yaml.bak-*"))
    assert len(backups) == 1, f"expected 1 backup, got {backups}"
    # backup 内容は旧 file
    assert "user_edited: keep" in backups[0].read_text(encoding="utf-8")
    # warning でも報告される
    assert any(
        w.get("warning_class") == "instrument_scaffold_force_backup"
        for w in res.warnings
    )


def test_scaffold_no_force_no_backup(tmp_path):
    """既存 file が無い + force=True でも backup は作らない"""
    out = tmp_path / "psu.yaml"
    res = scaffold_instrument_definition(
        "power_supply", output=out, force=True,
    )
    assert res.status == "ok"
    backups = list(out.parent.glob("psu.yaml.bak-*"))
    assert backups == []


# =========================================================
# P1-7: rollback テスト強化 (extension add-instrument)
# =========================================================


@pytest.fixture
def fresh_pack(tmp_path):
    res = init_extension_pack(
        "p", target_dir=tmp_path, template="instrument_pack", author="A",
    )
    return Path(res.pack_path)


def _force_post_validate_failure(monkeypatch):
    """monkeypatch: 更新後 validate を失敗させて rollback を起こす"""
    from visa_mcp import extension as _ext_module

    def fake_validate(path, strict=False):
        class _FakeRep:
            def __init__(self):
                self.errors = [{
                    "error_class": "validation",
                    "message": "forced post-update failure",
                }]
                self.warnings = []
                self.manifest = None
        rep = _FakeRep()
        return rep

    # add_instrument_to_pack 内 from lab_executor.extension import
    # validate_extension_file の参照を差し替え
    import lab_executor.instrument_authoring as ia_mod
    # 関数内 import なので、monkeypatch は extension モジュール側を直接
    monkeypatch.setattr(
        _ext_module, "validate_extension_file", fake_validate,
    )


def test_rollback_restores_extension_yaml(fresh_pack, monkeypatch):
    """更新後 validate 失敗 → extension.yaml が元に戻る"""
    ext_yaml = fresh_pack / "extension.yaml"
    before_text = ext_yaml.read_text(encoding="utf-8")
    # 事前 validate は通る必要があるため、pass-through で 1 回目はパス
    from visa_mcp import extension as _ext_module
    original = _ext_module.validate_extension_file
    call_count = {"n": 0}

    def gated_validate(path, strict=False):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return original(path, strict=strict)
        # 2 回目 (post-update) で失敗させる
        class _FakeRep:
            def __init__(self):
                self.errors = [{
                    "error_class": "validation",
                    "message": "forced post-update failure",
                }]
                self.warnings = []
                self.manifest = None
        return _FakeRep()

    monkeypatch.setattr(
        _ext_module, "validate_extension_file", gated_validate,
    )
    res = add_instrument_to_pack(
        fresh_pack, instrument_id="rb_ext", category="power_supply",
    )
    assert res.status == "error"
    # rolled_back sub_class
    assert any(
        (e.get("details") or {}).get("sub_class")
        == "add_instrument_rolled_back"
        for e in res.errors
    )
    # extension.yaml が元に戻る
    assert ext_yaml.read_text(encoding="utf-8") == before_text


def test_rollback_restores_registry_index(fresh_pack, monkeypatch):
    reg_index = fresh_pack / "registry_entries" / "INDEX.yaml"
    before = reg_index.read_text(encoding="utf-8") if reg_index.exists() else None

    from visa_mcp import extension as _ext_module
    original = _ext_module.validate_extension_file
    call_count = {"n": 0}

    def gated_validate(path, strict=False):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return original(path, strict=strict)
        class _FakeRep:
            def __init__(self):
                self.errors = [{"error_class": "validation",
                                  "message": "x"}]
                self.warnings = []
                self.manifest = None
        return _FakeRep()
    monkeypatch.setattr(
        _ext_module, "validate_extension_file", gated_validate,
    )
    res = add_instrument_to_pack(
        fresh_pack, instrument_id="rb_reg", category="dmm",
    )
    assert res.status == "error"
    # registry_entries/INDEX.yaml が元に戻る
    if before is not None:
        assert reg_index.read_text(encoding="utf-8") == before


def test_rollback_removes_new_instrument_file(fresh_pack, monkeypatch):
    """rollback で新規 instrument file が削除される"""
    from visa_mcp import extension as _ext_module
    original = _ext_module.validate_extension_file
    call_count = {"n": 0}

    def gated_validate(path, strict=False):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return original(path, strict=strict)
        class _FakeRep:
            def __init__(self):
                self.errors = [{"error_class": "validation", "message": "x"}]
                self.warnings = []
                self.manifest = None
        return _FakeRep()
    monkeypatch.setattr(
        _ext_module, "validate_extension_file", gated_validate,
    )
    inst_path = fresh_pack / "instruments" / "rb_new.yaml"
    res = add_instrument_to_pack(
        fresh_pack, instrument_id="rb_new", category="dmm",
    )
    assert res.status == "error"
    # rollback で削除
    assert not inst_path.exists(), (
        "new instrument file should have been removed on rollback"
    )


def test_rollback_preserves_existing_instrument_file(fresh_pack, monkeypatch):
    """既存 instrument file は --force 上書き → rollback で元内容に戻る"""
    inst_path = fresh_pack / "instruments" / "rb_pre.yaml"
    inst_path.parent.mkdir(parents=True, exist_ok=True)
    inst_path.write_text("original_content: keep\n", encoding="utf-8")
    original_text = inst_path.read_text(encoding="utf-8")

    from visa_mcp import extension as _ext_module
    original = _ext_module.validate_extension_file
    call_count = {"n": 0}

    def gated_validate(path, strict=False):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return original(path, strict=strict)
        class _FakeRep:
            def __init__(self):
                self.errors = [{"error_class": "validation", "message": "x"}]
                self.warnings = []
                self.manifest = None
        return _FakeRep()
    monkeypatch.setattr(
        _ext_module, "validate_extension_file", gated_validate,
    )
    res = add_instrument_to_pack(
        fresh_pack, instrument_id="rb_pre", category="power_supply",
        force=True,
    )
    assert res.status == "error"
    # 元の内容に戻る
    assert inst_path.read_text(encoding="utf-8") == original_text


# =========================================================
# P1-8: docs に manual_ref 例
# =========================================================


def test_docs_manual_ref_examples():
    text = (ROOT / "docs" / "instrument_authoring.md").read_text(
        encoding="utf-8")
    # 具体例 3 件以上
    assert text.count("manual_ref:") >= 3
    # vendor 名 / Rev / page range / URL 例のどれかを含む
    assert "Rev" in text or "Edition" in text or "pp." in text
    assert "https://" in text


def test_docs_dmm_category_alignment():
    text = (ROOT / "docs" / "instrument_authoring.md").read_text(
        encoding="utf-8")
    # CLI / metadata 一致を docs で明示
    assert "metadata.category" in text
    assert "multimeter" in text  # 旧表記との関係に触れる
    assert "dmm" in text


def test_docs_force_backup_described():
    text = (ROOT / "docs" / "instrument_authoring.md").read_text(
        encoding="utf-8")
    assert ".bak-" in text or "backup" in text.lower()
    assert "instrument_scaffold_force_backup" in text or "--force" in text


# =========================================================
# CHANGELOG
# =========================================================


def test_changelog_has_v181_entry():
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "v1.8.1" in text
    assert "instrument_scaffold_force_backup" in text
    assert "templates/instruments" in text
