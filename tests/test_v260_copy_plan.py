"""v2.6.0: Extension Migration Copy Plan tests."""
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


def _snapshot(p: Path) -> set[tuple[str, int]]:
    """Recursive (rel_path, size) snapshot for non-modification checks."""
    if not p.exists():
        return set()
    out: set[tuple[str, int]] = set()
    for f in p.rglob("*"):
        if f.is_file():
            out.add((str(f.relative_to(p)), f.stat().st_size))
    return out


# ============================================================
# plan_extension_migration(copy_plan=True)
# ============================================================


def test_copy_plan_legacy_only_candidates(tmp_path):
    from lab_executor.extension_migration import plan_extension_migration
    legacy = tmp_path / "legacy"
    new = tmp_path / "new"
    _make_pack(legacy, "local.a")
    _make_pack(legacy, "local.b")
    plan = plan_extension_migration(
        paths=_make_paths(legacy, new), copy_plan=True,
    )
    cp = plan.copy_plan
    assert cp is not None
    assert cp.status == "ready"
    assert cp.apply_available is False
    ids = {c.extension_id for c in cp.candidates}
    assert ids == {"local.a", "local.b"}
    for c in cp.candidates:
        assert c.source.parent == legacy
        assert c.target.parent == new
        assert c.safe_to_copy is True
        assert c.overwrite_required is False
    assert plan.summary["copy_candidates"] == 2
    assert plan.summary["copy_blocked"] is False


def test_copy_plan_new_only_no_candidates(tmp_path):
    from lab_executor.extension_migration import plan_extension_migration
    legacy = tmp_path / "legacy"
    new = tmp_path / "new"
    _make_pack(new, "local.x")
    plan = plan_extension_migration(
        paths=_make_paths(legacy, new), copy_plan=True,
    )
    cp = plan.copy_plan
    assert cp is not None
    assert cp.status == "empty"
    assert cp.candidates == []
    assert plan.summary["copy_candidates"] == 0
    assert plan.summary["copy_blocked"] is False


def test_copy_plan_duplicate_blocked(tmp_path):
    from lab_executor.extension_migration import plan_extension_migration
    legacy = tmp_path / "legacy"
    new = tmp_path / "new"
    _make_pack(legacy, "local.dup")
    _make_pack(new, "local.dup")
    plan = plan_extension_migration(
        paths=_make_paths(legacy, new), copy_plan=True,
    )
    cp = plan.copy_plan
    assert cp is not None
    assert cp.status == "blocked"
    assert cp.candidates == []
    rcs = [r["reason_class"] for r in cp.blocked_reasons]
    assert "duplicate_extension_id" in rcs
    assert plan.summary["copy_blocked"] is True
    assert plan.summary["copy_candidates"] == 0


def test_copy_plan_invalid_metadata_blocked(tmp_path):
    from lab_executor.extension_migration import plan_extension_migration
    legacy = tmp_path / "legacy"
    new = tmp_path / "new"
    legacy.mkdir(parents=True)
    p = legacy / "broken"
    p.mkdir()
    (p / "extension.yaml").write_text(
        yaml.safe_dump({"version": "0.1"}), encoding="utf-8",
    )
    plan = plan_extension_migration(
        paths=_make_paths(legacy, new), copy_plan=True,
    )
    cp = plan.copy_plan
    assert cp is not None
    assert cp.status == "blocked"
    rcs = [r["reason_class"] for r in cp.blocked_reasons]
    assert "invalid_extension_metadata" in rcs


def test_copy_plan_target_exists_skipped_or_blocked(tmp_path):
    """legacy has pack, new has same-named directory → target_exists"""
    from lab_executor.extension_migration import plan_extension_migration
    legacy = tmp_path / "legacy"
    new = tmp_path / "new"
    _make_pack(legacy, "local.same")
    # new に異なる extension_id の pack を作って同じ dir 名を占拠する
    # (legacy では "local_same" という dir 名になる)
    new.mkdir(parents=True)
    (new / "local_same").mkdir()  # target dir 名と同じ
    (new / "local_same" / "marker.txt").write_text("x", encoding="utf-8")
    # ↑ extension.yaml 無し なので discovery 上は missing_extension_yaml
    plan = plan_extension_migration(
        paths=_make_paths(legacy, new), copy_plan=True,
    )
    cp = plan.copy_plan
    assert cp is not None
    # candidates は 0 (target_exists でブロック)、blocked_reasons に
    # target_exists が含まれる
    assert cp.candidates == []
    rcs = [r["reason_class"] for r in cp.blocked_reasons]
    assert "target_exists" in rcs


def test_copy_plan_no_file_changes(tmp_path):
    """v2.6.0 の核: copy-plan は実ファイルを一切変更しない"""
    from lab_executor.extension_migration import plan_extension_migration
    legacy = tmp_path / "legacy"
    new = tmp_path / "new"
    _make_pack(legacy, "local.a")
    _make_pack(legacy, "local.b")
    legacy_before = _snapshot(legacy)
    new_before_exists = new.exists()

    plan_extension_migration(
        paths=_make_paths(legacy, new), copy_plan=True,
    )

    legacy_after = _snapshot(legacy)
    assert legacy_before == legacy_after, "legacy 側が変更された"
    # new path を勝手に作成しないこと
    assert new.exists() == new_before_exists, "new path が新規作成された"


def test_copy_plan_apply_available_false(tmp_path):
    from lab_executor.extension_migration import plan_extension_migration
    legacy = tmp_path / "legacy"
    new = tmp_path / "new"
    _make_pack(legacy, "local.a")
    plan = plan_extension_migration(
        paths=_make_paths(legacy, new), copy_plan=True,
    )
    assert plan.copy_plan.apply_available is False
    for c in plan.copy_plan.candidates:
        assert c.overwrite_required is False


def test_copy_plan_omitted_when_flag_false(tmp_path):
    """copy_plan=False (default) なら copy_plan は None"""
    from lab_executor.extension_migration import plan_extension_migration
    plan = plan_extension_migration(
        paths=_make_paths(tmp_path / "L", tmp_path / "N"),
        copy_plan=False,
    )
    assert plan.copy_plan is None


# ============================================================
# CLI: extension migration-plan --copy-plan
# ============================================================


def test_cli_migration_plan_copy_plan_help():
    res = subprocess.run(
        [sys.executable, "-m", "lab_executor.cli", "extension",
         "migration-plan", "--help"],
        text=True, capture_output=True, encoding="utf-8",
    )
    assert res.returncode == 0
    assert "--copy-plan" in res.stdout


def test_cli_migration_plan_copy_plan_json(tmp_path, monkeypatch):
    from lab_executor import extension_paths as ep_mod
    from lab_executor import extension_discovery as disc_mod
    from lab_executor import extension_migration as mig_mod

    legacy = tmp_path / "legacy"
    new = tmp_path / "new"
    _make_pack(legacy, "local.a")
    fake = _make_paths(legacy, new)
    monkeypatch.setattr(ep_mod, "get_extension_paths", lambda: fake)
    monkeypatch.setattr(disc_mod, "get_extension_paths", lambda: fake)
    monkeypatch.setattr(mig_mod, "get_extension_paths", lambda: fake)

    from lab_executor.cli import main
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["extension", "migration-plan", "--copy-plan",
                    "--json"])
    data = json.loads(buf.getvalue())
    assert "copy_plan" in data
    assert data["copy_plan"]["status"] == "ready"
    assert len(data["copy_plan"]["candidates"]) == 1
    assert data["copy_plan"]["apply_available"] is False
    assert data["schema_version"] == "v2.6"
    assert data["summary"]["copy_candidates"] == 1
    assert data["summary"]["copy_blocked"] is False


def test_cli_migration_plan_copy_plan_blocked_on_duplicate(
    tmp_path, monkeypatch,
):
    from lab_executor import extension_paths as ep_mod
    from lab_executor import extension_discovery as disc_mod
    from lab_executor import extension_migration as mig_mod

    legacy = tmp_path / "legacy"
    new = tmp_path / "new"
    _make_pack(legacy, "local.dup")
    _make_pack(new, "local.dup")
    fake = _make_paths(legacy, new)
    monkeypatch.setattr(ep_mod, "get_extension_paths", lambda: fake)
    monkeypatch.setattr(disc_mod, "get_extension_paths", lambda: fake)
    monkeypatch.setattr(mig_mod, "get_extension_paths", lambda: fake)

    from lab_executor.cli import main
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["extension", "migration-plan", "--copy-plan",
                    "--json"])
    data = json.loads(buf.getvalue())
    assert data["copy_plan"]["status"] == "blocked"
    assert data["copy_plan"]["candidates"] == []
    assert data["summary"]["copy_blocked"] is True
    # base status は error (duplicate あり)
    assert data["status"] == "error"
    assert rc == 1


# ============================================================
# Boundary / regression
# ============================================================


def test_no_pyvisa_visa_mcp_import_for_copy_plan():
    """copy-plan は PyVISA / visa_mcp 非依存"""
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
        "plan = plan_extension_migration(copy_plan=True)\n"
        "assert hasattr(plan, 'copy_plan')\n"
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


def test_v26_version():
    import lab_executor
    parts = lab_executor.__version__.split(".")
    assert int(parts[0]) >= 2 and int(parts[1]) >= 6


def test_mcp_tool_surface_unchanged_v26():
    """v2.6: Stable 43 + Experimental 7 = 50 不変"""
    from lab_executor import stability
    stable = [t for ts in stability.STABLE_TOOLS.values() for t in ts]
    exp = [t for ts in stability.EXPERIMENTAL_TOOLS.values() for t in ts]
    assert len(stable) == 43
    assert len(exp) == 7


def test_install_default_unchanged_v26():
    from lab_executor.extension_install import default_extensions_dir
    from lab_executor.extension_paths import get_extension_paths
    assert ".visa-mcp" in str(default_extensions_dir())
    assert get_extension_paths().write_default == default_extensions_dir()
