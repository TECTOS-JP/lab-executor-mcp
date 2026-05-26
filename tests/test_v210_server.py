"""v2.1.0: lab-executor MCP server activation tests.

v2.0 では `lab-executor serve` は placeholder だったが、v2.1 で
`serve --backend mock` を実装。本ファイルは server composition と
backend-independence を検証する。
"""
from __future__ import annotations
import ast
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent


# ============================================================
# Server composition (lab_executor.server.create_server)
# ============================================================


def test_create_server_with_default_mock_backend():
    """create_server() は default MockBackend で起動できる"""
    from lab_executor.server import create_server
    server = create_server()
    assert server is not None


def test_create_server_with_explicit_mock_backend():
    from lab_executor.server import create_server
    from lab_executor.backends import MockBackend
    backend = MockBackend()
    server = create_server(backend=backend, name="test-server")
    assert server is not None


def test_mock_server_tool_count_is_reasonable():
    """v2.1: server に MCP tool が登録される
    (Stable 43 + Experimental 7 の declaration に対し、実 registry
    数は実装で変動するが少なくとも 30 以上は登録される)"""
    from lab_executor.server import create_server, list_registered_tools
    server = create_server()
    tools = list_registered_tools(server)
    assert len(tools) >= 30, (
        f"too few tools registered: {len(tools)}")
    # 主要 stable tool は含まれる
    expected = {"validate_experiment_plan", "dry_run_plan",
                "start_experiment_job", "list_jobs", "get_job_status"}
    missing = expected - set(tools)
    assert not missing, f"missing core tools: {missing}"


def test_stability_declarations_unchanged():
    """v1.0 凍結の Stable 43 + Experimental 7 は v2.1 でも不変"""
    from lab_executor import stability
    flat = [t for ts in stability.STABLE_TOOLS.values() for t in ts]
    exp = [t for ts in stability.EXPERIMENTAL_TOOLS.values()
           for t in ts]
    assert len(flat) == 43
    assert len(exp) == 7


# ============================================================
# Backend-independence (PyVISA / visa_mcp 非依存)
# ============================================================


def test_server_module_imports_without_pyvisa():
    """`import lab_executor.server` 自体は PyVISA / visa_mcp に
    依存しない"""
    import lab_executor.server  # noqa: F401
    # backends は遅延 import される設計


def test_server_creates_without_visa_mcp_installed():
    """visa_mcp を import 経路から block しても create_server() が
    動く"""
    import sys as _sys

    class _Blocker:
        def find_spec(self, name, path=None, target=None):
            if name == "visa_mcp" or name.startswith("visa_mcp."):
                raise ImportError(f"blocked for test: {name}")
            return None

    blocker = _Blocker()
    _sys.meta_path.insert(0, blocker)
    try:
        # キャッシュ済みは削除
        for k in list(_sys.modules):
            if k.startswith("visa_mcp") or k == "lab_executor.server":
                del _sys.modules[k]
        from lab_executor.server import create_server
        server = create_server()
        assert server is not None
    finally:
        _sys.meta_path.remove(blocker)


def test_no_pyvisa_when_visa_mcp_blocked_subprocess():
    """visa-mcp を import 経路から block すると pyvisa も load されない
    (subprocess 隔離で fresh import 実行)

    注: lab-executor は visa-mcp を **optional** に扱う。dev 環境で
    visa-mcp が install 済みの場合は try-import 経由で pyvisa まで
    load されるが、これは v2.0 設計通り (lab-executor 自身は pyvisa
    を required dependency にしていない)。
    """
    script = (
        "import sys\n"
        "class B:\n"
        "    def find_spec(self, n, p=None, t=None):\n"
        "        if n == 'visa_mcp' or n.startswith('visa_mcp.'):\n"
        "            raise ImportError('blocked')\n"
        "        return None\n"
        "sys.meta_path.insert(0, B())\n"
        "from lab_executor.server import create_server\n"
        "create_server()\n"
        "assert 'pyvisa' not in sys.modules, f'leak: "
        "{[k for k in sys.modules if \"pyvisa\" in k]}'\n"
        "assert 'visa_mcp' not in sys.modules\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        text=True, capture_output=True, encoding="utf-8",
    )
    assert result.returncode == 0, (
        f"stdout: {result.stdout}\nstderr: {result.stderr[:300]}")
    assert "OK" in result.stdout


# ============================================================
# CLI: lab-executor serve --backend mock
# ============================================================


def test_cli_serve_requires_backend():
    """v2.1: `lab-executor serve` 引数なしは exit 2 + help"""
    result = subprocess.run(
        [sys.executable, "-m", "lab_executor.cli", "serve"],
        text=True, capture_output=True, encoding="utf-8",
    )
    assert result.returncode == 2
    assert "--backend" in result.stderr


def test_cli_serve_backend_mock_dry_run():
    """`lab-executor serve --backend mock --dry-run` で server を
    composition し tool 一覧を出して exit 0"""
    result = subprocess.run(
        [sys.executable, "-m", "lab_executor.cli", "serve",
         "--backend", "mock", "--dry-run"],
        text=True, capture_output=True, encoding="utf-8",
    )
    assert result.returncode == 0, (
        f"stderr: {result.stderr[:300]}")
    assert "registered tools" in result.stdout
    assert "backend_id: mock" in result.stdout


def test_cli_serve_help():
    result = subprocess.run(
        [sys.executable, "-m", "lab_executor.cli", "serve", "--help"],
        text=True, capture_output=True, encoding="utf-8",
    )
    assert result.returncode == 0
    assert "--backend" in result.stdout
    assert "mock" in result.stdout


# ============================================================
# CLI: validate extension (v2.1 port)
# ============================================================


def test_cli_validate_extension_help():
    result = subprocess.run(
        [sys.executable, "-m", "lab_executor.cli", "validate", "--help"],
        text=True, capture_output=True, encoding="utf-8",
    )
    assert result.returncode == 0
    assert "extension" in result.stdout


def test_cli_extension_help():
    result = subprocess.run(
        [sys.executable, "-m", "lab_executor.cli", "extension",
         "--help"],
        text=True, capture_output=True, encoding="utf-8",
    )
    assert result.returncode == 0
    # doctor / package / verify-package が出る
    for kw in ("doctor", "package", "verify-package"):
        assert kw in result.stdout, (
            f"missing {kw} in extension --help output")


def test_cli_extension_doctor_help():
    result = subprocess.run(
        [sys.executable, "-m", "lab_executor.cli", "extension",
         "doctor", "--help"],
        text=True, capture_output=True, encoding="utf-8",
    )
    assert result.returncode == 0


# ============================================================
# v2.1: 既存 v2.0 smoke (regression)
# ============================================================


def test_v2_1_version():
    import lab_executor
    parts = lab_executor.__version__.split(".")
    assert int(parts[0]) >= 2 and int(parts[1]) >= 1


def test_no_top_level_visa_mcp_import_added():
    """v2.1 で新規追加した src/lab_executor/server.py 等が
    top-level で visa_mcp.* を import していないこと"""
    src_root = ROOT / "src" / "lab_executor"
    forbidden: list[tuple[str, str]] = []
    for py in src_root.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("visa_mcp"):
                    forbidden.append(
                        (str(py.relative_to(src_root)), node.module))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("visa_mcp"):
                        forbidden.append(
                            (str(py.relative_to(src_root)),
                             alias.name))
    assert not forbidden, (
        f"top-level visa_mcp imports: {forbidden}")
