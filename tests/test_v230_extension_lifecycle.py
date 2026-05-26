"""v2.3.0: Extension Install / Check / Catalog + Path Migration tests."""
from __future__ import annotations
import ast
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent


# ============================================================
# extension paths (v2.3.0 new)
# ============================================================


def test_extension_paths_module_importable():
    from lab_executor.extension_paths import (
        get_extension_paths, ExtensionPaths,
    )
    paths = get_extension_paths()
    assert isinstance(paths, ExtensionPaths)


def test_extension_paths_default_legacy_path():
    """v2.3: current_default は ~/.visa-mcp/extensions/ のまま
    (v2.4: dual-read 開始、v2.5+ で切替判断)"""
    from lab_executor.extension_paths import get_extension_paths
    paths = get_extension_paths()
    assert paths.current_default.name == "extensions"
    assert ".visa-mcp" in str(paths.current_default)
    # future candidate
    assert ".lab-executor" in str(paths.future_default_candidate)
    # v2.4 では active_read_paths は dual (new → legacy)
    assert paths.current_default in paths.active_read_paths
    # migration は default では発生しない (情報提供のみ)
    assert paths.migration_required is False


def test_extension_paths_to_dict():
    from lab_executor.extension_paths import get_extension_paths
    d = get_extension_paths().to_dict()
    assert "current_default" in d
    assert "future_default_candidate" in d
    assert "active_read_paths" in d
    assert "migration_required" in d
    # v2.3 では "v2.3"、v2.4+ で "v2.4" 以上を許容
    assert d["schema_version"] in ("v2.3", "v2.4")


# ============================================================
# CLI: extension paths / install / check / catalog
# ============================================================


def test_cli_extension_paths_help():
    res = subprocess.run(
        [sys.executable, "-m", "lab_executor.cli", "extension",
         "paths", "--help"],
        text=True, capture_output=True, encoding="utf-8",
    )
    assert res.returncode == 0


def test_cli_extension_paths_json():
    res = subprocess.run(
        [sys.executable, "-m", "lab_executor.cli", "extension",
         "paths", "--json"],
        text=True, capture_output=True, encoding="utf-8",
    )
    assert res.returncode == 0
    import json
    data = json.loads(res.stdout)
    # v2.3 -> "v2.3", v2.4+ -> "v2.4" 以上
    assert data["schema_version"] in ("v2.3", "v2.4")
    assert data["migration_required"] is False


def test_cli_extension_install_help():
    res = subprocess.run(
        [sys.executable, "-m", "lab_executor.cli", "extension",
         "install", "--help"],
        text=True, capture_output=True, encoding="utf-8",
    )
    assert res.returncode == 0
    assert "zip_path" in res.stdout
    assert "--dry-run" in res.stdout


def test_cli_extension_check_help():
    res = subprocess.run(
        [sys.executable, "-m", "lab_executor.cli", "extension",
         "check", "--help"],
        text=True, capture_output=True, encoding="utf-8",
    )
    assert res.returncode == 0
    assert "--extension-id" in res.stdout
    assert "--strict" in res.stdout


def test_cli_extension_catalog_help():
    res = subprocess.run(
        [sys.executable, "-m", "lab_executor.cli", "extension",
         "catalog", "--help"],
        text=True, capture_output=True, encoding="utf-8",
    )
    assert res.returncode == 0


# ============================================================
# SessionFacade Protocol (v2.3.0 P1)
# ============================================================


def test_session_facade_protocol_importable():
    from lab_executor.session import SessionFacade
    assert SessionFacade is not None


def test_session_facade_runtime_checkable():
    """SessionFacade Protocol が runtime_checkable で、内部
    `_SessionFacade` が満たすこと"""
    from lab_executor.session import SessionFacade
    from lab_executor.server import _make_session_manager_for_backend
    from lab_executor.backends import MockBackend
    sf = _make_session_manager_for_backend(MockBackend())
    # 構造的に互換 (get_session が存在)
    assert hasattr(sf, "get_session")
    # runtime check
    assert isinstance(sf, SessionFacade)


# ============================================================
# JobManager TYPE_CHECKING cleanup (v2.3.0 P1)
# ============================================================


def test_job_manager_type_checking_no_visa_mcp_reference():
    """v2.3.0: job/manager.py の TYPE_CHECKING block 内に
    `visa_mcp.*` 参照が残っていないこと (lab_executor 側 Protocol
    へ置換済)"""
    p = ROOT / "src" / "lab_executor" / "job" / "manager.py"
    text = p.read_text(encoding="utf-8")
    tree = ast.parse(text)
    visa_mcp_in_type_checking: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        # if TYPE_CHECKING: ブロックを検出
        if not (isinstance(node.test, ast.Name)
                and node.test.id == "TYPE_CHECKING"):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.ImportFrom) and sub.module:
                if sub.module.startswith("visa_mcp"):
                    visa_mcp_in_type_checking.append(sub.module)
    assert not visa_mcp_in_type_checking, (
        f"TYPE_CHECKING block still references visa_mcp: "
        f"{visa_mcp_in_type_checking}")


# ============================================================
# Regression / version
# ============================================================


def test_v23_version():
    import lab_executor
    parts = lab_executor.__version__.split(".")
    assert int(parts[0]) >= 2 and int(parts[1]) >= 3


def test_no_pyvisa_for_extension_paths_subprocess():
    """`extension paths` は PyVISA / visa_mcp なしで動く"""
    script = (
        "import sys\n"
        "class B:\n"
        "    def find_spec(self, n, p=None, t=None):\n"
        "        if n == 'visa_mcp' or n.startswith('visa_mcp.'):\n"
        "            raise ImportError('blocked')\n"
        "        return None\n"
        "sys.meta_path.insert(0, B())\n"
        "from lab_executor.extension_paths import get_extension_paths\n"
        "paths = get_extension_paths()\n"
        "assert paths.migration_required is False\n"
        "assert 'pyvisa' not in sys.modules\n"
        "assert 'visa_mcp' not in sys.modules\n"
        "print('OK')\n"
    )
    res = subprocess.run(
        [sys.executable, "-c", script],
        text=True, capture_output=True, encoding="utf-8",
    )
    assert res.returncode == 0, (
        f"stdout={res.stdout}\nstderr={res.stderr[:300]}")
    assert "OK" in res.stdout


def test_mcp_tool_surface_unchanged():
    """v2.3.0: Stable 43 + Experimental 7 = 50 不変"""
    from lab_executor import stability
    stable = [t for ts in stability.STABLE_TOOLS.values() for t in ts]
    exp = [t for ts in stability.EXPERIMENTAL_TOOLS.values() for t in ts]
    assert len(stable) == 43
    assert len(exp) == 7
