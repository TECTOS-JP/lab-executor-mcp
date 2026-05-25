"""v0.9.2: Ecosystem 準備 (registry / schema / CLI / lint) テスト"""
from __future__ import annotations
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from lab_executor.models.instrument_def import InstrumentDefinition
from lab_executor import registry as reg


ROOT = Path(__file__).parent.parent
SCHEMAS = ROOT / "schemas"
REGISTRY = ROOT / "registry"
INDEX = REGISTRY / "INDEX.yaml"


# =========================================================
# Schema files: pretty-print + LF only + preview metadata
# =========================================================


SCHEMA_FILES = [
    "instrument.schema.json",
    "system_config.schema.json",
    "dsl.schema.json",
    "benchmark_task.schema.json",
]


@pytest.mark.parametrize("name", SCHEMA_FILES)
def test_schema_files_are_lf_only(name):
    p = SCHEMAS / name
    assert p.exists(), f"missing schema: {p}"
    text = p.read_text(encoding="utf-8")
    assert "\r" not in text, f"CR found in {p}"


@pytest.mark.parametrize("name", SCHEMA_FILES)
def test_schema_files_are_pretty_printed(name):
    p = SCHEMAS / name
    text = p.read_text(encoding="utf-8")
    # 多行であること
    assert text.count("\n") >= 10, f"schema {p} appears single-line"
    # JSON として parse 可能
    data = json.loads(text)
    assert isinstance(data, dict)


@pytest.mark.parametrize("name", SCHEMA_FILES)
def test_schema_files_have_preview_metadata(name):
    p = SCHEMAS / name
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data.get("x-visa-mcp-status") in ("preview", "stable")
    assert "$id" in data


def test_benchmark_task_schema_generated():
    p = SCHEMAS / "benchmark_task.schema.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    # BenchmarkTask の主要フィールドが含まれる
    props = data.get("properties", {})
    for key in ("id", "title", "layer", "input", "expected", "fixtures",
                "broken_plan", "repaired_plan",
                "expected_failure", "expected_repair"):
        assert key in props, f"benchmark_task.schema.json に {key!r} 無し"


# =========================================================
# Registry index + entries
# =========================================================


def test_registry_index_loads():
    idx = reg.load_registry_index(INDEX)
    assert len(idx.instruments) >= 3
    ids = [e.id for e in idx.instruments]
    assert "mock_psu" in ids
    assert "mock_dmm" in ids
    assert "mock_temp" in ids


def test_registry_entries_point_to_existing_files():
    idx = reg.load_registry_index(INDEX)
    for e in idx.instruments:
        full = REGISTRY / e.path
        assert full.exists(), f"missing instrument file: {full}"


def test_registry_instrument_definitions_validate():
    idx = reg.load_registry_index(INDEX)
    for e in idx.instruments:
        full = REGISTRY / e.path
        rep = reg.validate_instrument_file(full)
        # error が無いこと (warning は許容)
        assert not rep.errors, f"{e.id}: errors={rep.errors}"


def test_registry_support_level_required():
    idx = reg.load_registry_index(INDEX)
    for e in idx.instruments:
        assert e.support_level in reg.SUPPORT_LEVELS, (
            f"{e.id}: invalid support_level={e.support_level}"
        )


def test_validate_registry_returns_no_errors():
    reps = reg.validate_registry(INDEX)
    assert len(reps) >= 3
    for r in reps:
        assert not r.errors, f"{r.file}: {r.errors}"


# =========================================================
# support_level
# =========================================================


def test_metadata_default_support_level_is_draft():
    d = InstrumentDefinition(**{
        "metadata": {"manufacturer": "X", "model": "Y"},
        "commands": {},
    })
    assert d.metadata.support_level == "draft"


def test_metadata_accepts_known_support_levels():
    for sl in ("verified", "tested", "experimental", "draft"):
        d = InstrumentDefinition(**{
            "metadata": {"manufacturer": "X", "model": "Y",
                          "support_level": sl},
            "commands": {},
        })
        assert d.metadata.support_level == sl


# =========================================================
# Lint: missing safe_shutdown / verify / state_query / draft
# =========================================================


def test_lint_missing_safe_shutdown_warns(tmp_path):
    yml = """
metadata:
  manufacturer: T
  model: M
  support_level: tested
commands:
  set_voltage:
    scpi: "VOLT {v}"
    type: write
    parameters:
      - { name: v, type: float, range: [0, 30] }
"""
    p = tmp_path / "x.yaml"
    p.write_text(yml, encoding="utf-8")
    rep = reg.validate_instrument_file(p)
    classes = [w["warning_class"] for w in rep.warnings]
    assert "missing_safe_shutdown" in classes
    assert "missing_verify" in classes
    assert "missing_state_query" in classes


def test_lint_draft_support_level_warns(tmp_path):
    yml = """
metadata:
  manufacturer: T
  model: M
  support_level: draft
commands: {}
"""
    p = tmp_path / "d.yaml"
    p.write_text(yml, encoding="utf-8")
    rep = reg.validate_instrument_file(p)
    classes = [w["warning_class"] for w in rep.warnings]
    assert "support_level_draft" in classes


def test_lint_verified_clean_definition_has_no_lint_warnings(tmp_path):
    # registry の mock_psu は verify / safe_shutdown / state_query を持つ。
    # support_level=tested なので draft 警告は出ない。
    rep = reg.validate_instrument_file(
        REGISTRY / "instruments" / "mock" / "mock_psu.yaml",
    )
    classes = [w["warning_class"] for w in rep.warnings]
    # missing 系警告が出ないこと
    assert "missing_safe_shutdown" not in classes
    assert "missing_state_query" not in classes
    assert "support_level_draft" not in classes


# =========================================================
# CLI (subprocess 経由)
# =========================================================


def _run_cli(*args: str) -> tuple[int, dict]:
    """visa-mcp CLI を subprocess で呼び出し、JSON 出力 (--json 必須) を返す"""
    cmd = [sys.executable, "-m", "visa_mcp.cli", *args, "--json"]
    result = subprocess.run(
        cmd, capture_output=True, text=True, cwd=str(ROOT),
    )
    try:
        data = json.loads(result.stdout) if result.stdout.strip() else {}
    except json.JSONDecodeError:
        data = {"_raw": result.stdout, "_stderr": result.stderr}
    return result.returncode, data


def test_validate_cli_instrument_success():
    rc, data = _run_cli(
        "validate", "instrument",
        str(REGISTRY / "instruments" / "mock" / "mock_psu.yaml"),
    )
    assert rc == 0
    assert data["reports"][0]["status"] in ("ok", "warning")
    assert not data["reports"][0]["errors"]


def test_validate_cli_instrument_failure(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("metadata: { manufacturer: X }\n", encoding="utf-8")
    rc, data = _run_cli("validate", "instrument", str(bad))
    assert rc == 1
    assert data["reports"][0]["status"] == "error"


def test_validate_cli_registry_success():
    rc, data = _run_cli("validate", "registry", str(INDEX))
    assert rc == 0
    assert len(data["reports"]) >= 3


def test_validate_cli_benchmark_task_success():
    rc, data = _run_cli(
        "validate", "benchmark",
        str(ROOT / "benchmarks" / "tasks"
            / "task_001_basic_validate_dry_run.yaml"),
    )
    assert rc == 0


def test_validate_cli_schemas():
    rc, data = _run_cli("validate", "schemas")
    assert rc == 0
    # 全 schema が preview metadata を持つ → warning 無し
    for r in data["reports"]:
        assert not r["errors"]


# =========================================================
# English docs draft 存在
# =========================================================


def test_english_quickstart_exists():
    p = ROOT / "docs" / "en" / "quickstart.md"
    assert p.exists()
    text = p.read_text(encoding="utf-8")
    assert "visa-mcp" in text
    assert "MCP" in text
    assert "safe_shutdown" in text


def test_english_concepts_exists():
    p = ROOT / "docs" / "en" / "concepts.md"
    assert p.exists()
    text = p.read_text(encoding="utf-8")
    assert "ExperimentPlan" in text
    assert "support_level" in text
