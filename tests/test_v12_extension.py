"""v1.2: Extension policy / definition pack / docs tests"""
from __future__ import annotations
import json
import subprocess
import sys
from pathlib import Path

import pytest

from lab_executor import stability
from lab_executor.extension import (
    ExtensionManifest, validate_extension_file, SUPPORT_LEVELS,
)

ROOT = Path(__file__).parent.parent


# =========================================================
# Version
# =========================================================


REQUIRED_DOCS = [
    ("docs/extension_policy.md", [
        "definition_pack", "executable plugin", "Stability",
        "Python plugin",
    ]),
    ("docs/definition_packs.md", [
        "extension_id", "executable_code", "support_level",
        "visa_mcp_compatibility",
    ]),
    ("docs/registry_contribution.md", [
        "support_level", "verified", "tested", "experimental", "draft",
        "PR", "visa-mcp validate instrument",
    ]),
    ("docs/replay_backend_concept.md", [
        "can_be_replayed", "deterministic", "Why NOT v1.2",
        "implemented",
    ]),
]


@pytest.mark.parametrize("rel,keywords", REQUIRED_DOCS)
def test_v12_docs_exist_with_keywords(rel, keywords):
    p = ROOT / rel
    assert p.exists(), f"missing: {p}"
    text = p.read_text(encoding="utf-8")
    for kw in keywords:
        assert kw.lower() in text.lower(), f"{rel} に {kw!r} 無し"


def test_v1_stability_mentions_definition_packs():
    text = (ROOT / "docs" / "v1_stability_policy.md").read_text(encoding="utf-8")
    assert "Definition packs" in text or "definition pack" in text.lower()
    assert "Executable plugin" in text or "executable_code" in text
    assert "v1.2" in text


def test_backend_abstraction_capability_model_documented():
    text = (ROOT / "docs" / "backend_abstraction.md").read_text(encoding="utf-8")
    assert "Backend capability model" in text or \
        "backend_capabilities" in text
    assert "Error mapping" in text


# =========================================================
# ExtensionManifest schema validation
# =========================================================


VALID_MIN = {
    "extension_id": "tectos.mock.basic",
    "name": "Basic",
    "version": "0.1.0",
    "type": "definition_pack",
    "stability": {"support_level": "tested", "executable_code": False},
}


def test_manifest_accepts_minimal_valid():
    m = ExtensionManifest(**VALID_MIN)
    assert m.extension_id == "tectos.mock.basic"
    assert m.stability.executable_code is False


def test_manifest_rejects_executable_code_true():
    bad = {**VALID_MIN}
    bad["stability"] = {"support_level": "tested", "executable_code": True}
    with pytest.raises(Exception) as ei:
        ExtensionManifest(**bad)
    assert "executable_code" in str(ei.value).lower()


def test_manifest_rejects_non_definition_pack_type():
    bad = {**VALID_MIN, "type": "python_plugin"}
    with pytest.raises(Exception):
        ExtensionManifest(**bad)


def test_manifest_rejects_invalid_extension_id():
    bad = {**VALID_MIN, "extension_id": "BadID with spaces"}
    with pytest.raises(Exception):
        ExtensionManifest(**bad)


def test_manifest_rejects_non_semver_version():
    bad = {**VALID_MIN, "version": "v1"}
    with pytest.raises(Exception):
        ExtensionManifest(**bad)


def test_manifest_rejects_invalid_support_level():
    bad = {**VALID_MIN}
    bad["stability"] = {"support_level": "GOLD", "executable_code": False}
    with pytest.raises(Exception):
        ExtensionManifest(**bad)


def test_support_levels_known():
    for sl in SUPPORT_LEVELS:
        assert sl in ("verified", "tested", "experimental", "draft")


# =========================================================
# validate_extension_file (file-based)
# =========================================================


def test_validate_extension_file_example_pack_passes():
    p = ROOT / "examples" / "extensions" / "mock_basic_pack" / "extension.yaml"
    rep = validate_extension_file(p)
    assert not rep.errors, f"errors: {rep.errors}"
    assert rep.status in ("ok", "warning")
    assert rep.files_checked >= 3
    assert rep.manifest is not None
    assert rep.manifest["extension_id"] == "visa-mcp.mock.basic"


def test_validate_extension_file_not_found(tmp_path):
    rep = validate_extension_file(tmp_path / "nope.yaml")
    assert rep.status == "error"
    assert any(e["error_class"] == "not_found" for e in rep.errors)


def test_validate_extension_file_rejects_executable_code(tmp_path):
    """**重要**: executable_code=true は schema レベルで拒否"""
    p = tmp_path / "ext.yaml"
    p.write_text(
        "extension_id: a.b\nname: x\nversion: 0.1.0\ntype: definition_pack\n"
        "stability: { support_level: tested, executable_code: true }\n",
        encoding="utf-8",
    )
    rep = validate_extension_file(p)
    assert rep.status == "error"
    assert any(
        "executable_code" in (e.get("message") or "")
        for e in rep.errors
    )


def test_validate_extension_file_missing_referenced_file(tmp_path):
    p = tmp_path / "ext.yaml"
    p.write_text(
        "extension_id: a.b\nname: x\nversion: 0.1.0\ntype: definition_pack\n"
        "stability: { support_level: tested, executable_code: false }\n"
        "contents:\n"
        "  instruments: [ instruments/nope.yaml ]\n",
        encoding="utf-8",
    )
    rep = validate_extension_file(p)
    assert rep.status == "error"
    assert any(e["error_class"] == "not_found" for e in rep.errors)


def test_validate_extension_file_empty_contents_warns(tmp_path):
    p = tmp_path / "ext.yaml"
    p.write_text(
        "extension_id: a.b\nname: x\nversion: 0.1.0\ntype: definition_pack\n"
        "stability: { support_level: tested, executable_code: false }\n",
        encoding="utf-8",
    )
    rep = validate_extension_file(p)
    # contents 全空 → warning (status は warning または ok)
    classes = [w["warning_class"] for w in rep.warnings]
    assert "empty_contents" in classes


# =========================================================
# Schema file (extension_manifest.schema.json)
# =========================================================


def test_extension_manifest_schema_generated():
    p = ROOT / "schemas" / "extension_manifest.schema.json"
    assert p.exists()
    data = json.loads(p.read_text(encoding="utf-8"))
    assert "properties" in data
    for k in ("extension_id", "name", "version", "type", "contents",
              "stability"):
        assert k in data["properties"]


def test_extension_manifest_schema_status_experimental():
    p = ROOT / "schemas" / "extension_manifest.schema.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    # v1.2 では definition pack manifest は experimental
    assert data.get("x-visa-mcp-status") in ("experimental", "preview")


# =========================================================
# CLI integration
# =========================================================


def _run_cli(*args: str) -> tuple[int, dict]:
    cmd = [sys.executable, "-m", "visa_mcp.cli", *args, "--json"]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT))
    try:
        data = json.loads(result.stdout) if result.stdout.strip() else {}
    except json.JSONDecodeError:
        data = {"_raw": result.stdout, "_stderr": result.stderr}
    return result.returncode, data


def test_validate_cli_extension_success():
    rc, data = _run_cli(
        "validate", "extension",
        "examples/extensions/mock_basic_pack/extension.yaml",
    )
    assert rc == 0, data
    assert data["reports"][0]["status"] in ("ok", "warning")


def test_validate_cli_extension_failure(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("extension_id: x\nstability: { executable_code: true }\n",
                   encoding="utf-8")
    rc, data = _run_cli("validate", "extension", str(bad))
    assert rc == 1


# =========================================================
# No new stable MCP tools / Backend still experimental
# =========================================================


def test_no_new_stable_tools_in_v1_2():
    assert stability.stable_count() == 43


def test_backend_abstraction_still_experimental():
    text = (ROOT / "docs"
            / "v1_stability_policy.md").read_text(encoding="utf-8")
    # backend_abstraction が stable plugin API でないと書かれている
    assert "InstrumentBackend" in text
    # backend が experimental スコープ
    assert "experimental" in text.lower()


# =========================================================
# Stability の experimental tools には bundle inspection が残る
# =========================================================


def test_experimental_tools_unchanged_in_v1_2():
    """v1.2 では experimental tools も増えていない (7 のまま)"""
    assert stability.experimental_count() == 7


# =========================================================
# Repo format guard for v1.2 files
# =========================================================


V12_FILES = [
    "docs/extension_policy.md",
    "docs/definition_packs.md",
    "docs/registry_contribution.md",
    "docs/replay_backend_concept.md",
    "src/lab_executor/extension.py",
    "examples/extensions/mock_basic_pack/extension.yaml",
    "examples/extensions/mock_basic_pack/README.md",
    "tests/test_v12_extension.py",
]


@pytest.mark.parametrize("rel", V12_FILES)
def test_v12_files_lf_only(rel):
    p = ROOT / rel
    assert p.exists()
    text = p.read_text(encoding="utf-8")
    assert "\r" not in text


@pytest.mark.parametrize("rel", V12_FILES)
def test_v12_files_multiline(rel):
    p = ROOT / rel
    text = p.read_text(encoding="utf-8")
    assert text.count("\n") + 1 >= 5
