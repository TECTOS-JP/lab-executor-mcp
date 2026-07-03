"""v1.11.0: Separation Refactor + Split Rehearsal tests

- version
- InstrumentBackend Protocol runtime_checkable
- PyVisaBackend / MockBackend が Protocol を満たす
- runtime 候補 module が visa_manager / session_manager を top-level
  import していない (KNOWN_V111_TO_RESOLVE = 0)
- split rehearsal: lab_executor_candidate ツリー生成
- candidate が `import visa_mcp` を含まない (rewrite 後)
- docs/raw_visa.md が存在 (visa-mcp 側 v2.0 用)
- Stable 43 / Experimental 7 / 計 50 不変
"""
from __future__ import annotations
import ast
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent

# 要人間判断 (test debt triage フェーズ1):
# 以下の test は v1.11 当時の「split rehearsal (visa_mcp → lab_executor
# 移行リハーサル)」を検証するもの。v2.0 で分割は完了し、本 repo には
# src/visa_mcp ツリーが無いため split_rehearsal.generate_candidate は
# copied_count=0 になり、docs/raw_visa.md や
# src/visa_mcp/backends/pyvisa_backend.py は本 repo には存在しない
# (visa-mcp 側の資産)。移行リハーサル tool 自体が役目を終えているため、
# これらは削除候補だが安全側に倒して skip 化する。判断待ち。
# 詳細は docs/test_debt_triage.md 参照。
_SPLIT_REHEARSAL_OBSOLETE = pytest.mark.skip(
    reason=(
        "要人間判断: v1.11 split rehearsal 監査。v2.0 分割完了後の本 repo "
        "には src/visa_mcp ツリーや docs/raw_visa.md / pyvisa_backend.py が "
        "存在せず、移行リハーサル tool は役目を終えている。"
        "削除 or 保持の判断待ち (test_debt_triage.md)。"
    )
)


def test_instrument_backend_protocol_runtime_checkable():
    from visa_mcp.backends import InstrumentBackend
    from typing import get_origin
    # runtime_checkable Protocol
    assert hasattr(InstrumentBackend, "_is_runtime_protocol")
    assert InstrumentBackend._is_runtime_protocol is True


def test_pyvisa_backend_module_imports_without_instantiating():
    """v1.11.1 (P1-3): import と instance 生成を分離。
    PyVisaBackend module の import そのものは PyVISA 不要で成功する
    (型注釈は TYPE_CHECKING 内、内部の VisaManager 生成は __init__
    で初めて起こる)。"""
    import importlib
    mod = importlib.import_module("visa_mcp.backends.pyvisa_backend")
    assert hasattr(mod, "PyVisaBackend")


def test_pyvisa_backend_class_satisfies_protocol_shape():
    """class level の structural shape check (instance を作らない)"""
    from visa_mcp.backends import PyVisaBackend
    for name in ("backend_id", "list_resources", "query", "write",
                  "close"):
        assert hasattr(PyVisaBackend, name), (
            f"PyVisaBackend missing {name}")


def test_mock_backend_satisfies_protocol():
    from visa_mcp.backends import InstrumentBackend, MockBackend
    for name in ("backend_id", "list_resources", "query", "write",
                  "close"):
        assert hasattr(MockBackend, name), (
            f"MockBackend missing {name}")


def test_pyvisa_backend_constructible_without_explicit_visa(monkeypatch):
    """PyVisaBackend() が VisaManager を内部生成できる
    (PyVISA が install 済みの環境で動く)"""
    from visa_mcp.backends import PyVisaBackend
    b = PyVisaBackend()
    assert b.backend_id == "pyvisa"
    assert b.visa_manager is not None


def test_mock_backend_constructible_without_explicit_visa():
    from visa_mcp.backends import MockBackend
    b = MockBackend()
    assert b.backend_id == "mock"
    assert b.mock_visa is not None


def test_no_known_v1_11_violations():
    """v1.11 gate: KNOWN_V111_TO_RESOLVE = 0 件"""
    from lab_executor.dev.ownership_check import (
        KNOWN_V111_TO_RESOLVE, collect_report,
    )
    assert len(KNOWN_V111_TO_RESOLVE) == 0, (
        "v1.11 gate violated: KNOWN_V111_TO_RESOLVE must be empty. "
        f"残: {KNOWN_V111_TO_RESOLVE}")
    rep = collect_report()
    assert rep["lab_to_visa_top_level_violations"] == [], (
        f"NEW violation: {rep['lab_to_visa_top_level_violations']}")
    assert rep["known_v1_11_to_resolve_count"] == 0


def test_runtime_modules_no_toplevel_visa_manager_import():
    """lab-executor owner module の top-level に
    visa_manager / session_manager / pyvisa が無いこと"""
    forbidden = {
        "visa_mcp.visa_manager", "visa_mcp.session_manager",
        "pyvisa",
    }
    # 例外: lazy import が許容される module
    lazy_exceptions = {
        "lab_executor.testing.mock_instruments",
        # backends/pyvisa_backend.py は visa-mcp owner なので除外
    }

    import yaml
    manifest = yaml.safe_load(
        (ROOT / "docs" / "separation" / "module_ownership.yaml")
        .read_text(encoding="utf-8")
    )
    lab_modules = {
        m for m, info in (manifest.get("modules") or {}).items()
        if (info or {}).get("owner") == "lab-executor-mcp"
    }

    violations: list[tuple[str, str]] = []
    for mod in lab_modules:
        if mod in lazy_exceptions:
            continue
        parts = mod.split(".")
        p = ROOT / "src" / Path(*parts).with_suffix(".py")
        if not p.exists():
            p = ROOT / "src" / Path(*parts) / "__init__.py"
        if not p.exists():
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                if node.module in forbidden:
                    violations.append((mod, node.module))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in forbidden:
                        violations.append((mod, alias.name))

    assert not violations, (
        f"top-level forbidden imports detected: {violations}")


@_SPLIT_REHEARSAL_OBSOLETE
def test_split_rehearsal_generates_candidate(tmp_path):
    """split_rehearsal が candidate tree を生成"""
    from lab_executor.dev.split_rehearsal import generate_candidate
    out = tmp_path / "lab_executor_candidate"
    summary = generate_candidate(out)
    assert out.exists()
    assert (out / "__init__.py").exists()
    assert summary["copied_count"] >= 30, (
        f"copied too few: {summary['copied_count']}")
    # 主要 runtime module が含まれる
    for rel in ("dsl", "job", "extension.py", "observation.py"):
        candidate_path = out / rel
        if not candidate_path.exists():
            # may be `dsl/__init__.py` etc.
            assert any(out.rglob(rel.replace(".py", "*"))), (
                f"missing: {rel}")


def test_split_rehearsal_candidate_has_no_visa_mcp_imports(tmp_path):
    """生成 candidate ツリー内に `import visa_mcp.<lab module>`
    が残っていない (visa-mcp owner / shared module の import は許容)"""
    from lab_executor.dev.split_rehearsal import generate_candidate
    out = tmp_path / "lab_executor_candidate"
    generate_candidate(out)
    import yaml
    manifest = yaml.safe_load(
        (ROOT / "docs" / "separation" / "module_ownership.yaml")
        .read_text(encoding="utf-8")
    )
    lab_modules = {
        m for m, info in (manifest.get("modules") or {}).items()
        if (info or {}).get("owner") == "lab-executor-mcp"
    }
    failures: list[tuple[str, str]] = []
    for py in out.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        for lab in lab_modules:
            # `from visa_mcp.<lab>` または `import visa_mcp.<lab>`
            # が残っているか
            patterns = [
                f"from {lab}",
                f"import {lab}",
            ]
            for pat in patterns:
                if pat in text:
                    failures.append((str(py.relative_to(out)), pat))
    assert not failures, (
        f"candidate に rewrite 漏れ: {failures[:5]}...")


@_SPLIT_REHEARSAL_OBSOLETE
def test_split_rehearsal_cli_runs(tmp_path):
    out = tmp_path / "cli_candidate"
    res = subprocess.run(
        [sys.executable, "-m", "lab_executor.dev.split_rehearsal",
         "--out", str(out), "--json"],
        cwd=str(ROOT),
        capture_output=True, text=True,
    )
    assert res.returncode == 0, (
        f"stdout: {res.stdout}\nstderr: {res.stderr}")
    import json as _json
    data = _json.loads(res.stdout)
    assert data["copied_count"] >= 1
    assert out.exists()


@_SPLIT_REHEARSAL_OBSOLETE
def test_raw_visa_doc_exists():
    p = ROOT / "docs" / "raw_visa.md"
    assert p.exists()
    text = p.read_text(encoding="utf-8")
    assert "Raw VISA backend" in text
    assert "PyVisaBackend" in text
    assert "VISA_MCP_ALLOW_RAW" in text
    # multi-line + LF only
    assert text.count("\n") >= 30
    assert "\r" not in text


def test_stable_tool_count_unchanged():
    """Stable 43 / Experimental 7 / 計 50 が v1.11 でも不変
    (stability.STABLE_TOOLS は category -> list なので flatten で count)"""
    from visa_mcp import stability
    stable_flat = [t for ts in stability.STABLE_TOOLS.values() for t in ts]
    exp_flat = [
        t for ts in stability.EXPERIMENTAL_TOOLS.values() for t in ts
    ]
    assert len(stable_flat) == 43, (
        f"stable count drift: {len(stable_flat)}, names={stable_flat}")
    assert len(exp_flat) == 7, (
        f"experimental count drift: {len(exp_flat)}, names={exp_flat}")


def test_backends_init_exposes_adapters():
    from visa_mcp import backends
    assert hasattr(backends, "InstrumentBackend")
    assert hasattr(backends, "PyVisaBackend")
    assert hasattr(backends, "MockBackend")


@_SPLIT_REHEARSAL_OBSOLETE
def test_split_rehearsal_verify_candidate(tmp_path):
    """v1.11.1 (P1-4): verify_candidate が AST parse + leftover 検査
    を実行し、生成直後の candidate に対し OK を返す"""
    from lab_executor.dev.split_rehearsal import (
        generate_candidate, verify_candidate,
    )
    out = tmp_path / "cand_verify"
    generate_candidate(out)
    rep = verify_candidate(out)
    assert rep["ok"], (
        f"verify failed: parse_errors={rep['parse_errors'][:3]}, "
        f"leftover={rep['leftover_visa_mcp'][:3]}")
    assert rep["parse_ok_count"] >= 30


def test_split_rehearsal_cli_verify_flag(tmp_path):
    out = tmp_path / "cand_cli_verify"
    res = subprocess.run(
        [sys.executable, "-m", "lab_executor.dev.split_rehearsal",
         "--out", str(out), "--verify", "--json"],
        cwd=str(ROOT),
        capture_output=True, text=True,
    )
    assert res.returncode == 0, res.stderr
    import json as _json
    data = _json.loads(res.stdout)
    assert "verify" in data
    assert data["verify"]["ok"] is True


@_SPLIT_REHEARSAL_OBSOLETE
def test_v111_new_files_covered_by_format_guard():
    """v1.11.1 (P0-2): v1.11 で追加した新規 file が
    repo-wide format guard (SWEEP_PATTERNS) でカバーされる"""
    from tests.test_repo_format_guard import (
        SWEEP_PATTERNS, _collect_files,
    )
    files = {str(p.relative_to(ROOT)).replace("\\", "/")
             for p in _collect_files()}
    must_cover = [
        "src/visa_mcp/backends/base.py",
        "src/visa_mcp/backends/pyvisa_backend.py",
        "src/visa_mcp/backends/mock_backend.py",
        "src/visa_mcp/dev/split_rehearsal.py",
        "tests/test_v111_separation_refactor.py",
        "docs/raw_visa.md",
        "docs/separation/module_ownership.yaml",
    ]
    missing = [m for m in must_cover if m not in files]
    assert not missing, (
        f"format guard SWEEP_PATTERNS が以下を見ていない: {missing}")


@_SPLIT_REHEARSAL_OBSOLETE
def test_v111_new_files_are_multiline():
    """v1.11.1 (P0): v1.11 で追加した主要 file が multi-line
    (>= 30 行) + LF only で保存されている"""
    targets = [
        "src/visa_mcp/backends/base.py",
        "src/visa_mcp/backends/pyvisa_backend.py",
        "src/visa_mcp/backends/mock_backend.py",
        "src/visa_mcp/dev/split_rehearsal.py",
        "tests/test_v111_separation_refactor.py",
        "docs/raw_visa.md",
    ]
    failures: list[tuple[str, int, int]] = []
    for rel in targets:
        p = ROOT / rel
        text = p.read_text(encoding="utf-8")
        lines = text.count("\n") + 1
        cr = text.count("\r")
        if lines < 30 or cr:
            failures.append((rel, lines, cr))
    assert not failures, (
        f"multiline 違反 (rel, lines, CR): {failures}")
