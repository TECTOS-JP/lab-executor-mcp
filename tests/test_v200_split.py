"""v2.0.0: lab-executor-mcp split smoke tests"""
from __future__ import annotations
import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent


def test_version():
    import lab_executor
    # 2.0.0 / 2.0.1 / 2.0.x patch を許容
    parts = lab_executor.__version__.split(".")
    assert int(parts[0]) >= 2 and int(parts[1]) >= 0, (
        f"unexpected version: {lab_executor.__version__}")


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
    """lab-executor runtime module の top-level に lab_visa_mcp.* が無い
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
                if node.module.startswith("lab_visa_mcp"):
                    forbidden_top_level.append(
                        (str(py.relative_to(src_root)), node.module))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("lab_visa_mcp"):
                        forbidden_top_level.append(
                            (str(py.relative_to(src_root)), alias.name))
    assert not forbidden_top_level, (
        f"top-level lab_visa_mcp imports detected: {forbidden_top_level}")


# ============================================================
# v2.0.0-rc4: line-ending / multiline guard (rc3 review P0)
# ============================================================


def test_critical_files_are_multiline_and_lf_only():
    """v2.0.0-rc4: rc3 review で raw viewer の line count mis-report が
    繰り返し問題視されたため、`.gitattributes` の効果を CI で検証する。
    主要 file が
      - 10 行以上
      - CR (`\\r`) を含まない
    ことを assert。
    """
    targets = [
        "pyproject.toml",
        ".github/workflows/ci.yml",
        "README.md",
        "CHANGELOG.md",
        "docs/v2_migration.md",
        "src/lab_executor/__init__.py",
        "src/lab_executor/cli.py",
        "src/lab_executor/backends/base.py",
        "src/lab_executor/backends/mock_backend.py",
        "tests/test_v200_split.py",
        ".gitattributes",
    ]
    failures: list[tuple[str, int, int]] = []
    for rel in targets:
        p = ROOT / rel
        if not p.exists():
            failures.append((rel, -1, -1))
            continue
        text = p.read_text(encoding="utf-8")
        lines = text.count("\n") + 1
        cr = text.count("\r")
        # __init__.py 等は短いので別 threshold
        min_lines = 5 if rel.endswith("__init__.py") else 10
        if lines < min_lines or cr > 0:
            failures.append((rel, lines, cr))
    assert not failures, (
        f"line-ending / multiline guard failed (rel, lines, CR): "
        f"{failures}")


# ============================================================
# v2.0.0-rc4: lab-executor serve placeholder behavior (rc3 review P1)
# ============================================================


def test_lab_executor_serve_is_placeholder():
    """v2.0.0 では `lab-executor serve` は placeholder で、明示的に
    exit code 2 を返す (利用者に「未実装なのに成功する」と誤解されない
    ようにする)"""
    result = subprocess.run(
        [sys.executable, "-m", "lab_executor.cli", "serve"],
        text=True, capture_output=True, encoding="utf-8",
    )
    assert result.returncode == 2, (
        f"expected exit code 2 (placeholder), got "
        f"{result.returncode}\nstderr: {result.stderr}")
    assert "v2.1" in result.stderr, (
        f"placeholder message should reference v2.1; stderr: "
        f"{result.stderr}")


def test_lab_executor_cli_version():
    """CLI --version が正しい version を返す"""
    result = subprocess.run(
        [sys.executable, "-m", "lab_executor.cli", "--version"],
        text=True, capture_output=True, encoding="utf-8",
    )
    assert result.returncode == 0
    import lab_executor
    assert lab_executor.__version__ in result.stdout
