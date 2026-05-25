"""v1.2.1: v1.2 review response (P0/P1/P2)

- P0: raw 改行 (v1.2 files)
- P1-2: extension_manifest.schema.json title/description が experimental
- P1-3: extension_id を reverse-DNS style recommended と docs に明記
- P1-4: contents.* path traversal / 絶対パス拒否
- P1-5: validate extension が実行可能性まで保証しないと docs に明記
- P1-6: empty_contents strict mode 候補 docs
- P2-7/8: registry_entries / visa_mcp_compatibility 補強
"""
from __future__ import annotations
import json
from pathlib import Path

import pytest

from lab_executor.extension import validate_extension_file

ROOT = Path(__file__).parent.parent


# =========================================================
# Version
# =========================================================


def test_version_v1_2_1():
    """v1.2+ の v1.x 系列を許容"""
    import visa_mcp
    assert visa_mcp.__version__.startswith("1.")


# =========================================================
# P0: repo files LF + multi-line (v1.2 全ファイル)
# =========================================================


V12_FILES_FULL = [
    "src/visa_mcp/extension.py",
    "schemas/extension_manifest.schema.json",
    "docs/extension_policy.md",
    "docs/definition_packs.md",
    "docs/registry_contribution.md",
    "docs/replay_backend_concept.md",
    "docs/backend_abstraction.md",
    "docs/v1_stability_policy.md",
    "examples/extensions/mock_basic_pack/extension.yaml",
    "examples/extensions/mock_basic_pack/README.md",
    "tests/test_v12_extension.py",
    "tests/test_v121_review.py",
]


@pytest.mark.parametrize("rel", V12_FILES_FULL)
def test_v121_lf_only(rel):
    p = ROOT / rel
    assert p.exists()
    text = p.read_text(encoding="utf-8")
    assert "\r" not in text


@pytest.mark.parametrize("rel", V12_FILES_FULL)
def test_v121_multiline(rel):
    p = ROOT / rel
    text = p.read_text(encoding="utf-8")
    assert text.count("\n") + 1 >= 5


# =========================================================
# P1-2: extension_manifest schema title/description が experimental
# =========================================================


def test_extension_schema_title_is_experimental():
    p = ROOT / "schemas" / "extension_manifest.schema.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    title = data.get("title", "")
    assert "experimental" in title.lower()
    # "stable" を含まないこと
    assert "stable" not in title.lower()
    # description 側も「stable」を強く謳わない
    desc = data.get("description", "")
    assert "experimental" in desc.lower()
    assert "v1.x" in desc


def test_extension_schema_status_experimental():
    p = ROOT / "schemas" / "extension_manifest.schema.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["x-visa-mcp-status"] == "experimental"
    assert "subject-to-change" in data.get("x-compatibility", "")


# =========================================================
# P1-3: docs に reverse-DNS recommended 表現
# =========================================================


def test_definition_packs_doc_explains_extension_id_format():
    text = (ROOT / "docs" / "definition_packs.md").read_text(encoding="utf-8")
    assert "reverse-DNS" in text or "reverse DNS" in text
    assert "recommended" in text.lower()


def test_definition_packs_doc_explains_visa_mcp_compatibility():
    text = (ROOT / "docs" / "definition_packs.md").read_text(encoding="utf-8")
    assert "記録用メタデータ" in text or "metadata only" in text.lower()


# =========================================================
# P1-4: path traversal / 絶対パス拒否
# =========================================================


def test_extension_rejects_path_traversal(tmp_path):
    """contents path に '../outside.yaml' のような traversal は拒否"""
    p = tmp_path / "ext.yaml"
    p.write_text(
        "extension_id: a.b\nname: x\nversion: 0.1.0\ntype: definition_pack\n"
        "stability: { support_level: tested, executable_code: false }\n"
        "contents:\n"
        "  instruments: [ '../outside.yaml' ]\n",
        encoding="utf-8",
    )
    rep = validate_extension_file(p)
    assert rep.status == "error"
    assert any(
        (e.get("details") or {}).get("sub_class")
        == "extension_path_outside_pack"
        for e in rep.errors
    )


def test_extension_rejects_absolute_path(tmp_path):
    """contents path に絶対パスは拒否"""
    abspath = "/etc/passwd" if not Path("/").drive else "C:\\Windows\\System32\\drivers\\etc\\hosts"
    p = tmp_path / "ext.yaml"
    p.write_text(
        "extension_id: a.b\nname: x\nversion: 0.1.0\ntype: definition_pack\n"
        "stability: { support_level: tested, executable_code: false }\n"
        f"contents:\n  instruments: ['{abspath}']\n",
        encoding="utf-8",
    )
    rep = validate_extension_file(p)
    assert rep.status == "error"
    assert any(
        (e.get("details") or {}).get("sub_class")
        == "extension_path_outside_pack"
        for e in rep.errors
    )


def test_extension_allows_safe_relative_path(tmp_path):
    """通常の相対パスは許可される (file 自体は存在しないため not_found)"""
    p = tmp_path / "ext.yaml"
    p.write_text(
        "extension_id: a.b\nname: x\nversion: 0.1.0\ntype: definition_pack\n"
        "stability: { support_level: tested, executable_code: false }\n"
        "contents:\n  instruments: ['instruments/x.yaml']\n",
        encoding="utf-8",
    )
    rep = validate_extension_file(p)
    # path traversal error は出ない
    assert not any(
        (e.get("details") or {}).get("sub_class")
        == "extension_path_outside_pack"
        for e in rep.errors
    )
    # 代わりに not_found
    assert any(e["error_class"] == "not_found" for e in rep.errors)


# =========================================================
# P1-5: validate extension の保証範囲が docs に明記
# =========================================================


def test_definition_packs_doc_explains_validation_scope():
    text = (ROOT / "docs" / "definition_packs.md").read_text(encoding="utf-8")
    for kw in ("保証すること", "保証しないこと",
               "実機での実行可能性",
               "validate_experiment_plan"):
        assert kw in text, f"definition_packs.md に {kw!r} 無し"


# =========================================================
# P1-6: strict mode 候補が docs に
# =========================================================


def test_definition_packs_doc_mentions_strict_mode_candidate():
    text = (ROOT / "docs" / "definition_packs.md").read_text(encoding="utf-8")
    assert "strict" in text.lower()
    assert "empty_contents" in text


# =========================================================
# 既存の example pack は引き続き pass する (互換維持)
# =========================================================


def test_example_pack_still_passes_after_v121():
    p = ROOT / "examples" / "extensions" / "mock_basic_pack" / "extension.yaml"
    rep = validate_extension_file(p)
    assert not rep.errors, f"errors: {rep.errors}"
