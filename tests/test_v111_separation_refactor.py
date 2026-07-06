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
# 移行リハーサル)」を検証するものだったが、v2.0 で分割が完了し役目を
# 終えたため、split_rehearsal 監査テスト群 8 件は 2026-07-04 に削除した
# (人間判断済み)。経緯: docs/test_debt_triage.md


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
