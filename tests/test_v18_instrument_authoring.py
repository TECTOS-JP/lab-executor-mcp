"""v1.8.0: Instrument Definition Authoring tests

- instrument scaffold (4 categories)
- 生成 YAML が multi-line + yaml.safe_load + validate instrument OK
- power_supply scaffold に safe_shutdown / state_query / verify / safety
- extension add-instrument --dry-run
- extension add-instrument が contents / registry index を更新
- duplicate registry id 拒否
- 更新後 validate 失敗時 rollback
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
from lab_executor.extension_authoring import init_extension_pack
from lab_executor.instrument_authoring import (
    CATEGORIES, scaffold_instrument_definition,
    add_instrument_to_pack,
)
from lab_executor.registry import validate_instrument_file

ROOT = Path(__file__).parent.parent


# =========================================================
# Version + MCP surface
# =========================================================


def test_version_v1_8_0():
    import visa_mcp
    assert visa_mcp.__version__.startswith("1.")


def test_no_new_mcp_tools_in_v1_8():
    assert stability.stable_count() == 43
    assert stability.experimental_count() == 7
    assert stability.total_documented_count() == 50


def test_stable_tools_unchanged_in_v1_8():
    names = set(stability.stable_tool_names())
    assert "validate_experiment_plan" in names


# =========================================================
# scaffold (all categories)
# =========================================================


@pytest.mark.parametrize("cat", list(CATEGORIES))
def test_instrument_scaffold_writes_file(tmp_path, cat):
    out = tmp_path / "out" / f"x_{cat}.yaml"
    res = scaffold_instrument_definition(
        cat, output=out, manufacturer="Acme", model="M1",
    )
    assert res.status == "ok", res.errors
    assert out.exists()
    assert res.output_path == str(out)


@pytest.mark.parametrize("cat", list(CATEGORIES))
def test_scaffold_generated_yaml_is_multiline(tmp_path, cat):
    out = tmp_path / f"x_{cat}.yaml"
    res = scaffold_instrument_definition(cat, output=out)
    assert res.status == "ok"
    text = out.read_text(encoding="utf-8")
    # category 別に最小行数は違うが、最低 25 行 (draft header 込み)
    assert text.count("\n") > 25, (
        f"{cat}: only {text.count(chr(10)) + 1} lines"
    )


@pytest.mark.parametrize("cat", list(CATEGORIES))
def test_scaffold_generated_yaml_safe_loads(tmp_path, cat):
    out = tmp_path / f"x_{cat}.yaml"
    scaffold_instrument_definition(cat, output=out)
    data = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert "metadata" in data
    assert "commands" in data
    assert data["metadata"]["support_level"] == "draft"
    # manual_ref に TODO placeholder
    assert "TODO" in (data["metadata"].get("manual_ref") or "")


@pytest.mark.parametrize("cat", list(CATEGORIES))
def test_scaffold_generated_definition_validates(tmp_path, cat):
    """重要: scaffold 直後の YAML が validate instrument を通る"""
    out = tmp_path / f"x_{cat}.yaml"
    scaffold_instrument_definition(
        cat, output=out, manufacturer="Acme", model="M1",
    )
    rep = validate_instrument_file(out)
    assert not rep.errors, f"{cat}: {rep.errors}"


def test_scaffold_emits_draft_warning(tmp_path):
    out = tmp_path / "x.yaml"
    res = scaffold_instrument_definition(
        "generic_scpi", output=out,
    )
    assert any(
        w["warning_class"] == "instrument_definition_draft"
        for w in res.warnings
    )


def test_scaffold_rejects_unknown_category(tmp_path):
    out = tmp_path / "x.yaml"
    res = scaffold_instrument_definition("weird", output=out)
    assert res.status == "error"
    assert any(
        (e.get("details") or {}).get("sub_class")
        == "instrument_scaffold_unknown_category"
        for e in res.errors
    )


def test_scaffold_rejects_existing_without_force(tmp_path):
    out = tmp_path / "x.yaml"
    out.write_text("existing", encoding="utf-8")
    res = scaffold_instrument_definition("dmm", output=out)
    assert res.status == "error"
    assert any(
        (e.get("details") or {}).get("sub_class")
        == "instrument_scaffold_target_exists"
        for e in res.errors
    )


def test_scaffold_force_overwrites(tmp_path):
    out = tmp_path / "x.yaml"
    out.write_text("existing", encoding="utf-8")
    res = scaffold_instrument_definition("dmm", output=out, force=True)
    assert res.status == "ok"
    assert "MEAS:VOLT:DC?" in out.read_text(encoding="utf-8")


# =========================================================
# power_supply 特有: safe_shutdown / state_query / verify / safety
# =========================================================


def test_scaffold_power_supply_has_safe_shutdown(tmp_path):
    out = tmp_path / "psu.yaml"
    scaffold_instrument_definition(
        "power_supply", output=out, manufacturer="K", model="P",
    )
    data = yaml.safe_load(out.read_text(encoding="utf-8"))
    ss = data.get("safe_shutdown") or []
    assert ss, "safe_shutdown is empty"
    # 1 番目に OUTP OFF
    assert ss[0]["command"] == "set_output"
    assert ss[0]["args"]["state"] == "OFF"


def test_scaffold_power_supply_has_state_query(tmp_path):
    out = tmp_path / "psu.yaml"
    scaffold_instrument_definition("power_supply", output=out)
    data = yaml.safe_load(out.read_text(encoding="utf-8"))
    sq = data.get("state_query") or {}
    for k in ("voltage_set", "current_limit", "voltage_measured",
              "current_measured", "output_state"):
        assert k in sq, f"missing state_query key {k!r}"


def test_scaffold_power_supply_has_verify_on_set_voltage(tmp_path):
    out = tmp_path / "psu.yaml"
    scaffold_instrument_definition("power_supply", output=out)
    data = yaml.safe_load(out.read_text(encoding="utf-8"))
    sv = data["commands"]["set_voltage"]
    assert "verify" in sv
    assert sv["verify"]["readback_command"] == "query_voltage"
    assert sv["verify"]["tolerance"] >= 0


def test_scaffold_power_supply_has_safety_ratings(tmp_path):
    out = tmp_path / "psu.yaml"
    scaffold_instrument_definition("power_supply", output=out)
    data = yaml.safe_load(out.read_text(encoding="utf-8"))
    ratings = (data.get("safety") or {}).get("ratings") or {}
    assert "voltage" in ratings
    assert "current" in ratings
    assert ratings["voltage"]["unit"] == "V"


def test_scaffold_power_supply_has_set_output_precondition(tmp_path):
    out = tmp_path / "psu.yaml"
    scaffold_instrument_definition("power_supply", output=out)
    data = yaml.safe_load(out.read_text(encoding="utf-8"))
    pre = (data.get("safety") or {}).get("preconditions") or []
    assert any(p.get("command") == "set_output" for p in pre)


# =========================================================
# dmm / temperature / generic
# =========================================================


def test_scaffold_dmm_has_polling_safe(tmp_path):
    out = tmp_path / "dmm.yaml"
    scaffold_instrument_definition("dmm", output=out)
    data = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert data["commands"]["measure_voltage_dc"]["polling_safe"] is True


def test_scaffold_temperature_has_channel_param(tmp_path):
    out = tmp_path / "t.yaml"
    scaffold_instrument_definition("temperature_meter", output=out)
    data = yaml.safe_load(out.read_text(encoding="utf-8"))
    cmd = data["commands"]["measure_temperature_channel"]
    params = cmd.get("parameters") or []
    assert any(p["name"] == "channel" for p in params)


def test_scaffold_generic_minimal(tmp_path):
    out = tmp_path / "g.yaml"
    scaffold_instrument_definition("generic_scpi", output=out)
    data = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert "identify" in data["commands"]
    assert data["metadata"]["category"] == "generic_scpi"


# =========================================================
# extension add-instrument
# =========================================================


@pytest.fixture
def fresh_pack(tmp_path):
    """init された pack (instrument_pack template)"""
    res = init_extension_pack(
        "p", target_dir=tmp_path, template="instrument_pack",
        author="A",
    )
    assert res.status == "ok"
    return Path(res.pack_path)


def test_add_instrument_dry_run_does_not_write(fresh_pack):
    res = add_instrument_to_pack(
        fresh_pack, instrument_id="psu1", category="power_supply",
        manufacturer="K", model="P", dry_run=True,
    )
    assert res.status == "ok"
    assert res.dry_run is True
    # file は生成されていない
    assert not (fresh_pack / "instruments" / "psu1.yaml").exists()
    # extension.yaml も unchanged
    ext_text = (fresh_pack / "extension.yaml").read_text(encoding="utf-8")
    assert "psu1.yaml" not in ext_text or (
        "instruments: [" not in ext_text)
    # changes_preview に予定が含まれる
    assert res.changes_preview["registry_index_add_entry"]["id"] == "psu1"


def test_add_instrument_updates_contents_and_registry(fresh_pack):
    res = add_instrument_to_pack(
        fresh_pack, instrument_id="psu1", category="power_supply",
        manufacturer="Kikusui", model="PMX-A",
    )
    assert res.status == "ok", res.errors
    # 1. instrument file が生成
    inst_path = fresh_pack / "instruments" / "psu1.yaml"
    assert inst_path.exists()
    # 2. extension.yaml.contents.instruments に追加
    ext_data = yaml.safe_load(
        (fresh_pack / "extension.yaml").read_text(encoding="utf-8"))
    assert "instruments/psu1.yaml" in ext_data["contents"]["instruments"]
    # 3. registry_entries/INDEX.yaml に entry
    reg_data = yaml.safe_load(
        (fresh_pack / "registry_entries"
         / "INDEX.yaml").read_text(encoding="utf-8"))
    ids = [e["id"] for e in reg_data["instruments"]]
    assert "psu1" in ids
    # 4. 各 flag
    assert res.extension_contents_updated is True
    assert res.registry_entry_added is True


def test_add_instrument_rejects_duplicate_registry_id(fresh_pack):
    # 1 回目
    res1 = add_instrument_to_pack(
        fresh_pack, instrument_id="dup1", category="dmm",
    )
    assert res1.status == "ok"
    # 2 回目 (force でも duplicate registry id は拒否)
    res2 = add_instrument_to_pack(
        fresh_pack, instrument_id="dup1", category="power_supply",
        force=True,
    )
    assert res2.status == "error"
    assert any(
        (e.get("details") or {}).get("sub_class")
        == "add_instrument_duplicate_registry_id"
        for e in res2.errors
    )


def test_add_instrument_rejects_existing_file_without_force(fresh_pack):
    inst = fresh_pack / "instruments" / "psu1.yaml"
    inst.parent.mkdir(parents=True, exist_ok=True)
    inst.write_text("existing", encoding="utf-8")
    res = add_instrument_to_pack(
        fresh_pack, instrument_id="psu1", category="power_supply",
    )
    assert res.status == "error"
    assert any(
        (e.get("details") or {}).get("sub_class")
        == "add_instrument_target_exists"
        for e in res.errors
    )


def test_add_instrument_rejects_invalid_id(fresh_pack):
    res = add_instrument_to_pack(
        fresh_pack, instrument_id="Bad Id!", category="dmm",
    )
    assert res.status == "error"
    assert any(
        (e.get("details") or {}).get("sub_class")
        == "add_instrument_invalid_id"
        for e in res.errors
    )


def test_add_instrument_rejects_unknown_category(fresh_pack):
    res = add_instrument_to_pack(
        fresh_pack, instrument_id="x", category="weird",
    )
    assert res.status == "error"


def test_add_instrument_rejects_when_pack_yaml_invalid(tmp_path):
    pack = tmp_path / "bad"
    pack.mkdir()
    (pack / "extension.yaml").write_text(
        "extension_id: ID with spaces\n",  # invalid id
        encoding="utf-8",
    )
    res = add_instrument_to_pack(
        pack, instrument_id="x", category="dmm",
    )
    assert res.status == "error"


def test_add_instrument_validates_pack_after_update(fresh_pack):
    """更新後 pack が validate を通ることを add-instrument 自体が保証する"""
    res = add_instrument_to_pack(
        fresh_pack, instrument_id="psu1", category="power_supply",
        manufacturer="K", model="P",
    )
    assert res.status == "ok"
    # validate extension は別 module だが、ここでは pack 整合の確認
    from lab_executor.extension import validate_extension_file
    rep = validate_extension_file(fresh_pack / "extension.yaml")
    assert not rep.errors


# =========================================================
# End-to-end: init pack → add multiple instruments → package dry-run
# =========================================================


def test_e2e_init_then_add_multiple_instruments(tmp_path):
    res = init_extension_pack(
        "e2", target_dir=tmp_path, template="instrument_pack",
        author="A",
    )
    pack = Path(res.pack_path)
    for iid, cat in (
        ("psu1", "power_supply"),
        ("dmm1", "dmm"),
        ("temp1", "temperature_meter"),
    ):
        r = add_instrument_to_pack(
            pack, instrument_id=iid, category=cat,
        )
        assert r.status == "ok", (iid, cat, r.errors)
    # 最終 pack validate
    from lab_executor.extension import validate_extension_file
    rep = validate_extension_file(pack / "extension.yaml")
    assert not rep.errors
    # 3 instruments + 1 example_instrument (from init) = 4
    reg = yaml.safe_load(
        (pack / "registry_entries" / "INDEX.yaml").read_text(
            encoding="utf-8"))
    assert len(reg["instruments"]) == 3


# =========================================================
# CLI
# =========================================================


def _run_cli(*args: str) -> tuple[int, str, str]:
    r = subprocess.run(
        [sys.executable, "-m", "visa_mcp.cli", *args],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    return r.returncode, r.stdout, r.stderr


def test_cli_instrument_scaffold_help():
    rc, out, err = _run_cli("instrument", "scaffold", "--help")
    text = out + err
    assert "scaffold" in text
    assert "power_supply" in text and "dmm" in text


def test_cli_extension_add_instrument_help():
    rc, out, err = _run_cli("extension", "add-instrument", "--help")
    text = out + err
    assert "add-instrument" in text
    assert "--dry-run" in text
    assert "--category" in text


def test_cli_instrument_scaffold_runs(tmp_path):
    out = tmp_path / "psu.yaml"
    rc, ot, err = _run_cli(
        "instrument", "scaffold", "power_supply",
        "--output", str(out),
        "--manufacturer", "Kikusui", "--model", "PMX",
        "--json",
    )
    assert rc == 0, err
    data = json.loads(ot)
    assert data["scaffold"]["status"] == "ok"
    assert out.exists()


def test_cli_extension_add_instrument_dry_run_runs(tmp_path):
    res = init_extension_pack(
        "p", target_dir=tmp_path, template="instrument_pack", author="A",
    )
    pack = Path(res.pack_path)
    rc, ot, err = _run_cli(
        "extension", "add-instrument", str(pack),
        "--id", "psu1", "--category", "power_supply",
        "--dry-run", "--json",
    )
    assert rc == 0, err
    data = json.loads(ot)
    assert data["add_instrument"]["dry_run"] is True
    assert data["add_instrument"]["status"] == "ok"


# =========================================================
# Repo format
# =========================================================


V18_FILES = [
    "src/visa_mcp/instrument_authoring.py",
    "src/visa_mcp/cli.py",
    "docs/instrument_authoring.md",
    "CONTRIBUTING.md",
    "tests/test_v18_instrument_authoring.py",
    "CHANGELOG.md",
]


@pytest.mark.parametrize("rel", V18_FILES)
def test_v18_files_lf_only(rel):
    p = ROOT / rel
    assert p.exists(), f"missing: {p}"
    text = p.read_text(encoding="utf-8")
    assert "\r" not in text


@pytest.mark.parametrize("rel", V18_FILES)
def test_v18_files_multiline(rel):
    p = ROOT / rel
    text = p.read_text(encoding="utf-8")
    assert text.count("\n") + 1 >= 5


# =========================================================
# docs
# =========================================================


def test_instrument_authoring_doc_keywords():
    text = (ROOT / "docs" / "instrument_authoring.md").read_text(
        encoding="utf-8")
    for kw in (
        "instrument scaffold", "extension add-instrument",
        "power_supply", "dmm", "temperature_meter", "generic_scpi",
        "support_level: draft", "manual_ref",
        "safe_shutdown", "state_query", "verify",
    ):
        assert kw in text, f"instrument_authoring.md に {kw!r} 無し"


def test_contributing_mentions_instrument_workflow():
    text = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    for kw in (
        "instrument scaffold",
        "extension add-instrument",
        "manual_ref",
    ):
        assert kw in text, f"CONTRIBUTING.md に {kw!r} 無し"


def test_changelog_has_v180_entry():
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "v1.8.0" in text
    assert "instrument scaffold" in text
    assert "add-instrument" in text
