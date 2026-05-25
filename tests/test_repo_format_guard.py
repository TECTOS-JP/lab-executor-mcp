"""v1.9.1: Repo-wide format guard

レビュー P0-3 反映: 1〜5 行に圧縮された file を repo 全体で検出する
統合 guard。version 別の test_v***.py で個別に LF / multi-line を見る
代わりに、ここで **repo 全体を 1 つの test で sweep** し、回帰防止 +
CI lint job の単一 source of truth とする。

CI の `lint` job はこの test だけを動かせば format 違反を全部捕まえる。
"""
from __future__ import annotations
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent

# レビュー指摘のパターンを repo 全体に展開
SWEEP_PATTERNS = [
    "src/**/*.py",
    "tests/**/*.py",
    "docs/**/*.md",
    "docs/**/*.yaml",
    "docs/**/*.yml",
    ".github/workflows/**/*.yml",
    ".github/workflows/**/*.yaml",
    "examples/**/*.yaml",
    "examples/**/*.yml",
    "schemas/**/*.json",
    "registry/**/*.yaml",
    "scripts/**/*.py",
    "src/visa_mcp/templates/**/*.yaml",
]

# 5 行以下を許容する例外 (`__init__.py` 等で意図的に短いもの)
MIN_LINES_EXCEPTIONS = {
    # path: minimum lines expected (None = no minimum)
    "src/visa_mcp/dev/__init__.py": 0,
    "src/visa_mcp/templates/__init__.py": 0,
    "src/visa_mcp/templates/instruments/__init__.py": 0,
}

DEFAULT_MIN_LINES = 5


def _collect_files() -> list[Path]:
    out: set[Path] = set()
    for pat in SWEEP_PATTERNS:
        for p in ROOT.glob(pat):
            if p.is_file():
                out.add(p)
    return sorted(out)


def _rel(p: Path) -> str:
    return str(p.relative_to(ROOT)).replace("\\", "/")


def test_no_cr_in_tracked_text_files():
    """全 tracked text file に CR が含まれていないこと"""
    violations: list[tuple[str, int]] = []
    for p in _collect_files():
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        cr = text.count("\r")
        if cr:
            violations.append((_rel(p), cr))
    assert not violations, (
        f"Files with CR characters detected (use LF only): {violations}"
    )


def test_no_collapsed_single_line_files():
    """text source / docs / config の単一行潰れを検出。
    意図的に短い `__init__.py` 等は除外 (空または 1〜2 行が普通)。"""
    violations: list[tuple[str, int]] = []
    for p in _collect_files():
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        lines = text.count("\n") + 1
        rel = _rel(p)
        # __init__.py は default で除外 (空 / 1-2 行が普通)
        if p.name == "__init__.py":
            continue
        min_required = MIN_LINES_EXCEPTIONS.get(rel, DEFAULT_MIN_LINES)
        if lines < min_required:
            violations.append((rel, lines))
    assert not violations, (
        "Files appear collapsed (lines < expected minimum). This often "
        f"indicates CRLF / single-line rendering issues: {violations}"
    )


def test_yaml_workflows_parse_correctly():
    """.github/workflows/*.yml が yaml.safe_load で正しく parse できる
    + 期待 job keys を持つこと (P0-2 反映)"""
    import yaml
    workflows = list((ROOT / ".github" / "workflows").glob("*.yml"))
    assert workflows, "no workflow files found under .github/workflows/"
    for wf in workflows:
        text = wf.read_text(encoding="utf-8")
        # multi-line 確認
        assert text.count("\n") >= 10, (
            f"{wf.name}: too few lines ({text.count(chr(10))}) for a "
            "real GitHub Actions workflow"
        )
        # YAML として parse
        data = yaml.safe_load(text)
        assert isinstance(data, dict), f"{wf.name}: not a YAML mapping"
        # GitHub Actions workflow 必須 keys
        # PyYAML は `on:` を bool True と解釈するため、`on` または
        # `True` (== yaml.safe_load("on") の結果) を許容
        assert "name" in data, f"{wf.name}: missing 'name'"
        assert ("on" in data or True in data), (
            f"{wf.name}: missing 'on' trigger"
        )
        assert "jobs" in data, f"{wf.name}: missing 'jobs'"
        assert isinstance(data["jobs"], dict)
        assert data["jobs"], f"{wf.name}: no jobs defined"


def test_ci_workflow_includes_pyvisa_not_installed_job():
    """v1.9.0 で追加した pyvisa-not-installed job が CI workflow に
    存在する (回帰防止)"""
    import yaml
    ci = ROOT / ".github" / "workflows" / "ci.yml"
    assert ci.exists()
    data = yaml.safe_load(ci.read_text(encoding="utf-8"))
    jobs = data.get("jobs") or {}
    assert "pyvisa-not-installed" in jobs, (
        f"pyvisa-not-installed job missing. jobs found: {list(jobs)}"
    )
    assert "lint" in jobs
    assert "test" in jobs


def test_repo_sweep_covers_minimum_file_count():
    """sweep 対象 file が空でない (パターンが間違っていないか確認)"""
    files = _collect_files()
    assert len(files) >= 100, (
        f"only {len(files)} files swept; pattern may be misconfigured"
    )
