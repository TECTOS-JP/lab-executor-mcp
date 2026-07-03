"""v0.9.2.1: external review response (P0/P1)

- P0: repository file format (LF only, multi-line)
- P1: registry INDEX entry 必須項目 / 重複 id / path 存在 / invalid_support_level error
- P1: validate plan の docstring に「schema only」記載
- docs/registry.md 存在
"""
from __future__ import annotations
import inspect
from pathlib import Path

import pytest
import yaml

from lab_executor import registry as reg

ROOT = Path(__file__).parent.parent


# =========================================================
# P0: repo format (LF only / multi-line)
# =========================================================


REPO_TEXT_TARGETS = [
    "src/lab_executor/registry.py",
    "src/lab_executor/cli.py",
    "tests/test_v092_ecosystem.py",
    "registry/INDEX.yaml",
    "registry/README.md",
    "registry/instruments/mock/mock_psu.yaml",
    "registry/instruments/mock/mock_dmm.yaml",
    "registry/instruments/mock/mock_temp.yaml",
    "schemas/benchmark_task.schema.json",
    "docs/en/quickstart.md",
    "docs/en/concepts.md",
    "docs/registry.md",
]


@pytest.mark.parametrize("rel", REPO_TEXT_TARGETS)
def test_repo_text_files_are_lf_only(rel):
    p = ROOT / rel
    assert p.exists(), f"missing: {p}"
    text = p.read_text(encoding="utf-8")
    assert "\r" not in text, f"CR found in {p}"


@pytest.mark.parametrize("rel", REPO_TEXT_TARGETS)
def test_repo_text_files_are_multiline(rel):
    """期待: 単一行に潰れていないこと (>= 5 lines)"""
    p = ROOT / rel
    text = p.read_text(encoding="utf-8")
    line_count = text.count("\n") + 1
    assert line_count >= 5, (
        f"{p} は {line_count} 行 (期待 >= 5)。改行が潰れている可能性"
    )


# =========================================================
# P1-2: registry INDEX validation 強化
# =========================================================


def _index_with(tmp_path: Path, entries: list[dict]) -> Path:
    """テスト用に最小 registry を tmp に組み立てる。

    各 entry に対応する空ファイルも作る (path 存在テスト用)。
    """
    reg_root = tmp_path / "reg"
    (reg_root / "instruments").mkdir(parents=True, exist_ok=True)
    for e in entries:
        if e.get("path"):
            target = reg_root / e["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                target.write_text(
                    "metadata:\n  manufacturer: T\n  model: M\n"
                    "  support_level: tested\ncommands: {}\n",
                    encoding="utf-8",
                )
    idx = reg_root / "INDEX.yaml"
    idx.write_text(
        yaml.safe_dump({"instruments": entries}, sort_keys=False),
        encoding="utf-8",
    )
    return idx


def test_registry_validation_detects_missing_required_fields(tmp_path):
    idx = _index_with(tmp_path, [
        {"id": "x1", "model": "X", "category": "psu",
         "path": "instruments/x.yaml"},  # vendor 欠落
    ])
    reps = reg.validate_registry(idx)
    index_rep = reps[0]
    classes = [e["error_class"] for e in index_rep.errors]
    assert "registry_entry_missing_field" in classes


def test_registry_validation_detects_duplicate_id(tmp_path):
    idx = _index_with(tmp_path, [
        {"id": "dup", "vendor": "T", "model": "A", "category": "psu",
         "path": "instruments/a.yaml"},
        {"id": "dup", "vendor": "T", "model": "B", "category": "psu",
         "path": "instruments/b.yaml"},
    ])
    reps = reg.validate_registry(idx)
    classes = [e["error_class"] for e in reps[0].errors]
    assert "registry_duplicate_id" in classes


def test_registry_validation_detects_missing_path(tmp_path):
    idx = _index_with(tmp_path, [
        {"id": "z1", "vendor": "T", "model": "Z", "category": "psu",
         "path": "instruments/missing.yaml"},
    ])
    # path 配下を消す
    target = idx.parent / "instruments" / "missing.yaml"
    if target.exists():
        target.unlink()
    reps = reg.validate_registry(idx)
    classes = [e["error_class"] for e in reps[0].errors]
    assert "registry_entry_path_not_found" in classes


def test_registry_validation_invalid_support_level_is_error(tmp_path):
    idx = _index_with(tmp_path, [
        {"id": "x", "vendor": "T", "model": "M", "category": "psu",
         "support_level": "VERY_GOOD",
         "path": "instruments/x.yaml"},
    ])
    reps = reg.validate_registry(idx)
    classes = [e["error_class"] for e in reps[0].errors]
    assert "invalid_support_level" in classes


def test_real_registry_index_passes_strict_validation():
    """現行 registry/INDEX.yaml は強化後 lint を全て通る"""
    reps = reg.validate_registry(ROOT / "registry" / "INDEX.yaml")
    # 最初の report は INDEX 自身、以降は各 entry
    assert reps[0].status in ("ok", "warning"), reps[0].errors
    assert not reps[0].errors


# =========================================================
# P1-5: validate_plan_file docstring に schema-only 注記
# =========================================================


def test_validate_plan_file_docstring_mentions_schema_only():
    doc = inspect.getdoc(reg.validate_plan_file) or ""
    assert "schema" in doc.lower()
    # 完全 validation が validate_experiment_plan であることを明記
    assert "validate_experiment_plan" in doc


# =========================================================
# docs
# =========================================================


def test_docs_registry_exists():
    p = ROOT / "docs" / "registry.md"
    assert p.exists()
    text = p.read_text(encoding="utf-8")
    # 用語揺れ + plan schema-only + verified の自己申告 を docs に書いた
    assert "vendor" in text and "manufacturer" in text
    assert "schema" in text.lower()
    assert "support_level" in text
