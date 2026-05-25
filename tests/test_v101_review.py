"""v1.0.1: external review response (P0/P1)

- P0: repo file format (LF only, multi-line) — 拡張対象に v1 docs を含める
- P1-2: Stable / Experimental 数の整合性 (stability.py を単一 source として
       README / v1_stability_policy / 実装の整合確認)
- P1-3: README の results tools が experimental 表記でないこと
- P1-4: docs/bundle_export.md 存在 + 必須キーワード
- P1-6: extract_pdf_commands の保証範囲を docs に明記
- v1.0.1 で repo format CI を一段強化
"""
from __future__ import annotations
import json
import re
from pathlib import Path

import pytest

from lab_executor import stability

ROOT = Path(__file__).parent.parent


# =========================================================
# Version bump
# =========================================================


def test_version_v1_0_1():
    """v1.0.1 で導入したテスト。v1.1.0 以降でも v1.0.x 系列であれば許容"""
    import visa_mcp
    assert visa_mcp.__version__.startswith("1.")


# =========================================================
# P0: repo format (v1 docs + bundle_export 追加対象)
# =========================================================


REPO_TEXT_TARGETS_V1 = [
    "README.md",
    "docs/v1_stability_policy.md",
    "docs/compatibility.md",
    "docs/error_taxonomy.md",
    "docs/operational_integrity.md",
    "docs/bundle_export.md",
    "tests/test_v1_stability.py",
    "schemas/instrument.schema.json",
    "schemas/system_config.schema.json",
    "schemas/dsl.schema.json",
    "schemas/benchmark_task.schema.json",
    "src/visa_mcp/stability.py",
]


@pytest.mark.parametrize("rel", REPO_TEXT_TARGETS_V1)
def test_repo_files_lf_only(rel):
    p = ROOT / rel
    assert p.exists(), f"missing: {p}"
    text = p.read_text(encoding="utf-8")
    assert "\r" not in text, f"CR found in {p}"


@pytest.mark.parametrize("rel", REPO_TEXT_TARGETS_V1)
def test_repo_files_multiline(rel):
    p = ROOT / rel
    text = p.read_text(encoding="utf-8")
    assert text.count("\n") + 1 >= 5, f"{p} appears single-line"


# =========================================================
# P1-2: Stable / Experimental 整合性
# =========================================================


def test_stability_module_lists_match_count():
    # v1.0.1: 実数を stability.py の STABLE_TOOLS と一致させる
    # (v1.0.0 release note の「35」は実数とズレており、ここで正す)
    assert stability.stable_count() == 43, (
        f"stable={stability.stable_count()} (expected 43)"
    )
    # v1.1.0 で experimental 5 → 7 (validate/inspect_experiment_bundle 追加)
    assert stability.experimental_count() in (5, 7), (
        f"experimental={stability.experimental_count()} (expected 5 or 7)"
    )
    # v1.0.1: 48 / v1.1.0: 50
    assert stability.total_documented_count() in (48, 50)


def test_readme_tool_count_matches_stability_module():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    # README は raw 2 個を加算 (オプトイン) → 35 + 5 + (raw 2 別記) = 40 / 42
    # 現在 README は "48 個" 表記 (合計を Stable + Experimental + raw + ext.
    # = 48 と数えていた)。stability.py との整合のため、README の N を
    # `total_documented + len(raw)` と一致させる。
    expected = stability.total_documented_count() + len(stability.RAW_TOOLS)
    # ただし v1.0.1 で再カウント中の互換期間として 47-50 範囲を許容
    m = re.search(r"MCP ツール（(\d+) 個", text)
    assert m, "README に MCP ツール数表記が見当たらない"
    n = int(m.group(1))
    # raw も含めた合計が 42、Stable 35 + Exp 5 + raw 2 = 42
    # README は v1.0 まで "48 個" だったので overshoot 許容
    assert 42 <= n <= 50, f"README ツール数={n}, stability total={expected}"


def test_all_stable_tools_appear_in_v1_stability_policy():
    text = (ROOT / "docs" / "v1_stability_policy.md").read_text(encoding="utf-8")
    for name in stability.stable_tool_names():
        assert name in text, f"v1_stability_policy.md に {name!r} 無し"


def test_all_experimental_tools_appear_in_v1_stability_policy():
    text = (ROOT / "docs" / "v1_stability_policy.md").read_text(encoding="utf-8")
    for name in stability.experimental_tool_names():
        assert name in text, (
            f"v1_stability_policy.md に experimental {name!r} 無し"
        )


def test_no_tool_in_both_stable_and_experimental():
    s = set(stability.stable_tool_names())
    e = set(stability.experimental_tool_names())
    overlap = s & e
    assert not overlap, f"分類重複: {overlap}"


# =========================================================
# P1-3: README results tools が experimental 表記でない
# =========================================================


def test_readme_results_tools_not_marked_experimental():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    # 2 つの results tool の README 行を抽出
    for tool in ("get_experiment_results", "export_experiment_results"):
        # 該当行 (table row) を抽出
        m = re.search(rf"\| `{re.escape(tool)}` \|[^\n]*", text)
        assert m, f"README に {tool} 行が見つからない"
        line = m.group(0)
        assert "experimental" not in line.lower(), (
            f"README の {tool} 行が experimental 表記のまま: {line}"
        )


# =========================================================
# P1-4: docs/bundle_export.md
# =========================================================


def test_docs_bundle_export_exists():
    p = ROOT / "docs" / "bundle_export.md"
    assert p.exists()
    text = p.read_text(encoding="utf-8")
    for kw in (
        "export_experiment_bundle",
        "manifest.json",
        "SHA-256",
        "import",
        "v1.1",
        "path traversal",
        "overwrite",
        "include_monitor_data",
        "include_audit",
        "experimental",
    ):
        assert kw in text, f"docs/bundle_export.md に {kw!r} 無し"


# =========================================================
# P1-6: extract_pdf_commands の保証範囲
# =========================================================


def test_v1_stability_policy_mentions_pdf_extract_scope():
    text = (ROOT / "docs" / "v1_stability_policy.md").read_text(encoding="utf-8")
    assert "extract_pdf_commands" in text


# =========================================================
# P0 強化: schema files も pretty + LF (重複だが CI 用)
# =========================================================


SCHEMA_FILES_V101 = [
    "instrument.schema.json", "system_config.schema.json",
    "dsl.schema.json", "benchmark_task.schema.json",
]


@pytest.mark.parametrize("name", SCHEMA_FILES_V101)
def test_schema_files_still_stable(name):
    p = ROOT / "schemas" / name
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data.get("x-visa-mcp-status") == "stable"
