"""v2.0.0-rc1: lab-executor-mcp split smoke tests"""
from __future__ import annotations
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent


def test_version():
    import lab_executor
    assert lab_executor.__version__.startswith("2.0.0")


def test_top_level_imports_succeed():
    from lab_executor import (  # noqa: F401
        dsl, job, observation, extension, extension_install,
        extension_packaging, extension_catalog, extension_authoring,
        extension_integrity, instrument_authoring,
    )


def test_backends_layer_available():
    from lab_executor.backends import InstrumentBackend, MockBackend
    assert hasattr(MockBackend, "list_resources")
    assert hasattr(MockBackend, "query")
    assert hasattr(MockBackend, "write")
    assert hasattr(MockBackend, "close")


def test_dsl_compiler_importable():
    from lab_executor.dsl.compiler import validate_and_compile
    assert callable(validate_and_compile)


def test_mock_backend_basic():
    from lab_executor.backends import MockBackend
    b = MockBackend()
    assert b.backend_id == "mock"


def test_stable_tools_unchanged():
    """Stable 43 + Experimental 7 = 50 (v1.0 から不変)"""
    from lab_executor import stability
    flat = [t for ts in stability.STABLE_TOOLS.values() for t in ts]
    exp = [t for ts in stability.EXPERIMENTAL_TOOLS.values() for t in ts]
    assert len(flat) == 43
    assert len(exp) == 7


def test_no_visa_mcp_top_level_import_in_runtime_modules():
    """lab-executor runtime module の top-level に visa_mcp.* が無い
    (TYPE_CHECKING / try-ImportError fallback は許容)"""
    src_root = ROOT / "src" / "lab_executor"
    forbidden_top_level: list[tuple[str, str]] = []
    for py in src_root.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("visa_mcp"):
                    forbidden_top_level.append(
                        (str(py.relative_to(src_root)), node.module))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("visa_mcp"):
                        forbidden_top_level.append(
                            (str(py.relative_to(src_root)), alias.name))
    assert not forbidden_top_level, (
        f"top-level visa_mcp imports detected: {forbidden_top_level}")
