"""v2.11.0: Cleanup / Rollback Apply Preflight tests (no file changes)."""
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
# cleanup preflight
# ============================================================


def test_cleanup_preflight_ok(tmp_path):
    from lab_executor.extension_migration_log import (
        plan_extension_cleanup_from_log,
        evaluate_cleanup_apply_preconditions,
    )
    mp = _apply(tmp_path)
    plan = plan_extension_cleanup_from_log(mp)
    pf = evaluate_cleanup_apply_preconditions(plan)
    assert pf.operation == "cleanup_apply_preflight"
    assert pf.status == "ok"
    assert pf.eligible is True
    # v2.12: cleanup apply 実装済
    assert pf.apply_supported is True
    assert pf.apply_available is True
    assert pf.candidate_count == 1
    assert pf.blocked_reasons == []
    assert pf.required_confirmation is not None
    assert pf.required_confirmation.startswith("cleanup:1:")


def test_cleanup_preflight_no_candidates_blocked(tmp_path):
    """legacy source が無い → candidate なし → eligible=false"""
    from lab_executor.extension_migration_log import (
        plan_extension_cleanup_from_log,
        evaluate_cleanup_apply_preconditions,
    )
    mp = _apply(tmp_path)
    _sh.rmtree(tmp_path / "legacy" / "local_a")
    plan = plan_extension_cleanup_from_log(mp)
    pf = evaluate_cleanup_apply_preconditions(plan)
    assert pf.eligible is False
    assert pf.status == "error"
    rcs = [b["reason_class"] for b in pf.blocked_reasons]
    assert "no_cleanup_candidates" in rcs
    assert pf.required_confirmation is None


def test_cleanup_preflight_plan_blocked(tmp_path):
    """target が壊れている (extension_id_mismatch) → cleanup plan
    blocked → preflight も blocked"""
    from lab_executor.extension_migration_log import (
        plan_extension_cleanup_from_log,
        evaluate_cleanup_apply_preconditions,
    )
    mp = _apply(tmp_path)
    target_yaml = tmp_path / "new" / "local_a" / "extension.yaml"
    target_yaml.write_text(
        yaml.safe_dump({"extension_id": "local.tampered",
                          "version": "0.1.0"}),
        encoding="utf-8",
    )
    plan = plan_extension_cleanup_from_log(mp)
    pf = evaluate_cleanup_apply_preconditions(plan)
    assert pf.eligible is False
    assert pf.candidate_count == 0


def test_cleanup_preflight_verify_error_blocked(tmp_path):
    """delete_performed_unexpected (manifest 改ざん) → preflight も
    error"""
    from lab_executor.extension_migration_log import (
        plan_extension_cleanup_from_log,
        evaluate_cleanup_apply_preconditions,
    )
    mp = _apply(tmp_path)
    data = json.loads(mp.read_text(encoding="utf-8"))
    data["delete_performed"] = True
    mp.write_text(json.dumps(data), encoding="utf-8")
    plan = plan_extension_cleanup_from_log(mp)
    pf = evaluate_cleanup_apply_preconditions(plan)
    assert pf.eligible is False
    assert pf.status == "error"


def test_cleanup_preflight_confirmation_token_format(tmp_path):
    from lab_executor.extension_migration_log import (
        plan_extension_cleanup_from_log,
        evaluate_cleanup_apply_preconditions,
    )
    mp = _apply(tmp_path)
    plan = plan_extension_cleanup_from_log(mp)
    pf = evaluate_cleanup_apply_preconditions(plan)
    # format: cleanup:<count>:<manifest_stem>
    parts = pf.required_confirmation.split(":")
    assert parts[0] == "cleanup"
    assert parts[1] == "1"
    assert parts[2] == mp.stem


def test_cleanup_preflight_apply_available_v211_legacy(tmp_path):
    """v2.11 では cleanup apply 未実装だったが、v2.12 で実装された
    ため apply_supported=True / apply_available=True (eligible 時)
    に変わった。本テストは v2.12 以降の挙動を確認する."""
    from lab_executor.extension_migration_log import (
        plan_extension_cleanup_from_log,
        evaluate_cleanup_apply_preconditions,
    )
    mp = _apply(tmp_path)
    plan = plan_extension_cleanup_from_log(mp)
    pf = evaluate_cleanup_apply_preconditions(plan)
    assert pf.eligible is True
    # v2.12: cleanup apply 実装済
    assert pf.apply_supported is True
    assert pf.apply_available is True


# ============================================================
# rollback preflight
# ============================================================


def test_rollback_preflight_ok(tmp_path):
    from lab_executor.extension_migration_log import (
        plan_extension_rollback_from_log,
        evaluate_rollback_apply_preconditions,
    )
    mp = _apply(tmp_path)
    plan = plan_extension_rollback_from_log(mp)
    pf = evaluate_rollback_apply_preconditions(plan)
    assert pf.operation == "rollback_apply_preflight"
    assert pf.status == "ok"
    assert pf.eligible is True
    assert pf.apply_supported is False
    assert pf.apply_available is False
    assert pf.required_confirmation.startswith("rollback:1:")


def test_rollback_preflight_no_candidates_blocked(tmp_path):
    """target が無い → already_absent のみで candidate なし"""
    from lab_executor.extension_migration_log import (
        plan_extension_rollback_from_log,
        evaluate_rollback_apply_preconditions,
    )
    mp = _apply(tmp_path)
    _sh.rmtree(tmp_path / "new" / "local_a")
    plan = plan_extension_rollback_from_log(mp)
    pf = evaluate_rollback_apply_preconditions(plan)
    assert pf.eligible is False
    rcs = [b["reason_class"] for b in pf.blocked_reasons]
    assert "no_rollback_candidates" in rcs


def test_rollback_preflight_legacy_missing_blocked(tmp_path):
    """legacy source が無い → plan blocked → preflight も blocked"""
    from lab_executor.extension_migration_log import (
        plan_extension_rollback_from_log,
        evaluate_rollback_apply_preconditions,
    )
    mp = _apply(tmp_path)
    _sh.rmtree(tmp_path / "legacy" / "local_a")
    plan = plan_extension_rollback_from_log(mp)
    pf = evaluate_rollback_apply_preconditions(plan)
    assert pf.eligible is False


def test_rollback_preflight_apply_available_false(tmp_path):
    from lab_executor.extension_migration_log import (
        plan_extension_rollback_from_log,
        evaluate_rollback_apply_preconditions,
    )
    mp = _apply(tmp_path)
    plan = plan_extension_rollback_from_log(mp)
    pf = evaluate_rollback_apply_preconditions(plan)
    assert pf.apply_supported is False
    assert pf.apply_available is False


# ============================================================
# preflight does not change files
# ============================================================


def test_preflight_does_not_change_files(tmp_path):
    from lab_executor.extension_migration_log import (
        plan_extension_rollback_from_log,
        plan_extension_cleanup_from_log,
        evaluate_rollback_apply_preconditions,
        evaluate_cleanup_apply_preconditions,
    )
    mp = _apply(tmp_path)
    legacy_before = _snapshot(tmp_path / "legacy")
    new_before = _snapshot(tmp_path / "new")
    rb_plan = plan_extension_rollback_from_log(mp)
    cu_plan = plan_extension_cleanup_from_log(mp)
    evaluate_rollback_apply_preconditions(rb_plan)
    evaluate_cleanup_apply_preconditions(cu_plan)
    assert _snapshot(tmp_path / "legacy") == legacy_before
    assert _snapshot(tmp_path / "new") == new_before


# ============================================================
# CLI
# ============================================================


def test_cli_cleanup_preflight_json(tmp_path):
    mp = _apply(tmp_path)
    from lab_executor.cli import main
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["extension", "migration-log", "cleanup-plan",
                    str(mp), "--preflight", "--json"])
    data = json.loads(buf.getvalue())
    assert data["operation"] == "cleanup_apply_preflight"
    assert data["status"] == "ok"
    # v2.12: cleanup apply 実装済
    assert data["apply_supported"] is True
    assert data["apply_available"] is True
    assert data["preflight"]["eligible"] is True
    assert data["preflight"]["candidate_count"] == 1
    assert data["preflight"]["required_confirmation"].startswith(
        "cleanup:")
    assert data["schema_version"] == "v2.11"
    assert rc == 0


def test_cli_rollback_preflight_json(tmp_path):
    mp = _apply(tmp_path)
    from lab_executor.cli import main
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["extension", "migration-log", "rollback-plan",
                    str(mp), "--preflight", "--json"])
    data = json.loads(buf.getvalue())
    assert data["operation"] == "rollback_apply_preflight"
    assert data["status"] == "ok"
    assert data["preflight"]["eligible"] is True
    assert data["preflight"]["required_confirmation"].startswith(
        "rollback:")
    assert rc == 0


def test_cli_preflight_latest(tmp_path, monkeypatch):
    from lab_executor import extension_migration_log as mlog
    _apply(tmp_path)
    monkeypatch.setattr(
        mlog, "default_migration_log_dir",
        lambda: tmp_path / "logs",
    )
    from lab_executor.cli import main
    import io
    from contextlib import redirect_stdout
    for cmd in ("rollback-plan", "cleanup-plan"):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(["extension", "migration-log", cmd,
                        "--latest", "--preflight", "--json"])
        data = json.loads(buf.getvalue())
        assert data["preflight"]["eligible"] is True
        assert rc == 0


def test_cli_cleanup_preflight_blocked_returns_1(tmp_path):
    """legacy source 無し → eligible=false → exit 1"""
    mp = _apply(tmp_path)
    _sh.rmtree(tmp_path / "legacy" / "local_a")
    from lab_executor.cli import main
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["extension", "migration-log", "cleanup-plan",
                    str(mp), "--preflight", "--json"])
    data = json.loads(buf.getvalue())
    assert data["preflight"]["eligible"] is False
    assert rc == 1


# ============================================================
# Boundary / regression
# ============================================================


def test_no_pyvisa_visa_mcp_import_for_preflight():
    script = (
        "import sys\n"
        "class B:\n"
        "    def find_spec(self, n, p=None, t=None):\n"
        "        if n == 'lab_visa_mcp' or n.startswith('lab_visa_mcp.'):\n"
        "            raise ImportError('blocked')\n"
        "        if n == 'pyvisa' or n.startswith('pyvisa'):\n"
        "            raise ImportError('blocked')\n"
        "        return None\n"
        "sys.meta_path.insert(0, B())\n"
        "from lab_executor.extension_migration_log import (\n"
        "    evaluate_cleanup_apply_preconditions,\n"
        "    evaluate_rollback_apply_preconditions,\n"
        "    ApplyPreflightResult, ApplyPreconditionCheck,\n"
        ")\n"
        "assert 'pyvisa' not in sys.modules\n"
        "assert 'lab_visa_mcp' not in sys.modules\n"
        "print('OK')\n"
    )
    res = subprocess.run(
        [sys.executable, "-c", script],
        text=True, capture_output=True, encoding="utf-8",
    )
    assert res.returncode == 0, (
        f"stdout={res.stdout}\nstderr={res.stderr[:300]}")
    assert "OK" in res.stdout


def test_v211_version():
    import lab_executor
    parts = lab_executor.__version__.split(".")
    assert int(parts[0]) >= 2 and int(parts[1]) >= 11


def test_mcp_tool_surface_unchanged_v211():
    from lab_executor import stability
    stable = [t for ts in stability.STABLE_TOOLS.values() for t in ts]
    exp = [t for ts in stability.EXPERIMENTAL_TOOLS.values() for t in ts]
    assert len(stable) == 43
    assert len(exp) == 7


def test_install_default_unchanged_v211():
    from lab_executor.extension_install import default_extensions_dir
    from lab_executor.extension_paths import get_extension_paths
    assert ".visa-mcp" in str(default_extensions_dir())
    assert get_extension_paths().write_default == default_extensions_dir()
