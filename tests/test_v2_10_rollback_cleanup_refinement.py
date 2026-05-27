"""v2.10.0: Rollback / Cleanup Plan Refinement + --latest support."""
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
# find_latest + --latest CLI
# ============================================================


def test_find_latest_extension_copy_manifest(tmp_path):
    import time
    from lab_executor.extension_migration import apply_extension_copy_plan
    from lab_executor.extension_migration_log import (
        find_latest_extension_copy_manifest,
    )
    legacy = tmp_path / "legacy"
    new = tmp_path / "new"
    logs = tmp_path / "logs"
    # 最初の apply
    _make_pack(legacy, "local.a")
    r1 = apply_extension_copy_plan(
        paths=_make_paths(legacy, new), log_dir=logs,
    )
    time.sleep(1.1)  # timestamp 差を確実に
    _make_pack(legacy, "local.b")
    r2 = apply_extension_copy_plan(
        paths=_make_paths(legacy, new), log_dir=logs,
    )
    latest = find_latest_extension_copy_manifest(log_dir=logs)
    assert latest == r2.manifest_path
    assert latest != r1.manifest_path


def test_find_latest_returns_none_when_empty(tmp_path):
    from lab_executor.extension_migration_log import (
        find_latest_extension_copy_manifest,
    )
    assert find_latest_extension_copy_manifest(
        log_dir=tmp_path / "empty") is None


def test_cli_latest_and_explicit_manifest_conflict(tmp_path, monkeypatch):
    """--latest と manifest path 同時指定は usage error (exit 2)"""
    mp = _apply(tmp_path)
    from lab_executor.cli import main
    import io
    from contextlib import redirect_stderr
    buf = io.StringIO()
    with redirect_stderr(buf):
        rc = main(["extension", "migration-log", "verify",
                    str(mp), "--latest", "--json"])
    assert rc == 2
    assert "--latest" in buf.getvalue()


def test_cli_latest_no_manifest_found(tmp_path, monkeypatch):
    from lab_executor import extension_migration_log as mlog
    monkeypatch.setattr(
        mlog, "default_migration_log_dir",
        lambda: tmp_path / "empty",
    )
    from lab_executor.cli import main
    import io
    from contextlib import redirect_stderr
    buf = io.StringIO()
    with redirect_stderr(buf):
        rc = main(["extension", "migration-log", "verify",
                    "--latest", "--json"])
    assert rc == 1


def test_cli_verify_latest(tmp_path, monkeypatch):
    from lab_executor import extension_migration_log as mlog
    _apply(tmp_path)
    monkeypatch.setattr(
        mlog, "default_migration_log_dir",
        lambda: tmp_path / "logs",
    )
    from lab_executor.cli import main
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["extension", "migration-log", "verify",
                    "--latest", "--json"])
    data = json.loads(buf.getvalue())
    assert data["status"] == "ok"
    assert rc == 0


def test_cli_inspect_latest(tmp_path, monkeypatch):
    from lab_executor import extension_migration_log as mlog
    _apply(tmp_path)
    monkeypatch.setattr(
        mlog, "default_migration_log_dir",
        lambda: tmp_path / "logs",
    )
    from lab_executor.cli import main
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["extension", "migration-log", "inspect",
                    "--latest", "--json"])
    data = json.loads(buf.getvalue())
    assert data["schema_version"] == "v2.7"
    assert rc == 0


def test_cli_rollback_plan_latest(tmp_path, monkeypatch):
    from lab_executor import extension_migration_log as mlog
    _apply(tmp_path)
    monkeypatch.setattr(
        mlog, "default_migration_log_dir",
        lambda: tmp_path / "logs",
    )
    from lab_executor.cli import main
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["extension", "migration-log", "rollback-plan",
                    "--latest", "--json"])
    data = json.loads(buf.getvalue())
    assert data["summary"]["rollback_candidates"] == 1
    assert rc == 0


def test_cli_cleanup_plan_latest(tmp_path, monkeypatch):
    from lab_executor import extension_migration_log as mlog
    _apply(tmp_path)
    monkeypatch.setattr(
        mlog, "default_migration_log_dir",
        lambda: tmp_path / "logs",
    )
    from lab_executor.cli import main
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["extension", "migration-log", "cleanup-plan",
                    "--latest", "--json"])
    data = json.loads(buf.getvalue())
    assert data["summary"]["cleanup_candidates"] == 1
    assert rc == 0


# ============================================================
# cleanup-plan uses verify_extension_migration_log()
# ============================================================


def test_cleanup_plan_uses_verify_result(tmp_path, monkeypatch):
    """v2.10: cleanup-plan は内部で verify_extension_migration_log を
    呼び出す。verify の error → blocked、verify ok → candidate へ
    変換されることを確認 (整合性 contract)."""
    from lab_executor.extension_migration_log import (
        plan_extension_cleanup_from_log,
    )
    from lab_executor import extension_migration_log as mlog
    mp = _apply(tmp_path)
    # target に extension_id 不一致を仕込む
    target_yaml = tmp_path / "new" / "local_a" / "extension.yaml"
    target_yaml.write_text(
        yaml.safe_dump({"extension_id": "local.tampered",
                          "version": "0.1.0"}),
        encoding="utf-8",
    )
    plan = plan_extension_cleanup_from_log(mp)
    rcs = [b["reason_class"] for b in plan.blocked_reasons]
    assert "extension_id_mismatch" in rcs
    assert plan.candidates == []


def test_cleanup_plan_verify_meta_error_blocks_whole_plan(tmp_path):
    """verify の overall meta error (delete_performed_unexpected) は
    cleanup-plan を全体 block する"""
    from lab_executor.extension_migration_log import (
        plan_extension_cleanup_from_log,
    )
    mp = _apply(tmp_path)
    data = json.loads(mp.read_text(encoding="utf-8"))
    data["delete_performed"] = True
    mp.write_text(json.dumps(data), encoding="utf-8")
    plan = plan_extension_cleanup_from_log(mp)
    assert plan.status == "error"
    rcs = [b["reason_class"] for b in plan.blocked_reasons]
    assert "delete_performed_unexpected" in rcs


# ============================================================
# plan-only warning -> status=ok (案 A)
# ============================================================


def test_plan_only_warning_does_not_force_warning_status(tmp_path):
    """v2.10 案 A: plan-only warning だけなら status=ok"""
    from lab_executor.extension_migration_log import (
        plan_extension_rollback_from_log,
        plan_extension_cleanup_from_log,
    )
    mp = _apply(tmp_path)
    rb = plan_extension_rollback_from_log(mp)
    cu = plan_extension_cleanup_from_log(mp)
    assert rb.status == "ok"
    assert cu.status == "ok"
    # warnings には plan-only が残る
    assert any(w["warning_class"] == "rollback_is_plan_only"
                for w in rb.warnings)
    assert any(w["warning_class"] == "cleanup_is_plan_only"
                for w in cu.warnings)


def test_strict_does_not_fail_on_plan_only_warning(tmp_path):
    mp = _apply(tmp_path)
    from lab_executor.cli import main
    import io
    from contextlib import redirect_stdout
    for cmd in ("rollback-plan", "cleanup-plan"):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(["extension", "migration-log", cmd,
                        str(mp), "--json", "--strict"])
        assert rc == 0, f"{cmd} --strict should exit 0 on plan-only"


# ============================================================
# rollback already_absent classification
# ============================================================


def test_rollback_plan_already_absent_partition(tmp_path):
    """target_missing は already_absent へ、legacy_source_missing は
    blocked_reasons へ、ちゃんと分かれて入ること"""
    from lab_executor.extension_migration import apply_extension_copy_plan
    from lab_executor.extension_migration_log import (
        plan_extension_rollback_from_log,
    )
    legacy = tmp_path / "legacy"
    new = tmp_path / "new"
    logs = tmp_path / "logs"
    _make_pack(legacy, "local.a")
    _make_pack(legacy, "local.b")
    apply_extension_copy_plan(
        paths=_make_paths(legacy, new), log_dir=logs,
    )
    # local.a の target を消す → already_absent
    _sh.rmtree(new / "local_a")
    # local.b の legacy source を消す → blocked
    _sh.rmtree(legacy / "local_b")
    # latest manifest
    from lab_executor.extension_migration_log import (
        find_latest_extension_copy_manifest,
    )
    mp = find_latest_extension_copy_manifest(log_dir=logs)
    plan = plan_extension_rollback_from_log(mp)
    assert len(plan.already_absent) == 1
    assert plan.already_absent[0]["extension_id"] == "local.a"
    blocked_ids = [b.get("extension_id") for b in plan.blocked_reasons]
    assert "local.b" in blocked_ids


# ============================================================
# no file changes
# ============================================================


def test_rollback_cleanup_no_file_changes(tmp_path):
    from lab_executor.extension_migration_log import (
        plan_extension_rollback_from_log,
        plan_extension_cleanup_from_log,
    )
    mp = _apply(tmp_path)
    legacy_before = _snapshot(tmp_path / "legacy")
    new_before = _snapshot(tmp_path / "new")
    plan_extension_rollback_from_log(mp)
    plan_extension_cleanup_from_log(mp)
    assert _snapshot(tmp_path / "legacy") == legacy_before
    assert _snapshot(tmp_path / "new") == new_before


# ============================================================
# Boundary / regression
# ============================================================


def test_no_pyvisa_visa_mcp_import_for_v2_10():
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
        "    find_latest_extension_copy_manifest,\n"
        "    plan_extension_rollback_from_log,\n"
        "    plan_extension_cleanup_from_log,\n"
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


def test_v210_version():
    import lab_executor
    parts = lab_executor.__version__.split(".")
    assert int(parts[0]) >= 2 and int(parts[1]) >= 10


def test_mcp_tool_surface_unchanged_v210():
    from lab_executor import stability
    stable = [t for ts in stability.STABLE_TOOLS.values() for t in ts]
    exp = [t for ts in stability.EXPERIMENTAL_TOOLS.values() for t in ts]
    assert len(stable) == 43
    assert len(exp) == 7


def test_install_default_unchanged_v210():
    from lab_executor.extension_install import default_extensions_dir
    from lab_executor.extension_paths import get_extension_paths
    assert ".visa-mcp" in str(default_extensions_dir())
    assert get_extension_paths().write_default == default_extensions_dir()
