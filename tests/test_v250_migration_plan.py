"""v2.5.0: Extension Migration Plan + resolve_extension_by_id tests."""
from __future__ import annotations
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


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


# ============================================================
# plan_extension_migration
# ============================================================


def test_migration_plan_no_extensions(tmp_path):
    from lab_executor.extension_migration import plan_extension_migration
    paths = _make_paths(tmp_path / "legacy", tmp_path / "new")
    plan = plan_extension_migration(paths=paths)
    assert plan.status == "ok"
    s = plan.summary
    assert s["legacy_only"] == 0
    assert s["new_only"] == 0
    assert s["duplicates"] == 0
    assert s["migration_required"] is False
    assert plan.actions == []


def test_migration_plan_legacy_only(tmp_path):
    from lab_executor.extension_migration import plan_extension_migration
    legacy = tmp_path / "legacy"
    new = tmp_path / "new"
    _make_pack(legacy, "local.a")
    _make_pack(legacy, "local.b")
    plan = plan_extension_migration(paths=_make_paths(legacy, new))
    assert plan.status == "warning"
    assert plan.summary["legacy_only"] == 2
    assert plan.summary["new_only"] == 0
    assert plan.summary["migration_required"] is True
    # legacy_only 件分の copy candidate
    copy_actions = [a for a in plan.actions
                    if a.action == "candidate_copy_to_new_path"]
    assert len(copy_actions) == 2
    for a in copy_actions:
        assert a.severity == "info"
        assert a.apply_available is False


def test_migration_plan_new_only(tmp_path):
    from lab_executor.extension_migration import plan_extension_migration
    legacy = tmp_path / "legacy"
    new = tmp_path / "new"
    _make_pack(new, "local.x")
    plan = plan_extension_migration(paths=_make_paths(legacy, new))
    # new_only のみなら migration 不要
    assert plan.status == "ok"
    assert plan.summary["legacy_only"] == 0
    assert plan.summary["new_only"] == 1
    assert plan.summary["migration_required"] is False


def test_migration_plan_duplicate(tmp_path):
    from lab_executor.extension_migration import plan_extension_migration
    legacy = tmp_path / "legacy"
    new = tmp_path / "new"
    _make_pack(legacy, "local.dup")
    _make_pack(new, "local.dup")
    plan = plan_extension_migration(paths=_make_paths(legacy, new))
    assert plan.status == "error"
    assert plan.summary["duplicates"] == 1
    assert plan.summary["migration_required"] is True
    dup_actions = [a for a in plan.actions
                   if a.action == "resolve_duplicate_extension_id"]
    assert len(dup_actions) == 1
    assert dup_actions[0].severity == "error"
    assert dup_actions[0].extension_id == "local.dup"
    assert len(dup_actions[0].locations) == 2


def test_migration_plan_invalid_metadata(tmp_path):
    from lab_executor.extension_migration import plan_extension_migration
    legacy = tmp_path / "legacy"
    new = tmp_path / "new"
    legacy.mkdir(parents=True)
    p = legacy / "broken"
    p.mkdir()
    # extension_id を欠く YAML
    (p / "extension.yaml").write_text(
        yaml.safe_dump({"version": "0.1"}), encoding="utf-8",
    )
    plan = plan_extension_migration(paths=_make_paths(legacy, new))
    assert plan.status == "error"
    assert plan.summary["invalid"] >= 1
    fix_actions = [a for a in plan.actions
                   if a.action == "fix_invalid_extension_metadata"]
    assert len(fix_actions) >= 1


def test_migration_plan_does_not_write_files(tmp_path):
    """migration-plan は実ファイルを変更しない"""
    from lab_executor.extension_migration import plan_extension_migration
    legacy = tmp_path / "legacy"
    new = tmp_path / "new"
    _make_pack(legacy, "local.x")
    # snapshot before
    legacy_files_before = set(p.name for p in legacy.rglob("*"))
    new_exists_before = new.exists()

    plan_extension_migration(paths=_make_paths(legacy, new))

    legacy_files_after = set(p.name for p in legacy.rglob("*"))
    assert legacy_files_before == legacy_files_after
    # new path を勝手に作らない
    assert new.exists() == new_exists_before


def test_migration_plan_json_schema(tmp_path):
    from lab_executor.extension_migration import plan_extension_migration
    plan = plan_extension_migration(
        paths=_make_paths(tmp_path / "L", tmp_path / "N"))
    d = plan.to_dict()
    for k in ("status", "legacy_path", "new_path", "write_default",
              "active_read_paths", "duplicate_policy", "summary",
              "actions", "schema_version"):
        assert k in d
    assert d["schema_version"] == "v2.5"


# ============================================================
# resolve_extension_by_id
# ============================================================


def test_resolve_extension_by_id_ok(tmp_path):
    from lab_executor.extension_discovery import resolve_extension_by_id
    legacy = tmp_path / "legacy"
    new = tmp_path / "new"
    _make_pack(legacy, "local.only")
    paths = _make_paths(legacy, new)
    ext = resolve_extension_by_id("local.only", paths=paths)
    assert ext.extension_id == "local.only"


def test_resolve_extension_by_id_not_found(tmp_path):
    from lab_executor.extension_discovery import (
        resolve_extension_by_id, ExtensionResolveError,
    )
    legacy = tmp_path / "legacy"
    new = tmp_path / "new"
    paths = _make_paths(legacy, new)
    with pytest.raises(ExtensionResolveError) as excinfo:
        resolve_extension_by_id("local.nope", paths=paths)
    assert excinfo.value.error_class == "extension_not_found"
    assert excinfo.value.to_dict()["error_class"] == "extension_not_found"


def test_resolve_extension_by_id_duplicate_error(tmp_path):
    from lab_executor.extension_discovery import (
        resolve_extension_by_id, ExtensionResolveError,
    )
    legacy = tmp_path / "legacy"
    new = tmp_path / "new"
    _make_pack(legacy, "local.dup")
    _make_pack(new, "local.dup")
    paths = _make_paths(legacy, new)
    with pytest.raises(ExtensionResolveError) as excinfo:
        resolve_extension_by_id("local.dup", paths=paths)
    assert excinfo.value.error_class == "duplicate_extension_id"
    assert len(excinfo.value.locations) == 2


# ============================================================
# CLI: extension migration-plan
# ============================================================


def test_cli_migration_plan_help():
    res = subprocess.run(
        [sys.executable, "-m", "lab_executor.cli", "extension",
         "migration-plan", "--help"],
        text=True, capture_output=True, encoding="utf-8",
    )
    assert res.returncode == 0
    assert "--strict" in res.stdout


def test_cli_migration_plan_strict_fails_on_duplicates(
    tmp_path, monkeypatch,
):
    from lab_executor import extension_paths as ep_mod
    from lab_executor import extension_discovery as disc_mod
    legacy = tmp_path / "legacy"
    new = tmp_path / "new"
    _make_pack(legacy, "local.dup")
    _make_pack(new, "local.dup")
    fake = _make_paths(legacy, new)
    monkeypatch.setattr(ep_mod, "get_extension_paths", lambda: fake)
    monkeypatch.setattr(disc_mod, "get_extension_paths", lambda: fake)
    from lab_executor import extension_migration as mig_mod
    monkeypatch.setattr(mig_mod, "get_extension_paths", lambda: fake)

    from lab_executor.cli import main
    import io
    from contextlib import redirect_stdout

    # default (no --strict): duplicate は status=error なので exit 1
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["extension", "migration-plan", "--json"])
    data = json.loads(buf.getvalue())
    assert data["status"] == "error"
    assert data["summary"]["duplicates"] == 1
    assert rc == 1

    # --strict も同じく exit 1
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["extension", "migration-plan", "--json", "--strict"])
    assert rc == 1


def test_cli_migration_plan_warning_strict_behavior(
    tmp_path, monkeypatch,
):
    """legacy_only のみ → status=warning。default exit 0、--strict で 1"""
    from lab_executor import extension_paths as ep_mod
    from lab_executor import extension_discovery as disc_mod
    legacy = tmp_path / "legacy"
    new = tmp_path / "new"
    _make_pack(legacy, "local.only")
    fake = _make_paths(legacy, new)
    monkeypatch.setattr(ep_mod, "get_extension_paths", lambda: fake)
    monkeypatch.setattr(disc_mod, "get_extension_paths", lambda: fake)
    from lab_executor import extension_migration as mig_mod
    monkeypatch.setattr(mig_mod, "get_extension_paths", lambda: fake)

    from lab_executor.cli import main
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["extension", "migration-plan", "--json"])
    data = json.loads(buf.getvalue())
    assert data["status"] == "warning"
    assert rc == 0

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["extension", "migration-plan", "--json", "--strict"])
    assert rc == 1


# ============================================================
# Boundary
# ============================================================


def test_no_pyvisa_visa_mcp_import_for_migration_plan():
    """migration-plan is PyVISA / visa_mcp 非依存"""
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
        "from lab_executor.extension_migration import "
        "plan_extension_migration\n"
        "plan = plan_extension_migration()\n"
        "assert hasattr(plan, 'summary')\n"
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
# Regression
# ============================================================


def test_v25_version():
    import lab_executor
    parts = lab_executor.__version__.split(".")
    assert int(parts[0]) >= 2 and int(parts[1]) >= 5


def test_mcp_tool_surface_unchanged_v25():
    """v2.5: Stable 43 + Experimental 7 = 50 不変"""
    from lab_executor import stability
    stable = [t for ts in stability.STABLE_TOOLS.values() for t in ts]
    exp = [t for ts in stability.EXPERIMENTAL_TOOLS.values() for t in ts]
    assert len(stable) == 43
    assert len(exp) == 7


def test_install_default_unchanged_v25():
    from lab_executor.extension_install import default_extensions_dir
    from lab_executor.extension_paths import get_extension_paths
    assert ".visa-mcp" in str(default_extensions_dir())
    assert get_extension_paths().write_default == default_extensions_dir()
