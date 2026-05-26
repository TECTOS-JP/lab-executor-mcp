"""v2.4.0: Dual-path extension discovery + duplicate conflict detection."""
from __future__ import annotations
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


# ============================================================
# ExtensionPaths v2.4 schema
# ============================================================


def test_extension_paths_dual_read_paths():
    from lab_executor.extension_paths import get_extension_paths
    paths = get_extension_paths()
    # 2 path 含む
    assert len(paths.active_read_paths) == 2
    # new_path が先 (内部順序、duplicate 自動採用はしない)
    assert paths.new_path == paths.active_read_paths[0]
    assert paths.legacy_path == paths.active_read_paths[1]
    assert ".lab-executor" in str(paths.new_path)
    assert ".visa-mcp" in str(paths.legacy_path)


def test_extension_paths_write_default_still_legacy():
    """v2.4 では install default は legacy 維持。v2.5+ で切替判断。"""
    from lab_executor.extension_paths import get_extension_paths
    paths = get_extension_paths()
    assert paths.write_default == paths.legacy_path
    assert ".visa-mcp" in str(paths.write_default)
    # current_default は表示用 legacy alias
    assert paths.current_default == paths.legacy_path


def test_extension_paths_duplicate_policy_report_conflict():
    from lab_executor.extension_paths import (
        get_extension_paths, DUPLICATE_POLICY,
    )
    paths = get_extension_paths()
    assert paths.duplicate_policy == \
        "report_conflict_no_implicit_precedence"
    assert DUPLICATE_POLICY == \
        "report_conflict_no_implicit_precedence"


def test_extension_paths_to_dict_v24_schema():
    from lab_executor.extension_paths import get_extension_paths
    d = get_extension_paths().to_dict()
    for key in ("current_default", "future_default_candidate",
                "legacy_path", "new_path", "write_default",
                "active_read_paths", "duplicate_policy",
                "migration_required"):
        assert key in d, f"missing {key}"
    assert d["schema_version"] == "v2.4"
    assert d["duplicate_policy"] == \
        "report_conflict_no_implicit_precedence"


# ============================================================
# discover_installed_extensions
# ============================================================


def _make_pack(parent: Path, ext_id: str, version: str = "0.1.0") -> Path:
    parent.mkdir(parents=True, exist_ok=True)
    pack_dir = parent / ext_id.replace(".", "_")
    pack_dir.mkdir()
    (pack_dir / "extension.yaml").write_text(
        yaml.safe_dump({
            "extension_id": ext_id,
            "version": version,
            "stability": {"support_level": "experimental"},
        }),
        encoding="utf-8",
    )
    return pack_dir


def _make_paths(legacy: Path, new: Path):
    from lab_executor.extension_paths import ExtensionPaths, DUPLICATE_POLICY
    return ExtensionPaths(
        current_default=legacy,
        future_default_candidate=new,
        legacy_path=legacy,
        new_path=new,
        write_default=legacy,
        active_read_paths=[new, legacy],
        duplicate_policy=DUPLICATE_POLICY,
        migration_required=False,
    )


def test_discover_installed_extensions_from_legacy_path(tmp_path):
    from lab_executor.extension_discovery import (
        discover_installed_extensions,
    )
    legacy = tmp_path / "legacy"
    new = tmp_path / "new"
    _make_pack(legacy, "local.foo")
    res = discover_installed_extensions(_make_paths(legacy, new))
    assert len(res.extensions) == 1
    assert res.extensions[0].extension_id == "local.foo"
    assert res.extensions[0].source_path == legacy
    assert not res.has_duplicates()


def test_discover_installed_extensions_from_new_path(tmp_path):
    from lab_executor.extension_discovery import (
        discover_installed_extensions,
    )
    legacy = tmp_path / "legacy"
    new = tmp_path / "new"
    _make_pack(new, "local.bar")
    res = discover_installed_extensions(_make_paths(legacy, new))
    assert len(res.extensions) == 1
    assert res.extensions[0].extension_id == "local.bar"
    assert res.extensions[0].source_path == new


def test_discover_duplicate_extension_id_across_paths(tmp_path):
    from lab_executor.extension_discovery import (
        discover_installed_extensions,
    )
    legacy = tmp_path / "legacy"
    new = tmp_path / "new"
    _make_pack(legacy, "local.dup")
    _make_pack(new, "local.dup")
    res = discover_installed_extensions(_make_paths(legacy, new))
    assert res.has_duplicates()
    assert "local.dup" in res.duplicates
    assert len(res.duplicates["local.dup"]) == 2
    # warnings に duplicate_extension_id が含まれる
    warning_classes = [w.get("warning_class") for w in res.warnings]
    assert "duplicate_extension_id" in warning_classes


def test_discover_missing_extension_yaml(tmp_path):
    from lab_executor.extension_discovery import (
        discover_installed_extensions,
    )
    legacy = tmp_path / "legacy"
    new = tmp_path / "new"
    legacy.mkdir(parents=True)
    # pack dir はあるが extension.yaml なし
    (legacy / "broken_pack").mkdir()
    res = discover_installed_extensions(_make_paths(legacy, new))
    assert len(res.extensions) == 0
    warning_classes = [w.get("warning_class") for w in res.warnings]
    assert "missing_extension_yaml" in warning_classes


def test_discover_invalid_extension_metadata(tmp_path):
    from lab_executor.extension_discovery import (
        discover_installed_extensions,
    )
    legacy = tmp_path / "legacy"
    new = tmp_path / "new"
    legacy.mkdir(parents=True)
    pack = legacy / "no_id"
    pack.mkdir()
    # extension_id を欠く
    (pack / "extension.yaml").write_text(
        yaml.safe_dump({"version": "0.1.0"}),
        encoding="utf-8",
    )
    res = discover_installed_extensions(_make_paths(legacy, new))
    error_classes = [e.get("error_class") for e in res.errors]
    assert "invalid_extension_metadata" in error_classes


# ============================================================
# CLI: extension paths / catalog / check (v2.4)
# ============================================================


def _run_cli(*args, env=None):
    return subprocess.run(
        [sys.executable, "-m", "lab_executor.cli", *args],
        text=True, capture_output=True, encoding="utf-8",
        env=env,
    )


def test_cli_extension_paths_shows_v24_fields():
    res = _run_cli("extension", "paths", "--json")
    assert res.returncode == 0
    data = json.loads(res.stdout)
    assert data["schema_version"] == "v2.4"
    assert data["duplicate_policy"] == \
        "report_conflict_no_implicit_precedence"
    assert "legacy_path" in data
    assert "new_path" in data
    assert "write_default" in data
    assert len(data["active_read_paths"]) == 2


def test_cli_extension_catalog_help_has_strict():
    res = _run_cli("extension", "catalog", "--help")
    assert res.returncode == 0
    assert "--strict" in res.stdout


def test_cli_extension_catalog_reports_duplicates(tmp_path,
                                                    monkeypatch):
    """duplicate がある状態で catalog を呼ぶと warning status + exit 0
    (--strict なら exit 1)."""
    import os
    from lab_executor import extension_paths as ep_mod

    legacy = tmp_path / "legacy"
    new = tmp_path / "new"
    _make_pack(legacy, "local.dup")
    _make_pack(new, "local.dup")

    fake = _make_paths(legacy, new)
    monkeypatch.setattr(ep_mod, "get_extension_paths",
                        lambda: fake)
    # extension_discovery が `from extension_paths import
    # get_extension_paths` 形式で bind しているため、そちらも patch
    from lab_executor import extension_discovery as disc_mod
    monkeypatch.setattr(disc_mod, "get_extension_paths",
                        lambda: fake)

    # subprocess ではなく直接 CLI を invoke
    from lab_executor.cli import main
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["extension", "catalog", "--json"])
    out = buf.getvalue()
    data = json.loads(out)
    assert data["status"] == "warning"
    assert data["duplicate_count"] == 1
    assert rc == 0  # default: warning -> exit 0

    # --strict なら exit 1
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["extension", "catalog", "--json", "--strict"])
    assert rc == 1


def test_cli_extension_check_warns_on_duplicates(tmp_path,
                                                   monkeypatch):
    from lab_executor import extension_paths as ep_mod
    legacy = tmp_path / "legacy"
    new = tmp_path / "new"
    _make_pack(legacy, "local.dup")
    _make_pack(new, "local.dup")
    fake = _make_paths(legacy, new)
    monkeypatch.setattr(ep_mod, "get_extension_paths",
                        lambda: fake)
    # extension_discovery が `from extension_paths import
    # get_extension_paths` 形式で bind しているため、そちらも patch
    from lab_executor import extension_discovery as disc_mod
    monkeypatch.setattr(disc_mod, "get_extension_paths",
                        lambda: fake)

    from lab_executor.cli import main
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["extension", "check", "--json"])
    data = json.loads(buf.getvalue())
    # duplicate 検出が動いていること (個別 check error は test fixture
    # の限界なので status は warning|error どちらでも可)
    assert data["summary"]["duplicate_extension_ids"] == 1
    assert data["status"] in ("warning", "error")


def test_cli_extension_check_strict_fails_on_duplicates(tmp_path,
                                                          monkeypatch):
    from lab_executor import extension_paths as ep_mod
    legacy = tmp_path / "legacy"
    new = tmp_path / "new"
    _make_pack(legacy, "local.dup")
    _make_pack(new, "local.dup")
    fake = _make_paths(legacy, new)
    monkeypatch.setattr(ep_mod, "get_extension_paths",
                        lambda: fake)
    # extension_discovery が `from extension_paths import
    # get_extension_paths` 形式で bind しているため、そちらも patch
    from lab_executor import extension_discovery as disc_mod
    monkeypatch.setattr(disc_mod, "get_extension_paths",
                        lambda: fake)

    from lab_executor.cli import main
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["extension", "check", "--json", "--strict"])
    assert rc == 1


# ============================================================
# Boundary: PyVISA / visa_mcp 非依存
# ============================================================


def test_no_pyvisa_visa_mcp_import_for_extension_discovery():
    """`extension_discovery` は PyVISA / visa_mcp 無しで動く"""
    script = (
        "import sys\n"
        "class B:\n"
        "    def find_spec(self, n, p=None, t=None):\n"
        "        if n == 'visa_mcp' or n.startswith('visa_mcp.'):\n"
        "            raise ImportError('blocked')\n"
        "        if n == 'pyvisa' or n.startswith('pyvisa'):\n"
        "            raise ImportError('blocked')\n"
        "        return None\n"
        "sys.meta_path.insert(0, B())\n"
        "from lab_executor.extension_discovery import "
        "discover_installed_extensions\n"
        "from lab_executor.extension_paths import "
        "get_extension_paths\n"
        "paths = get_extension_paths()\n"
        "res = discover_installed_extensions(paths)\n"
        "assert hasattr(res, 'extensions')\n"
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


# ============================================================
# Regression: install default unchanged / tool surface unchanged
# ============================================================


def test_install_write_default_unchanged_v24():
    """v2.4 では install_extension の write 先 default は legacy"""
    from lab_executor.extension_install import default_extensions_dir
    from lab_executor.extension_paths import get_extension_paths
    paths = get_extension_paths()
    # default_extensions_dir は legacy (`.visa-mcp/extensions`)
    assert ".visa-mcp" in str(default_extensions_dir())
    # paths.write_default も legacy 一致
    assert paths.write_default == default_extensions_dir()


def test_v24_version():
    import lab_executor
    parts = lab_executor.__version__.split(".")
    assert int(parts[0]) >= 2 and int(parts[1]) >= 4


def test_mcp_tool_surface_unchanged_v24():
    """v2.4: Stable 43 + Experimental 7 = 50 不変"""
    from lab_executor import stability
    stable = [t for ts in stability.STABLE_TOOLS.values() for t in ts]
    exp = [t for ts in stability.EXPERIMENTAL_TOOLS.values() for t in ts]
    assert len(stable) == 43
    assert len(exp) == 7
