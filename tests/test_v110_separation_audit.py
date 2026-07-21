"""v1.10.0: Separation Readiness Audit tests

- pyproject / __version__ が 1.10.0
- module_ownership.yaml が全 module をカバー (unclassified=0)
- split_manifest.yaml が path として実在 (or directory)
- ownership_check が 新規 lab→visa top-level violation を出さない
  (KNOWN_V111_TO_RESOLVE のみが残る)
- dependency_graph.md 生成
- instrument review-report CLI が markdown report を出す
- review_report_instrument() が ok/warning/error を返す
"""
from __future__ import annotations
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parent.parent

# 要人間判断 (test debt triage フェーズ1):
# 以下 2 test は v1.10 当時「visa-mcp からの分割準備」を監査するもので、
# v1.10 分割準備監査のうち、分割前の lab_visa_mcp ツリーを前提とするテスト
# (test_module_ownership_manifest_complete / test_split_manifest_paths_exist)
# は v2.0 分割完了に伴い 2026-07-04 に削除した (人間判断済み)。
# 経緯: docs/test_debt_triage.md


def test_version_is_1_10_or_later():
    from lab_visa_mcp import __version__
    # v1.10.* or later (v1.11.* / v1.12.* / v2.0 等を許容)
    parts = [int(p) for p in __version__.split(".")[:2]]
    major, minor = parts[0], parts[1]
    assert (major, minor) >= (1, 10), (
        f"v1.10 audit テストは v1.10+ で動く想定 (got {__version__})")


def test_no_new_lab_to_visa_top_level_violations():
    """v1.10 では NEW violation は 0 件、KNOWN は tracking のみ"""
    from lab_executor.dev.ownership_check import collect_report
    rep = collect_report()
    assert rep["lab_to_visa_top_level_violations"] == [], (
        f"新規 violation: {rep['lab_to_visa_top_level_violations']}\n"
        f"v1.11 で解消予定なら KNOWN_V111_TO_RESOLVE に追加してください")


def test_known_v1_11_to_resolve_tracked():
    """v1.10 で 10 件登録した既知 violation の tracking が機能する
    (v1.11 で 0 件まで削減されたため、件数の上限のみ確認)"""
    from lab_executor.dev.ownership_check import (
        KNOWN_V111_TO_RESOLVE, collect_report,
    )
    rep = collect_report()
    # report で known_pending として一致登録されている (0 でも OK)
    assert rep["known_v1_11_to_resolve_count"] == len(
        KNOWN_V111_TO_RESOLVE)


def test_dependency_graph_generated(tmp_path):
    """ownership_check --graph-md で markdown が生成できる"""
    from lab_executor.dev.ownership_check import collect_report, render_graph_md
    rep = collect_report()
    md = render_graph_md(rep)
    assert "# Dependency Graph Report" in md
    assert "Owner counts" in md
    assert "## Statistics" in md


def test_dependency_graph_md_committed_multiline():
    """v1.10.1 P0-3: docs/separation/dependency_graph.md が
    実体として multi-line で commit されている (raw 上で潰れていない)"""
    p = ROOT / "docs" / "separation" / "dependency_graph.md"
    assert p.exists(), f"{p} が存在しない"
    text = p.read_text(encoding="utf-8")
    lines = text.count("\n") + 1
    assert lines >= 20, (
        f"dependency_graph.md が単一行に潰れている疑い (lines={lines}). "
        f"`python -m lab_executor.dev.ownership_check --graph-md "
        f"docs/separation/dependency_graph.md` で再生成してください"
    )
    # 期待 section
    for section in ("# Dependency Graph Report",
                     "## Statistics",
                     "## Owner counts"):
        assert section in text, f"missing section: {section}"
    assert "\r" not in text, "CR が混入 (LF のみで保存)"


def test_module_ownership_statistics_match():
    """v1.10.1 P1-4: module_ownership.yaml の statistics block が
    実 manifest の owner 別 module count と一致していること"""
    from collections import Counter
    manifest_path = ROOT / "docs" / "separation" / "module_ownership.yaml"
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    stats = data.get("statistics") or {}
    modules = data.get("modules") or {}
    counter: Counter = Counter()
    for info in modules.values():
        owner = (info or {}).get("owner", "unknown")
        counter[owner] += 1
    expected = {
        "lab_executor_mcp_count": counter.get("lab-executor-mcp", 0),
        "visa_mcp_count": counter.get("visa-mcp", 0),
        "split_count": counter.get("split", 0),
        "shared_count": counter.get("shared", 0),
    }
    for key, want in expected.items():
        got = stats.get(key)
        assert got == want, (
            f"statistics.{key}: manifest 宣言 {got} != "
            f"実 owner count {want}")


def test_module_ownership_yaml_not_collapsed():
    """v1.10.1 P0-1: module_ownership.yaml / split_manifest.yaml が
    multi-line で保存されている"""
    for rel in ("docs/separation/module_ownership.yaml",
                "docs/separation/split_manifest.yaml"):
        p = ROOT / rel
        text = p.read_text(encoding="utf-8")
        lines = text.count("\n") + 1
        assert lines >= 30, (
            f"{rel} が圧縮されている疑い (lines={lines})")
        assert "\r" not in text, f"{rel} に CR が混入"


def test_ownership_check_cli_exit_zero(tmp_path):
    """CLI exit code = 0 (NEW violation 無し)"""
    res = subprocess.run(
        [sys.executable, "-m", "lab_executor.dev.ownership_check"],
        cwd=str(ROOT),
        capture_output=True, text=True,
    )
    assert res.returncode == 0, (
        f"stdout: {res.stdout}\nstderr: {res.stderr}")
    assert "unclassified: 0" in res.stdout


def test_ownership_check_json_output():
    res = subprocess.run(
        [sys.executable, "-m", "lab_executor.dev.ownership_check", "--json"],
        cwd=str(ROOT),
        capture_output=True, text=True,
    )
    assert res.returncode == 0
    data = json.loads(res.stdout)
    assert "unclassified_modules" in data
    assert "lab_to_visa_top_level_violations" in data
    assert "known_v1_11_to_resolve" in data


def test_review_report_function_ok():
    """review_report_instrument が valid YAML に対し markdown を返す"""
    from lab_executor.instrument_authoring import review_report_instrument
    # registry の verified instrument を使う
    candidates = list((ROOT / "registry").rglob("*.yaml"))
    # registry index 以外
    candidates = [c for c in candidates if "INDEX" not in c.name]
    assert candidates, "registry に instrument YAML が無い"
    target = candidates[0]
    res = review_report_instrument(target)
    assert res["status"] in ("ok", "warning", "error")
    assert "# Instrument review report" in res["markdown"]
    assert res["file"] == str(target.expanduser())


def test_review_report_function_missing_file():
    from lab_executor.instrument_authoring import review_report_instrument
    res = review_report_instrument("/nonexistent/path.yaml")
    assert res["status"] == "error"
    assert "not found" in res["markdown"]
    assert "# Instrument review report" in res["markdown"]


def test_review_report_cli(tmp_path):
    """visa-mcp instrument review-report CLI が動く"""
    candidates = list((ROOT / "registry").rglob("*.yaml"))
    candidates = [c for c in candidates if "INDEX" not in c.name]
    assert candidates
    target = candidates[0]
    out = tmp_path / "review.md"
    res = subprocess.run(
        [sys.executable, "-m", "lab_visa_mcp.cli", "instrument",
         "review-report", str(target), "--output", str(out)],
        cwd=str(ROOT),
        capture_output=True, text=True,
    )
    assert res.returncode in (0, 1), (
        f"stdout: {res.stdout}\nstderr: {res.stderr}")
    assert out.exists()
    md = out.read_text(encoding="utf-8")
    assert "# Instrument review report" in md


def test_review_report_cli_json(tmp_path):
    candidates = list((ROOT / "registry").rglob("*.yaml"))
    candidates = [c for c in candidates if "INDEX" not in c.name]
    target = candidates[0]
    res = subprocess.run(
        [sys.executable, "-m", "lab_visa_mcp.cli", "instrument",
         "review-report", str(target), "--json"],
        cwd=str(ROOT),
        capture_output=True, text=True,
    )
    assert res.returncode in (0, 1)
    data = json.loads(res.stdout)
    assert "review_report" in data
    assert "markdown" in data["review_report"]
    assert "status" in data["review_report"]
