"""v2.2.0: CLI authoring workflow + JobManager backend rename tests."""
from __future__ import annotations
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent


# ============================================================
# extension init (v2.2.0 port)
# ============================================================


def test_cli_extension_init_help():
    res = subprocess.run(
        [sys.executable, "-m", "lab_executor.cli", "extension",
         "init", "--help"],
        text=True, capture_output=True, encoding="utf-8",
    )
    assert res.returncode == 0
    assert "pack_name" in res.stdout


def test_cli_extension_init_generates_pack(tmp_path):
    res = subprocess.run(
        [sys.executable, "-m", "lab_executor.cli", "extension",
         "init", "my_pack",
         "--target-dir", str(tmp_path),
         "--template", "minimal",
         "--json"],
        text=True, capture_output=True, encoding="utf-8",
    )
    assert res.returncode == 0, res.stderr
    pack_dir = tmp_path / "my_pack"
    assert pack_dir.exists()
    assert (pack_dir / "extension.yaml").exists()


# ============================================================
# instrument scaffold (v2.2.0 port)
# ============================================================


def test_cli_instrument_scaffold_help():
    res = subprocess.run(
        [sys.executable, "-m", "lab_executor.cli", "instrument",
         "scaffold", "--help"],
        text=True, capture_output=True, encoding="utf-8",
    )
    assert res.returncode == 0
    for cat in ("power_supply", "dmm", "temperature_meter"):
        assert cat in res.stdout


def test_cli_instrument_scaffold_generates_yaml(tmp_path):
    out = tmp_path / "test_dmm.yaml"
    res = subprocess.run(
        [sys.executable, "-m", "lab_executor.cli", "instrument",
         "scaffold", "dmm",
         "--output", str(out),
         "--manufacturer", "TestCorp",
         "--model", "TM-100",
         "--json"],
        text=True, capture_output=True, encoding="utf-8",
    )
    assert res.returncode == 0, res.stderr
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert "TestCorp" in text or "TM-100" in text
    assert "support_level" in text


# ============================================================
# instrument review-report (v2.2.0 port)
# ============================================================


def test_cli_instrument_review_report_help():
    res = subprocess.run(
        [sys.executable, "-m", "lab_executor.cli", "instrument",
         "review-report", "--help"],
        text=True, capture_output=True, encoding="utf-8",
    )
    assert res.returncode == 0


# ============================================================
# diagnose tool-surface (v2.2.0)
# ============================================================


def test_cli_diagnose_tool_surface_help():
    res = subprocess.run(
        [sys.executable, "-m", "lab_executor.cli", "diagnose",
         "tool-surface", "--help"],
        text=True, capture_output=True, encoding="utf-8",
    )
    assert res.returncode == 0


def test_cli_diagnose_tool_surface_json():
    res = subprocess.run(
        [sys.executable, "-m", "lab_executor.cli", "diagnose",
         "tool-surface", "--backend", "mock", "--json"],
        text=True, capture_output=True, encoding="utf-8",
    )
    # status==warning なら exit 1, ok なら 0。どちらでも JSON が出る
    assert res.returncode in (0, 1)
    import json
    data = json.loads(res.stdout)
    assert data["declared_total"] == 50
    assert "registered_count" in data


# ============================================================
# JobManager backend keyword (v2.2.0 rename)
# ============================================================


def test_job_manager_accepts_backend_keyword():
    """v2.2.0: JobManager(backend=...) 推奨 keyword"""
    from lab_executor.backends import MockBackend
    from lab_executor.job import JobManager, JobStore
    backend = MockBackend()

    class _SF:
        def get_session(self, name): return None
        def list_sessions(self): return []

    mgr = JobManager(backend=backend, session_mgr=_SF(),
                      store=JobStore(":memory:"))
    assert mgr is not None


def test_job_manager_visa_keyword_deprecated():
    """v2.2.0: `visa=` は DeprecationWarning"""
    import warnings
    from lab_executor.backends import MockBackend
    from lab_executor.job import JobManager, JobStore
    backend = MockBackend()

    class _SF:
        def get_session(self, name): return None
        def list_sessions(self): return []

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        mgr = JobManager(visa=backend, session_mgr=_SF(),
                          store=JobStore(":memory:"))
        assert mgr is not None
    dep = [w for w in caught
           if issubclass(w.category, DeprecationWarning)]
    assert dep, f"expected DeprecationWarning, got: {caught}"


def test_job_manager_rejects_both_keywords():
    """v2.2.0: `visa=` と `backend=` 同時指定は TypeError"""
    from lab_executor.backends import MockBackend
    from lab_executor.job import JobManager, JobStore
    backend = MockBackend()

    class _SF:
        def get_session(self, name): return None

    with pytest.raises(TypeError, match="both"):
        JobManager(backend=backend, visa=backend, session_mgr=_SF(),
                    store=JobStore(":memory:"))


def test_create_server_uses_backend_keyword_path():
    """v2.2.0: server.create_server() は backend= 経由で渡すので
    DeprecationWarning が **出ない**"""
    import warnings
    from lab_executor.server import create_server
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        create_server()
    dep = [w for w in caught
           if issubclass(w.category, DeprecationWarning)
           and "visa=" in str(w.message)]
    assert not dep, (
        f"create_server() should not trigger visa= DeprecationWarning, "
        f"got: {[str(w.message) for w in dep]}")


# ============================================================
# v2.2 regression
# ============================================================


def test_v22_version():
    import lab_executor
    parts = lab_executor.__version__.split(".")
    assert int(parts[0]) >= 2 and int(parts[1]) >= 2


def test_authoring_cli_no_pyvisa_subprocess(tmp_path):
    """authoring CLI (extension init / instrument scaffold) が
    PyVISA / lab_visa_mcp なしで動く"""
    out = tmp_path / "dmm.yaml"
    script = (
        "import sys\n"
        "class B:\n"
        "    def find_spec(self, n, p=None, t=None):\n"
        "        if n == 'lab_visa_mcp' or n.startswith('lab_visa_mcp.'):\n"
        "            raise ImportError('blocked')\n"
        "        return None\n"
        "sys.meta_path.insert(0, B())\n"
        f"sys.argv = ['lab-executor','instrument','scaffold','dmm',"
        f"'--output',r'{out}','--json']\n"
        "from lab_executor.cli import main\n"
        "rc = main()\n"
        "assert rc == 0, f'rc={rc}'\n"
        "assert 'pyvisa' not in sys.modules\n"
        "print('OK')\n"
    )
    res = subprocess.run(
        [sys.executable, "-c", script],
        text=True, capture_output=True, encoding="utf-8",
    )
    assert res.returncode == 0, (
        f"stdout={res.stdout}\nstderr={res.stderr[:300]}")
    assert "OK" in res.stdout
