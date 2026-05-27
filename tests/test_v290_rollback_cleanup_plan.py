"""v2.9.0: Rollback Plan / Cleanup Plan tests (plan only)."""
from __future__ import annotations
import json
import shutil as _sh
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


def _make_pack(parent: Path, ext_id: str) -> Path:
    parent.mkdir(parents=True, exist_ok=True)
    pack_dir = parent / ext_id.replace(".", "_")
    pack_dir.mkdir()
    (pack_dir / "extension.yaml").write_text(
        yaml.safe_dump({
            "extension_id": ext_id, "version": "0.1.0",
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


def _apply(tmp_path: Path) -> Path:
    from lab_executor.extension_migration import apply_extension_copy_plan
    legacy = tmp_path / "legacy"
    new = tmp_path / "new"
    logs = tmp_path / "logs"
    _make_pack(legacy, "local.a")
    res = apply_extension_copy_plan(
        paths=_make_paths(legacy, new), log_dir=logs,
    )
    assert res.status == "ok"
    return res.manifest_path


def _snapshot(p: Path) -> set[tuple[str, int]]:
    if not p.exists():
        return set()
    out: set[tuple[str, int]] = set()
    for f in p.rglob("*"):
        if f.is_file():
            out.add((str(f.relative_to(p)), f.stat().st_size))
    return out


# ============================================================
# rollback-plan
# ============================================================


def test_rollback_plan_ok(tmp_path):
    """v2.10: plan-only warning だけなら status="ok" (案 A)"""
    from lab_executor.extension_migration_log import (
        plan_extension_rollback_from_log,
    )
    mp = _apply(tmp_path)
    plan = plan_extension_rollback_from_log(mp)
    assert plan.status == "ok"
    assert len(plan.candidates) == 1
    c = plan.candidates[0]
    assert c.extension_id == "local.a"
    assert c.target_exists is True
    assert c.legacy_source_exists is True
    assert c.safe_to_plan is True
    assert c.apply_available is False
    assert plan.apply_available is False
    # plan-only warning は warnings に残る
    wcs = [w["warning_class"] for w in plan.warnings]
    assert "rollback_is_plan_only" in wcs


def test_rollback_plan_target_missing_already_absent(tmp_path):
    """v2.10: target_missing は blocked ではなく already_absent"""
    from lab_executor.extension_migration_log import (
        plan_extension_rollback_from_log,
    )
    mp = _apply(tmp_path)
    _sh.rmtree(tmp_path / "new" / "local_a")
    plan = plan_extension_rollback_from_log(mp)
    assert plan.candidates == []
    assert plan.blocked_reasons == []
    assert len(plan.already_absent) == 1
    assert plan.already_absent[0]["reason"] == "target_missing"
    assert plan.status == "ok"  # already_absent は問題ではない


def test_rollback_plan_legacy_source_missing_blocked(tmp_path):
    """legacy source が無いなら rollback してはいけない"""
    from lab_executor.extension_migration_log import (
        plan_extension_rollback_from_log,
    )
    mp = _apply(tmp_path)
    _sh.rmtree(tmp_path / "legacy" / "local_a")
    plan = plan_extension_rollback_from_log(mp)
    assert plan.candidates == []
    rcs = [b["reason_class"] for b in plan.blocked_reasons]
    assert "legacy_source_missing" in rcs


def test_rollback_plan_schema_unsupported(tmp_path):
    from lab_executor.extension_migration_log import (
        plan_extension_rollback_from_log,
    )
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({
        "schema_version": "v999",
        "operation": "extension_copy_apply",
    }), encoding="utf-8")
    plan = plan_extension_rollback_from_log(bad)
    assert plan.status == "error"
    rcs = [b["reason_class"] for b in plan.blocked_reasons]
    assert "manifest_schema_unsupported" in rcs


def test_rollback_plan_delete_performed_unexpected(tmp_path):
    from lab_executor.extension_migration_log import (
        plan_extension_rollback_from_log,
    )
    mp = _apply(tmp_path)
    data = json.loads(mp.read_text(encoding="utf-8"))
    data["delete_performed"] = True
    mp.write_text(json.dumps(data), encoding="utf-8")
    plan = plan_extension_rollback_from_log(mp)
    assert plan.status == "error"
    rcs = [b["reason_class"] for b in plan.blocked_reasons]
    assert "delete_performed_unexpected" in rcs


def test_rollback_plan_does_not_delete_files(tmp_path):
    """rollback-plan は実ファイルを変更しない"""
    from lab_executor.extension_migration_log import (
        plan_extension_rollback_from_log,
    )
    mp = _apply(tmp_path)
    legacy_before = _snapshot(tmp_path / "legacy")
    new_before = _snapshot(tmp_path / "new")
    plan_extension_rollback_from_log(mp)
    assert _snapshot(tmp_path / "legacy") == legacy_before
    assert _snapshot(tmp_path / "new") == new_before


# ============================================================
# cleanup-plan
# ============================================================


def test_cleanup_plan_ok(tmp_path):
    """v2.10: plan-only warning は status を warning にしない"""
    from lab_executor.extension_migration_log import (
        plan_extension_cleanup_from_log,
    )
    mp = _apply(tmp_path)
    plan = plan_extension_cleanup_from_log(mp)
    assert plan.status == "ok"
    assert len(plan.candidates) == 1
    c = plan.candidates[0]
    assert c.extension_id == "local.a"
    assert c.target_verified is True
    assert c.legacy_source_exists is True
    assert c.apply_available is False
    wcs = [w["warning_class"] for w in plan.warnings]
    assert "cleanup_is_plan_only" in wcs


def test_cleanup_plan_target_missing_blocked(tmp_path):
    from lab_executor.extension_migration_log import (
        plan_extension_cleanup_from_log,
    )
    mp = _apply(tmp_path)
    _sh.rmtree(tmp_path / "new" / "local_a")
    plan = plan_extension_cleanup_from_log(mp)
    assert plan.candidates == []
    rcs = [b["reason_class"] for b in plan.blocked_reasons]
    assert "target_missing" in rcs


def test_cleanup_plan_extension_id_mismatch_blocked(tmp_path):
    from lab_executor.extension_migration_log import (
        plan_extension_cleanup_from_log,
    )
    mp = _apply(tmp_path)
    target_yaml = tmp_path / "new" / "local_a" / "extension.yaml"
    target_yaml.write_text(
        yaml.safe_dump({"extension_id": "local.tampered",
                          "version": "0.1.0"}),
        encoding="utf-8",
    )
    plan = plan_extension_cleanup_from_log(mp)
    assert plan.candidates == []
    rcs = [b["reason_class"] for b in plan.blocked_reasons]
    assert "extension_id_mismatch" in rcs


def test_cleanup_plan_source_missing_legacy_source_missing(tmp_path):
    """v2.10: source が既に無い場合は legacy_source_missing リストへ"""
    from lab_executor.extension_migration_log import (
        plan_extension_cleanup_from_log,
    )
    mp = _apply(tmp_path)
    _sh.rmtree(tmp_path / "legacy" / "local_a")
    plan = plan_extension_cleanup_from_log(mp)
    assert plan.candidates == []
    assert len(plan.legacy_source_missing) == 1
    assert plan.legacy_source_missing[0]["extension_id"] == "local.a"
    # status は ok (legacy_source_missing は problem ではない)
    assert plan.status == "ok"


def test_cleanup_plan_does_not_delete_files(tmp_path):
    from lab_executor.extension_migration_log import (
        plan_extension_cleanup_from_log,
    )
    mp = _apply(tmp_path)
    legacy_before = _snapshot(tmp_path / "legacy")
    new_before = _snapshot(tmp_path / "new")
    plan_extension_cleanup_from_log(mp)
    assert _snapshot(tmp_path / "legacy") == legacy_before
    assert _snapshot(tmp_path / "new") == new_before


def test_cleanup_plan_delete_performed_unexpected(tmp_path):
    from lab_executor.extension_migration_log import (
        plan_extension_cleanup_from_log,
    )
    mp = _apply(tmp_path)
    data = json.loads(mp.read_text(encoding="utf-8"))
    data["overwrite_performed"] = True
    mp.write_text(json.dumps(data), encoding="utf-8")
    plan = plan_extension_cleanup_from_log(mp)
    assert plan.status == "error"
    rcs = [b["reason_class"] for b in plan.blocked_reasons]
    assert "overwrite_performed_unexpected" in rcs


# ============================================================
# CLI
# ============================================================


def test_cli_rollback_plan_help():
    res = subprocess.run(
        [sys.executable, "-m", "lab_executor.cli", "extension",
         "migration-log", "rollback-plan", "--help"],
        text=True, capture_output=True, encoding="utf-8",
    )
    assert res.returncode == 0
    assert "manifest" in res.stdout


def test_cli_cleanup_plan_help():
    res = subprocess.run(
        [sys.executable, "-m", "lab_executor.cli", "extension",
         "migration-log", "cleanup-plan", "--help"],
        text=True, capture_output=True, encoding="utf-8",
    )
    assert res.returncode == 0


def test_cli_rollback_plan_json(tmp_path):
    mp = _apply(tmp_path)
    from lab_executor.cli import main
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["extension", "migration-log", "rollback-plan",
                    str(mp), "--json"])
    data = json.loads(buf.getvalue())
    assert data["operation"] == "extension_copy_rollback_plan"
    assert data["schema_version"] == "v2.10"
    assert data["apply_available"] is False
    assert data["summary"]["rollback_candidates"] == 1
    # v2.10: status=ok なので rc=0
    assert data["status"] == "ok"
    assert rc == 0


def test_cli_cleanup_plan_json(tmp_path):
    mp = _apply(tmp_path)
    from lab_executor.cli import main
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["extension", "migration-log", "cleanup-plan",
                    str(mp), "--json"])
    data = json.loads(buf.getvalue())
    assert data["operation"] == "extension_cleanup_plan"
    assert data["schema_version"] == "v2.10"
    assert data["apply_available"] is False
    assert data["summary"]["cleanup_candidates"] == 1
    assert data["status"] == "ok"
    assert rc == 0


def test_cli_rollback_plan_strict_passes_on_plan_only(tmp_path):
    """v2.10: plan-only warning だけなら --strict でも exit 0"""
    mp = _apply(tmp_path)
    from lab_executor.cli import main
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["extension", "migration-log", "rollback-plan",
                    str(mp), "--json", "--strict"])
    assert rc == 0


def test_cli_cleanup_plan_strict_passes_on_plan_only(tmp_path):
    """v2.10: plan-only warning だけなら --strict でも exit 0"""
    mp = _apply(tmp_path)
    from lab_executor.cli import main
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["extension", "migration-log", "cleanup-plan",
                    str(mp), "--json", "--strict"])
    assert rc == 0


# ============================================================
# Boundary / regression
# ============================================================


def test_no_pyvisa_visa_mcp_import_for_rollback_cleanup_plan():
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
        "from lab_executor.extension_migration_log import (\n"
        "    plan_extension_rollback_from_log,\n"
        "    plan_extension_cleanup_from_log,\n"
        "    ExtensionRollbackPlan, ExtensionCleanupPlan,\n"
        ")\n"
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


def test_v29_version():
    import lab_executor
    parts = lab_executor.__version__.split(".")
    assert int(parts[0]) >= 2 and int(parts[1]) >= 9


def test_mcp_tool_surface_unchanged_v29():
    from lab_executor import stability
    stable = [t for ts in stability.STABLE_TOOLS.values() for t in ts]
    exp = [t for ts in stability.EXPERIMENTAL_TOOLS.values() for t in ts]
    assert len(stable) == 43
    assert len(exp) == 7


def test_install_default_unchanged_v29():
    from lab_executor.extension_install import default_extensions_dir
    from lab_executor.extension_paths import get_extension_paths
    assert ".visa-mcp" in str(default_extensions_dir())
    assert get_extension_paths().write_default == default_extensions_dir()
