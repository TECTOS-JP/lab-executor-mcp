"""v2.12.0: Controlled Cleanup Apply (legacy source → trash move)."""
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


def _apply_copy(tmp_path: Path) -> Path:
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


def _expected_token(mp: Path) -> str:
    """preflight が生成する confirmation token を取得"""
    from lab_executor.extension_migration_log import (
        plan_extension_cleanup_from_log,
        evaluate_cleanup_apply_preconditions,
    )
    plan = plan_extension_cleanup_from_log(mp)
    pf = evaluate_cleanup_apply_preconditions(plan)
    return pf.required_confirmation


# ============================================================
# apply preconditions
# ============================================================


def test_cleanup_apply_requires_confirm(tmp_path):
    from lab_executor.extension_migration_log import (
        apply_extension_cleanup_plan,
    )
    mp = _apply_copy(tmp_path)
    logs = tmp_path / "logs"
    trash = tmp_path / "trash"
    res = apply_extension_cleanup_plan(
        mp, confirm=None, log_dir=logs, trash_root_base=trash,
    )
    assert res.status == "blocked"
    rcs = [b["reason_class"] for b in res.blocked_reasons]
    assert "confirmation_required" in rcs


def test_cleanup_apply_rejects_wrong_confirm(tmp_path):
    from lab_executor.extension_migration_log import (
        apply_extension_cleanup_plan,
    )
    mp = _apply_copy(tmp_path)
    res = apply_extension_cleanup_plan(
        mp, confirm="cleanup:1:wrong-stem",
        log_dir=tmp_path / "logs",
        trash_root_base=tmp_path / "trash",
    )
    assert res.status == "blocked"
    rcs = [b["reason_class"] for b in res.blocked_reasons]
    assert "confirmation_mismatch" in rcs


def test_cleanup_apply_moves_legacy_source_to_trash(tmp_path):
    """正常系: legacy source が trash へ移動、target は残る"""
    from lab_executor.extension_migration_log import (
        apply_extension_cleanup_plan,
    )
    mp = _apply_copy(tmp_path)
    token = _expected_token(mp)
    logs = tmp_path / "logs"
    trash = tmp_path / "trash"
    res = apply_extension_cleanup_plan(
        mp, confirm=token, log_dir=logs, trash_root_base=trash,
    )
    assert res.status == "ok"
    assert len(res.moved_to_trash) == 1
    # source が legacy から trash へ移動
    assert not (tmp_path / "legacy" / "local_a").exists()
    trash_target = trash / mp.stem / "local_a"
    assert trash_target.exists()
    assert (trash_target / "extension.yaml").exists()
    # target (new path) は残る
    assert (tmp_path / "new" / "local_a" / "extension.yaml").exists()


def test_cleanup_apply_does_not_permanently_delete(tmp_path):
    from lab_executor.extension_migration_log import (
        apply_extension_cleanup_plan,
    )
    mp = _apply_copy(tmp_path)
    token = _expected_token(mp)
    res = apply_extension_cleanup_plan(
        mp, confirm=token,
        log_dir=tmp_path / "logs",
        trash_root_base=tmp_path / "trash",
    )
    assert res.status == "ok"
    assert res.permanent_delete_performed is False
    assert res.overwrite_performed is False
    assert res.trash_move_performed is True


def test_cleanup_apply_blocks_when_trash_target_exists(tmp_path):
    """trash target が既に存在すれば apply 不可 (skipped + fail-fast)"""
    from lab_executor.extension_migration_log import (
        apply_extension_cleanup_plan,
    )
    mp = _apply_copy(tmp_path)
    token = _expected_token(mp)
    trash = tmp_path / "trash"
    # 先に trash target を作っておく
    pre = trash / mp.stem / "local_a"
    pre.mkdir(parents=True)
    (pre / "marker.txt").write_text("preexisting", encoding="utf-8")
    res = apply_extension_cleanup_plan(
        mp, confirm=token, log_dir=tmp_path / "logs",
        trash_root_base=trash,
    )
    # candidate は skipped、moved_to_trash は空
    assert res.moved_to_trash == []
    assert res.status == "partial_failure"
    rcs = [s["reason_class"] for s in res.skipped]
    assert "trash_target_exists" in rcs
    # legacy source は触られていない
    assert (tmp_path / "legacy" / "local_a").exists()
    # 既存 trash target も触られていない
    assert (pre / "marker.txt").read_text(encoding="utf-8") == \
        "preexisting"


def test_cleanup_apply_blocks_when_preflight_ineligible(tmp_path):
    """legacy source が無くて plan で candidate=0 → preflight
    ineligible → apply blocked"""
    from lab_executor.extension_migration_log import (
        apply_extension_cleanup_plan,
    )
    mp = _apply_copy(tmp_path)
    # apply 直前に source を消す (再計算で candidate=0)
    _sh.rmtree(tmp_path / "legacy" / "local_a")
    res = apply_extension_cleanup_plan(
        mp, confirm="cleanup:1:dummy",
        log_dir=tmp_path / "logs",
        trash_root_base=tmp_path / "trash",
    )
    assert res.status == "blocked"
    rcs = [b["reason_class"] for b in res.blocked_reasons]
    assert "preflight_not_eligible" in rcs


def test_cleanup_apply_recomputes_preflight(tmp_path):
    """apply 内で plan + preflight を再計算する contract。
    最初の preflight 後に source を消してから apply すると blocked."""
    from lab_executor.extension_migration_log import (
        apply_extension_cleanup_plan,
        plan_extension_cleanup_from_log,
        evaluate_cleanup_apply_preconditions,
    )
    mp = _apply_copy(tmp_path)
    # 1回目: preflight ok を取得
    pf = evaluate_cleanup_apply_preconditions(
        plan_extension_cleanup_from_log(mp))
    assert pf.eligible is True
    token = pf.required_confirmation
    # apply 前に source を消す → 再計算で eligible=false に
    _sh.rmtree(tmp_path / "legacy" / "local_a")
    res = apply_extension_cleanup_plan(
        mp, confirm=token,
        log_dir=tmp_path / "logs",
        trash_root_base=tmp_path / "trash",
    )
    assert res.status == "blocked"


def test_cleanup_apply_manifest_written(tmp_path):
    from lab_executor.extension_migration_log import (
        apply_extension_cleanup_plan,
        load_extension_migration_log,
    )
    mp = _apply_copy(tmp_path)
    token = _expected_token(mp)
    res = apply_extension_cleanup_plan(
        mp, confirm=token,
        log_dir=tmp_path / "logs",
        trash_root_base=tmp_path / "trash",
    )
    assert res.manifest_path is not None
    assert res.manifest_path.exists()
    # cleanup manifest を load できる (schema v2.12)
    m = load_extension_migration_log(res.manifest_path)
    assert m.schema_version == "v2.12"
    assert m.operation == "extension_cleanup_apply"


def test_cleanup_apply_manifest_blocked_also_saved(tmp_path):
    """blocked 時も manifest を残す (audit)"""
    from lab_executor.extension_migration_log import (
        apply_extension_cleanup_plan,
    )
    mp = _apply_copy(tmp_path)
    res = apply_extension_cleanup_plan(
        mp, confirm=None,
        log_dir=tmp_path / "logs",
        trash_root_base=tmp_path / "trash",
    )
    assert res.status == "blocked"
    assert res.manifest_path is not None
    assert res.manifest_path.exists()


# ============================================================
# preflight reports apply_supported=True for cleanup
# ============================================================


def test_cleanup_preflight_apply_supported_true_v212(tmp_path):
    from lab_executor.extension_migration_log import (
        plan_extension_cleanup_from_log,
        evaluate_cleanup_apply_preconditions,
    )
    mp = _apply_copy(tmp_path)
    pf = evaluate_cleanup_apply_preconditions(
        plan_extension_cleanup_from_log(mp))
    # v2.12 では cleanup apply 実装済 → apply_supported=True
    assert pf.apply_supported is True
    assert pf.apply_available is True  # eligible なので


def test_rollback_preflight_apply_supported_false_v212(tmp_path):
    from lab_executor.extension_migration_log import (
        plan_extension_rollback_from_log,
        evaluate_rollback_apply_preconditions,
    )
    mp = _apply_copy(tmp_path)
    pf = evaluate_rollback_apply_preconditions(
        plan_extension_rollback_from_log(mp))
    # rollback apply は v2.12 でも未実装
    assert pf.apply_supported is False
    assert pf.apply_available is False


# ============================================================
# CLI
# ============================================================


def test_cli_cleanup_apply_requires_confirm(tmp_path):
    mp = _apply_copy(tmp_path)
    from lab_executor.cli import main
    import io
    from contextlib import redirect_stderr
    buf = io.StringIO()
    with redirect_stderr(buf):
        rc = main(["extension", "migration-log", "cleanup-plan",
                    str(mp), "--apply"])
    assert rc == 2
    assert "confirm" in buf.getvalue().lower()


def test_cli_rollback_apply_not_implemented(tmp_path):
    mp = _apply_copy(tmp_path)
    from lab_executor.cli import main
    import io
    from contextlib import redirect_stderr
    buf = io.StringIO()
    with redirect_stderr(buf):
        rc = main(["extension", "migration-log", "rollback-plan",
                    str(mp), "--apply", "--confirm",
                    "rollback:1:foo"])
    assert rc == 2
    assert "not implemented" in buf.getvalue().lower()


def test_cli_cleanup_apply_ok(tmp_path, monkeypatch):
    from lab_executor import extension_migration_log as mlog
    mp = _apply_copy(tmp_path)
    token = _expected_token(mp)
    monkeypatch.setattr(
        mlog, "default_migration_log_dir",
        lambda: tmp_path / "logs",
    )
    # trash_root_base は CLI から指定できないので home の下を使うが、
    # テストでは Path.home() を tmp に向ける
    monkeypatch.setattr(
        Path, "home", classmethod(lambda cls: tmp_path / "fake_home"),
    )
    from lab_executor.cli import main
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["extension", "migration-log", "cleanup-plan",
                    str(mp), "--apply", "--confirm", token, "--json"])
    data = json.loads(buf.getvalue())
    assert data["status"] == "ok"
    assert data["operation"] == "extension_cleanup_apply"
    assert data["permanent_delete_performed"] is False
    assert data["overwrite_performed"] is False
    assert data["trash_move_performed"] is True
    assert data["schema_version"] == "v2.12"
    assert rc == 0


# ============================================================
# Boundary / regression
# ============================================================


def test_no_pyvisa_visa_mcp_import_for_cleanup_apply():
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
        "    apply_extension_cleanup_plan,\n"
        "    ExtensionCleanupApplyResult,\n"
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


def test_v212_version():
    import lab_executor
    parts = lab_executor.__version__.split(".")
    assert int(parts[0]) >= 2 and int(parts[1]) >= 12


def test_mcp_tool_surface_unchanged_v212():
    from lab_executor import stability
    stable = [t for ts in stability.STABLE_TOOLS.values() for t in ts]
    exp = [t for ts in stability.EXPERIMENTAL_TOOLS.values() for t in ts]
    assert len(stable) == 43
    assert len(exp) == 7


def test_install_default_unchanged_v212():
    from lab_executor.extension_install import default_extensions_dir
    from lab_executor.extension_paths import get_extension_paths
    assert ".visa-mcp" in str(default_extensions_dir())
    assert get_extension_paths().write_default == default_extensions_dir()
