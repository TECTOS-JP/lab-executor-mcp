"""v1.9.0: Instrument Quality / Strict Validation tests

- validate instrument --strict 強化
  - manual_ref TODO 検出
  - output-capable safe_shutdown 必須
  - output-capable safety.ratings 必須
  - state-changing set command の verify 必須
  - verified なのに validation_evidence 空
- normalize_category / category aliases
- instrument promote-check (minimal)
- extension doctor の instrument_quality summary
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
from lab_executor.extension_authoring import (
    doctor_extension, init_extension_pack,
)
from lab_executor.instrument_authoring import (
    add_instrument_to_pack, scaffold_instrument_definition,
    promote_check_instrument,
)
from lab_executor.registry import (
    CATEGORY_ALIASES, OUTPUT_CAPABLE_CATEGORIES,
    normalize_category, validate_instrument_file,
)

ROOT = Path(__file__).parent.parent


# =========================================================
# Version + MCP surface
# =========================================================


def test_no_new_mcp_tools_in_v1_9():
    assert stability.stable_count() == 43
    assert stability.experimental_count() == 7
    assert stability.total_documented_count() == 50


# =========================================================
# category aliases
# =========================================================


def test_category_alias_multimeter_to_dmm():
    assert normalize_category("multimeter") == "dmm"
    assert normalize_category("MULTIMETER") == "dmm"
    assert normalize_category("digital_multimeter") == "dmm"


def test_category_alias_psu_to_power_supply():
    assert normalize_category("psu") == "power_supply"
    assert normalize_category("PSU") == "power_supply"


def test_category_no_alias_passthrough():
    assert normalize_category("power_supply") == "power_supply"
    assert normalize_category("oscilloscope") == "oscilloscope"


def test_output_capable_categories_includes_power_supply():
    assert "power_supply" in OUTPUT_CAPABLE_CATEGORIES
    assert "smu" in OUTPUT_CAPABLE_CATEGORIES
    assert "function_generator" in OUTPUT_CAPABLE_CATEGORIES
    assert "dmm" not in OUTPUT_CAPABLE_CATEGORIES


# =========================================================
# strict validation: manual_ref TODO
# =========================================================


def _write_instrument(p: Path, **md_overrides):
    md = {
        "manufacturer": "Acme", "model": "M1",
        "category": "dmm", "support_level": "tested",
    }
    md.update(md_overrides)
    body = {"metadata": md, "commands": {}}
    p.write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")


def test_strict_detects_manual_ref_todo(tmp_path):
    p = tmp_path / "x.yaml"
    _write_instrument(
        p, manual_ref="TODO: URL or document title + revision + page range",
    )
    rep = validate_instrument_file(p, strict=True)
    assert rep.status == "error"
    assert any(
        e["error_class"] == "instrument_manual_ref_todo"
        for e in rep.errors
    )


def test_strict_detects_tbd_in_manual_ref(tmp_path):
    p = tmp_path / "x.yaml"
    _write_instrument(p, manual_ref="TBD")
    rep = validate_instrument_file(p, strict=True)
    assert any(
        e["error_class"] == "instrument_manual_ref_todo"
        for e in rep.errors
    )


def test_strict_real_manual_ref_passes(tmp_path):
    p = tmp_path / "x.yaml"
    _write_instrument(
        p, manual_ref="Kikusui PMX-A Programming Manual Rev 1.05 pp.34-58",
    )
    rep = validate_instrument_file(p, strict=True)
    classes = {e["error_class"] for e in rep.errors}
    assert "instrument_manual_ref_todo" not in classes


def test_strict_does_not_apply_in_normal_mode(tmp_path):
    p = tmp_path / "x.yaml"
    _write_instrument(p, manual_ref="TODO")
    rep = validate_instrument_file(p)  # strict=False (default)
    # TODO 残存は normal では error にしない (warnings は他要因で出る可能性)
    assert not any(
        e["error_class"] == "instrument_manual_ref_todo"
        for e in rep.errors
    )


# =========================================================
# strict validation: output-capable safe_shutdown
# =========================================================


def test_strict_output_capable_requires_safe_shutdown(tmp_path):
    p = tmp_path / "psu.yaml"
    body = {
        "metadata": {
            "manufacturer": "A", "model": "P",
            "category": "power_supply", "support_level": "tested",
            "manual_ref": "Manual rev 1.0",
        },
        "commands": {
            "set_voltage": {
                "scpi": "VOLT {v}", "type": "write",
            },
            "query_voltage": {
                "scpi": "VOLT?", "type": "query",
            },
        },
        "safety": {
            "ratings": {
                "voltage": {
                    "rated": 30, "absolute_max": 31.5, "unit": "V",
                },
            },
        },
    }
    p.write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")
    rep = validate_instrument_file(p, strict=True)
    classes = {e["error_class"] for e in rep.errors}
    assert "instrument_missing_safe_shutdown" in classes


def test_strict_output_capable_with_safe_shutdown_ok(tmp_path):
    p = tmp_path / "psu.yaml"
    body = {
        "metadata": {
            "manufacturer": "A", "model": "P",
            "category": "power_supply", "support_level": "tested",
            "manual_ref": "Manual rev 1.0",
        },
        "commands": {
            "set_voltage": {
                "scpi": "VOLT {v}", "type": "write",
                "verify": {
                    "readback_command": "query_voltage", "tolerance": 0.01,
                },
            },
            "query_voltage": {"scpi": "VOLT?", "type": "query"},
            "set_output": {"scpi": "OUTP {s}", "type": "write"},
        },
        "safety": {
            "ratings": {
                "voltage": {"rated": 30, "unit": "V"},
                "current": {"rated": 10, "unit": "A"},
            },
        },
        "safe_shutdown": [
            {"command": "set_output", "args": {"s": "OFF"}},
        ],
    }
    p.write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")
    rep = validate_instrument_file(p, strict=True)
    classes = {e["error_class"] for e in rep.errors}
    assert "instrument_missing_safe_shutdown" not in classes


def test_strict_non_output_dmm_does_not_require_safe_shutdown(tmp_path):
    p = tmp_path / "dmm.yaml"
    body = {
        "metadata": {
            "manufacturer": "A", "model": "M",
            "category": "dmm", "support_level": "tested",
            "manual_ref": "Manual rev 1.0",
        },
        "commands": {
            "measure_voltage_dc": {
                "scpi": "MEAS:VOLT:DC?", "type": "query",
            },
        },
    }
    p.write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")
    rep = validate_instrument_file(p, strict=True)
    classes = {e["error_class"] for e in rep.errors}
    assert "instrument_missing_safe_shutdown" not in classes
    assert "instrument_missing_safety_ratings" not in classes


def test_strict_multimeter_alias_does_not_require_safe_shutdown(tmp_path):
    """category: multimeter (alias) も dmm 扱いで safe_shutdown 不要"""
    p = tmp_path / "mm.yaml"
    body = {
        "metadata": {
            "manufacturer": "A", "model": "M",
            "category": "multimeter", "support_level": "tested",
            "manual_ref": "Manual rev 1.0",
        },
        "commands": {
            "measure": {"scpi": "MEAS?", "type": "query"},
        },
    }
    p.write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")
    rep = validate_instrument_file(p, strict=True)
    classes = {e["error_class"] for e in rep.errors}
    assert "instrument_missing_safe_shutdown" not in classes


# =========================================================
# strict validation: output-capable safety.ratings
# =========================================================


def test_strict_output_capable_requires_safety_ratings(tmp_path):
    p = tmp_path / "psu.yaml"
    body = {
        "metadata": {
            "manufacturer": "A", "model": "P",
            "category": "power_supply", "support_level": "tested",
            "manual_ref": "Manual rev 1.0",
        },
        "commands": {
            "set_output": {"scpi": "OUTP {s}", "type": "write"},
        },
        "safe_shutdown": [{"command": "set_output", "args": {"s": "OFF"}}],
        # safety: 不在
    }
    p.write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")
    rep = validate_instrument_file(p, strict=True)
    classes = {e["error_class"] for e in rep.errors}
    assert "instrument_missing_safety_ratings" in classes


# =========================================================
# strict validation: state-changing verify
# =========================================================


def test_strict_state_changing_set_voltage_requires_verify(tmp_path):
    p = tmp_path / "psu.yaml"
    body = {
        "metadata": {
            "manufacturer": "A", "model": "P",
            "category": "power_supply", "support_level": "tested",
            "manual_ref": "Manual rev 1.0",
        },
        "commands": {
            "set_voltage": {"scpi": "VOLT {v}", "type": "write"},
            "query_voltage": {"scpi": "VOLT?", "type": "query"},
            "set_output": {"scpi": "OUTP {s}", "type": "write"},
        },
        "safety": {"ratings": {"voltage": {"rated": 30, "unit": "V"}}},
        "safe_shutdown": [{"command": "set_output", "args": {"s": "OFF"}}],
    }
    p.write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")
    rep = validate_instrument_file(p, strict=True)
    missing = [
        e for e in rep.errors
        if e["error_class"] == "instrument_missing_verify"
    ]
    assert any(
        (e.get("details") or {}).get("command") == "set_voltage"
        for e in missing
    )
    # suggested readback が推測される
    sv_error = next(
        e for e in missing
        if (e.get("details") or {}).get("command") == "set_voltage"
    )
    assert sv_error["details"]["suggested_readback_command"] == (
        "query_voltage")


def test_strict_set_display_not_required_for_verify(tmp_path):
    """set_display や set_brightness のような auxiliary write は
    state-changing と見なさない (=verify 必須化しない)"""
    p = tmp_path / "dev.yaml"
    body = {
        "metadata": {
            "manufacturer": "A", "model": "P",
            "category": "dmm", "support_level": "tested",
            "manual_ref": "Manual rev 1.0",
        },
        "commands": {
            "set_display_brightness": {
                "scpi": "DISP:BRIG {level}", "type": "write",
            },
            "set_beep": {"scpi": "BEEP", "type": "write"},
        },
    }
    p.write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")
    rep = validate_instrument_file(p, strict=True)
    # state-changing set ではないので instrument_missing_verify は出ない
    classes = {e["error_class"] for e in rep.errors}
    assert "instrument_missing_verify" not in classes


# =========================================================
# strict validation: verified missing evidence
# =========================================================


def test_strict_verified_requires_validation_evidence(tmp_path):
    p = tmp_path / "v.yaml"
    body = {
        "metadata": {
            "manufacturer": "A", "model": "P",
            "category": "dmm", "support_level": "verified",
            "manual_ref": "Manual rev 1.0",
            # validation_evidence 不在
        },
        "commands": {"measure": {"scpi": "MEAS?", "type": "query"}},
    }
    p.write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")
    rep = validate_instrument_file(p, strict=True)
    classes = {e["error_class"] for e in rep.errors}
    assert "instrument_verified_missing_evidence" in classes


def test_strict_verified_with_evidence_ok(tmp_path):
    p = tmp_path / "v.yaml"
    body = {
        "metadata": {
            "manufacturer": "A", "model": "P",
            "category": "dmm", "support_level": "verified",
            "manual_ref": "Manual rev 1.0",
            "validation_evidence": {
                "tested_by": "TECTOS",
                "tested_at": "2026-05-24",
                "interface": "USB",
                "tested_items": ["measure"],
            },
        },
        "commands": {"measure": {"scpi": "MEAS?", "type": "query"}},
    }
    p.write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")
    rep = validate_instrument_file(p, strict=True)
    classes = {e["error_class"] for e in rep.errors}
    assert "instrument_verified_missing_evidence" not in classes


# =========================================================
# scaffold 生成 YAML は strict で TODO だけが落ちる
# =========================================================


def test_scaffold_power_supply_strict_passes_safety_checks(tmp_path):
    """scaffold 生成 power_supply は safety / safe_shutdown / ratings が
    揃っている (set_voltage / set_current_limit には verify あり)"""
    out = tmp_path / "psu.yaml"
    scaffold_instrument_definition(
        "power_supply", output=out, manufacturer="K", model="P",
    )
    rep = validate_instrument_file(out, strict=True)
    classes = {e["error_class"] for e in rep.errors}
    # 安全関連の strict error は出ないこと
    assert "instrument_missing_safe_shutdown" not in classes
    assert "instrument_missing_safety_ratings" not in classes
    # manual_ref TODO は当然出る (scaffold 直後 / publishing 前に埋める)
    assert "instrument_manual_ref_todo" in classes
    # set_voltage / set_current_limit には verify がある
    missing_verify = [
        e for e in rep.errors
        if e["error_class"] == "instrument_missing_verify"
    ]
    missing_verify_cmds = {
        (e.get("details") or {}).get("command") for e in missing_verify
    }
    assert "set_voltage" not in missing_verify_cmds
    assert "set_current_limit" not in missing_verify_cmds
    # set_output は scaffold では verify が無い (publishing 前に user が
    # 追加する想定。strict check で気付ける)
    # → publishing checklist で「set_output に verify を追加する」を
    # 促す機能として有効
    if "set_output" in missing_verify_cmds:
        # 期待動作: scaffold は出すが publishing 時に user が埋める
        pass


def test_scaffold_dmm_strict_passes_after_filling_manual_ref(tmp_path):
    out = tmp_path / "dmm.yaml"
    scaffold_instrument_definition(
        "dmm", output=out, manufacturer="K", model="D",
    )
    # manual_ref を埋める
    data = yaml.safe_load(out.read_text(encoding="utf-8"))
    data["metadata"]["manual_ref"] = "Test Manual Rev 1.0 p.10"
    out.write_text(yaml.safe_dump(data, sort_keys=False),
                   encoding="utf-8")
    rep = validate_instrument_file(out, strict=True)
    # dmm は output-capable ではないので strict 全 pass の想定
    assert not rep.errors, rep.errors


# =========================================================
# promote-check (minimal)
# =========================================================


def test_promote_check_dmm_to_tested_with_filled_manual_ref(tmp_path):
    out = tmp_path / "dmm.yaml"
    scaffold_instrument_definition("dmm", output=out)
    data = yaml.safe_load(out.read_text(encoding="utf-8"))
    data["metadata"]["manual_ref"] = "Real Manual rev 1.0 p.10"
    out.write_text(yaml.safe_dump(data, sort_keys=False),
                   encoding="utf-8")
    res = promote_check_instrument(out, target="tested")
    assert res.eligible is True


def test_promote_check_blocked_by_todo(tmp_path):
    out = tmp_path / "dmm.yaml"
    scaffold_instrument_definition("dmm", output=out)
    # manual_ref は TODO のまま
    res = promote_check_instrument(out, target="tested")
    assert res.eligible is False
    assert any(
        b["issue"] == "instrument_manual_ref_todo"
        for b in res.blocking_issues
    )
    assert any(
        a["action"] == "fill_manual_ref"
        for a in res.recommended_actions
    )


def test_promote_check_verified_requires_evidence(tmp_path):
    out = tmp_path / "v.yaml"
    body = {
        "metadata": {
            "manufacturer": "A", "model": "P",
            "category": "dmm", "support_level": "tested",
            "manual_ref": "Real manual rev 1.0",
        },
        "commands": {"measure": {"scpi": "MEAS?", "type": "query"}},
    }
    out.write_text(yaml.safe_dump(body, sort_keys=False),
                   encoding="utf-8")
    res = promote_check_instrument(out, target="verified")
    assert res.eligible is False
    assert any(
        b["issue"] == "instrument_verified_missing_evidence"
        for b in res.blocking_issues
    )


def test_promote_check_downgrade_always_eligible(tmp_path):
    """verified → tested のような下方移動は即 eligible"""
    out = tmp_path / "v.yaml"
    body = {
        "metadata": {
            "manufacturer": "A", "model": "P",
            "category": "dmm", "support_level": "verified",
            "manual_ref": "Real rev 1.0",
            "validation_evidence": {"tested_by": "X"},
        },
        "commands": {"measure": {"scpi": "MEAS?", "type": "query"}},
    }
    out.write_text(yaml.safe_dump(body, sort_keys=False),
                   encoding="utf-8")
    res = promote_check_instrument(out, target="tested")
    assert res.eligible is True


# =========================================================
# extension doctor: instrument_quality summary
# =========================================================


def test_doctor_emits_instrument_quality_summary(tmp_path):
    res = init_extension_pack(
        "p", target_dir=tmp_path, template="instrument_pack", author="A",
    )
    pack = Path(res.pack_path)
    add_instrument_to_pack(
        pack, instrument_id="psu1", category="power_supply",
        manufacturer="K", model="P",
    )
    add_instrument_to_pack(
        pack, instrument_id="dmm1", category="dmm",
    )
    drep = doctor_extension(pack / "extension.yaml")
    s = drep.summary.get("instrument_quality") or {}
    assert "total" in s
    assert "strict_passed" in s
    assert "strict_failed" in s
    assert "missing_verify_commands" in s
    assert "missing_safe_shutdown_instruments" in s
    assert "manual_ref_todo_instruments" in s
    # scaffold 直後の add は manual_ref TODO で必ず strict_failed
    # (example_instrument.yaml stub も含むので total >= 3)
    assert s["total"] >= 2
    # 追加した 2 件 (psu1, dmm1) は manual_ref TODO で必ず failed
    assert s["strict_failed"] >= 2
    assert s["manual_ref_todo_instruments"] >= 2


# =========================================================
# CLI
# =========================================================


def _run_cli(*args: str) -> tuple[int, str, str]:
    r = subprocess.run(
        [sys.executable, "-m", "visa_mcp.cli", *args],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    return r.returncode, r.stdout, r.stderr


def test_cli_validate_instrument_strict_help():
    rc, out, err = _run_cli("validate", "--help")
    assert "--strict" in (out + err)


def test_cli_instrument_promote_check_help():
    rc, out, err = _run_cli("instrument", "promote-check", "--help")
    text = out + err
    assert "promote-check" in text
    assert "--target" in text


def test_cli_dependency_report_runs():
    rc, out, err = _run_cli(
        # actually use direct module form
    )
    r = subprocess.run(
        [sys.executable, "-m", "lab_executor.dev.dependency_report", "--json"],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    assert r.returncode == 0
    data = json.loads(r.stdout)
    assert data["forbidden_import_violations"] == []
    assert data["pyvisa_direct_import_violations"] == []


def test_cli_validate_instrument_strict_runs(tmp_path):
    out = tmp_path / "dmm.yaml"
    scaffold_instrument_definition("dmm", output=out)
    rc, ot, err = _run_cli(
        "validate", "instrument", str(out), "--strict", "--json",
    )
    # 生成直後 (manual_ref TODO) なので error 終了する想定
    assert rc == 1
    data = json.loads(ot)
    classes = {
        e["error_class"]
        for r in data["reports"] for e in r["errors"]
    }
    assert "instrument_manual_ref_todo" in classes


# =========================================================
# Repo format
# =========================================================


V19_FILES = [
    "src/lab_executor/registry.py",
    "src/lab_executor/cli.py",
    "src/lab_executor/dev/dependency_report.py",
    "src/lab_executor/instrument_authoring.py",
    "src/lab_executor/extension_authoring.py",
    "docs/separation/notes.md",
    "tests/test_separation_boundary.py",
    "tests/test_v19_instrument_quality.py",
    ".github/workflows/ci.yml",
    "CHANGELOG.md",
]


@pytest.mark.parametrize("rel", V19_FILES)
def test_v19_files_lf_only(rel):
    p = ROOT / rel
    assert p.exists(), f"missing: {p}"
    text = p.read_text(encoding="utf-8")
    assert "\r" not in text


@pytest.mark.parametrize("rel", V19_FILES)
def test_v19_files_multiline(rel):
    p = ROOT / rel
    text = p.read_text(encoding="utf-8")
    assert text.count("\n") + 1 >= 5


def test_separation_notes_keywords():
    text = (ROOT / "docs" / "separation" / "notes.md").read_text(
        encoding="utf-8")
    for kw in (
        "pyvisa", "visa_manager", "lab-executor",
        "module top-level", "boundary",
        "install path", "PDF extractor",
    ):
        assert kw in text, f"docs/separation/notes.md に {kw!r} 無し"

