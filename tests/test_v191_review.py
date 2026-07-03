"""v1.9.1: v1.9.0 review response

- P0-1: raw 改行 / multi-line (新規 / 既存 v1.9 file)
- P0-2: .github/workflows/ci.yml が正しい YAML として読める +
  pyvisa-not-installed job が存在
- P0-3: repo-wide format guard が CR / 5 行未満を検出できる
- P1-4: docs/separation/notes.md に boundary test の限界が明記
- P1-5: dependency_report が top-level import 限定であることを docs 化
- P1-6: promote-check の昇格ルールを docs 化
- P1-7: registry.py 分割候補が docs/separation/notes.md に TODO 化
- P2-8: category canonicalization policy が docs 化
"""
from __future__ import annotations
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parent.parent


# =========================================================
# Version
# =========================================================


V19_FILES_FULL = [
    "src/lab_executor/registry.py",
    "src/lab_executor/cli.py",
    "src/lab_executor/dev/dependency_report.py",
    "src/lab_executor/instrument_authoring.py",
    "src/lab_executor/extension_authoring.py",
    "docs/separation/notes.md",
    "docs/instrument_promote_check.md",
    "docs/category_policy.md",
    "tests/test_separation_boundary.py",
    "tests/test_v19_instrument_quality.py",
    "tests/test_v191_review.py",
    "tests/test_repo_format_guard.py",
    ".github/workflows/ci.yml",
    "CHANGELOG.md",
]


@pytest.mark.parametrize("rel", V19_FILES_FULL)
def test_v191_lf_only(rel):
    p = ROOT / rel
    assert p.exists(), f"missing: {p}"
    text = p.read_text(encoding="utf-8")
    assert "\r" not in text, f"{rel} に CR 含む"


@pytest.mark.parametrize("rel", V19_FILES_FULL)
def test_v191_multiline(rel):
    p = ROOT / rel
    text = p.read_text(encoding="utf-8")
    assert text.count("\n") + 1 >= 5, f"{rel} が 5 行未満"


# =========================================================
# P0-2: CI workflow YAML parse + 必須 job
# =========================================================


def test_ci_workflow_yaml_parses_with_real_multiline():
    ci = ROOT / ".github" / "workflows" / "ci.yml"
    text = ci.read_text(encoding="utf-8")
    # local 上では 60 行以上ある (raw GitHub の圧縮表示は別問題)
    assert text.count("\n") >= 50
    data = yaml.safe_load(text)
    assert isinstance(data, dict)
    # PyYAML quirk: `on:` は True に bool 化される可能性
    assert "name" in data
    assert ("on" in data or True in data)
    assert "jobs" in data


def test_ci_workflow_has_pyvisa_not_installed_job():
    ci = ROOT / ".github" / "workflows" / "ci.yml"
    data = yaml.safe_load(ci.read_text(encoding="utf-8"))
    jobs = data["jobs"]
    assert "pyvisa-not-installed" in jobs
    # job 内に dependency_report と test_separation_boundary が含まれる
    steps_text = yaml.safe_dump(jobs["pyvisa-not-installed"])
    assert "dependency_report" in steps_text
    assert "test_separation_boundary" in steps_text


def test_ci_workflow_has_lint_job_running_repo_guard():
    """P0-3: lint job が repo-wide format guard を実行する"""
    ci = ROOT / ".github" / "workflows" / "ci.yml"
    data = yaml.safe_load(ci.read_text(encoding="utf-8"))
    jobs = data["jobs"]
    assert "lint" in jobs
    steps_text = yaml.safe_dump(jobs["lint"])
    assert "test_repo_format_guard" in steps_text


# =========================================================
# P0-3: repo-wide guard が動作する (test_repo_format_guard.py 経由で
# 既に存在することを確認)
# =========================================================


def test_repo_format_guard_module_exists():
    p = ROOT / "tests" / "test_repo_format_guard.py"
    assert p.exists()
    text = p.read_text(encoding="utf-8")
    for kw in (
        "SWEEP_PATTERNS",
        "test_no_cr_in_tracked_text_files",
        "test_no_collapsed_single_line_files",
        "test_yaml_workflows_parse_correctly",
        "test_ci_workflow_includes_pyvisa_not_installed_job",
        ".github/workflows",
        "schemas/**/*.json",
        "src/lab_executor/templates",
    ):
        assert kw in text, f"test_repo_format_guard.py に {kw!r} 無し"


# =========================================================
# P1-4: separation notes に boundary limitations
# =========================================================


def test_separation_notes_mentions_boundary_limitations():
    text = (ROOT / "docs" / "separation" / "notes.md").read_text(
        encoding="utf-8")
    for kw in (
        "import-time coupling only",
        "Function-level lazy imports are allowed",
        "v1.10",
        "v1.11",
    ):
        assert kw in text, f"separation/notes.md に {kw!r} 無し"


# =========================================================
# P1-5: dependency_report top-level limited であることを docs に
# =========================================================


def test_separation_notes_mentions_top_level_only():
    text = (ROOT / "docs" / "separation" / "notes.md").read_text(
        encoding="utf-8")
    assert ("top-level" in text or "top level" in text
            or "module top-level" in text)
    # pyvisa CI 戦略の docs
    assert "pyvisa-not-installed" in text or "PyVISA" in text


def test_separation_notes_mentions_pyvisa_ci_v2_strategy():
    """P1: pyvisa CI 戦略 (v2.0 で lab-executor 側は base install で
    pyvisa を引かない方針) が記述されている"""
    text = (ROOT / "docs" / "separation" / "notes.md").read_text(
        encoding="utf-8")
    for kw in ("v1.9", "v2.0", "lab-executor"):
        assert kw in text, f"separation/notes.md に {kw!r} 無し"


# =========================================================
# P1-6: promote-check の昇格ルールが docs 化
# =========================================================


def test_promote_check_doc_exists_and_has_rules():
    p = ROOT / "docs" / "instrument_promote_check.md"
    assert p.exists()
    text = p.read_text(encoding="utf-8")
    for kw in (
        "promote-check",
        "draft", "tested", "verified",
        "validation_evidence",
        "下方移動",
        "strict validation",
        "tested → verified",
        "eligible",
        "blocking_issues",
    ):
        assert kw in text, f"instrument_promote_check.md に {kw!r} 無し"


def test_promote_check_doc_has_exit_codes():
    text = (ROOT / "docs" / "instrument_promote_check.md").read_text(
        encoding="utf-8")
    assert "exit code" in text.lower() or "終了コード" in text


# =========================================================
# P1-7: registry.py 分割候補が docs に TODO 化
# =========================================================


def test_separation_notes_lists_registry_split_candidates():
    text = (ROOT / "docs" / "separation" / "notes.md").read_text(
        encoding="utf-8")
    for kw in (
        "registry.py",
        "split",
        "instrument_validation",
        "strict_checks",
        "category_policy",
    ):
        assert kw in text, f"separation/notes.md に {kw!r} 無し"


# =========================================================
# P2-8: category canonicalization policy
# =========================================================


def test_category_policy_doc_exists_with_canonical_list():
    p = ROOT / "docs" / "category_policy.md"
    assert p.exists()
    text = p.read_text(encoding="utf-8")
    for kw in (
        "canonical",
        "alias",
        "power_supply", "dmm", "smu",
        "function_generator", "electronic_load",
        "temperature_controller", "actuator",
        "multimeter",  # alias 表記
        "psu",         # alias 表記
        "OUTPUT_CAPABLE_CATEGORIES",
        "CATEGORY_ALIASES",
        "normalize_category",
    ):
        assert kw in text, f"category_policy.md に {kw!r} 無し"


def test_category_policy_doc_matches_implementation():
    """docs の canonical 一覧と実装の OUTPUT_CAPABLE_CATEGORIES が
    一致している (回帰防止)"""
    from lab_executor.registry import (
        OUTPUT_CAPABLE_CATEGORIES, CATEGORY_ALIASES,
    )
    text = (ROOT / "docs" / "category_policy.md").read_text(
        encoding="utf-8")
    # 各 output-capable が docs に明記されている
    for cat in OUTPUT_CAPABLE_CATEGORIES:
        assert cat in text, f"category_policy.md に {cat!r} 無し"
    # alias が docs に明記されている
    for alias in CATEGORY_ALIASES:
        assert alias in text, (
            f"category_policy.md に alias {alias!r} 無し"
        )


# =========================================================
# CHANGELOG
# =========================================================

