"""v2.7.0: Controlled Extension Copy Apply tests.

apply は実ファイル操作を伴うため、必ず tmp_path 内に migration_logs
を切り出し、ユーザーの ~/.lab-executor/migration_logs/ には書かない
こと (`log_dir=tmp_path/"logs"`)。
"""
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
    (pack_dir / "README.md").write_text(f"# {ext_id}\n", encoding="utf-8")
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
    if not p.exists():
        return set()
    out: set[tuple[str, int]] = set()
    for f in p.rglob("*"):
        if f.is_file():
            out.add((str(f.relative_to(p)), f.stat().st_size))
    return out


# ============================================================
# apply preconditions
# ============================================================


def test_apply_copies_legacy_only_to_new_path(tmp_path):
    from lab_executor.extension_migration import apply_extension_copy_plan
    legacy = tmp_path / "legacy"
    new = tmp_path / "new"
    logs = tmp_path / "logs"
    _make_pack(legacy, "local.a")
    _make_pack(legacy, "local.b")
    res = apply_extension_copy_plan(
        paths=_make_paths(legacy, new), log_dir=logs,
    )
    assert res.status == "ok"
    assert len(res.copied) == 2
    ids = {c["extension_id"] for c in res.copied}
    assert ids == {"local.a", "local.b"}
    # new path 側に作成されたか
    assert (new / "local_a" / "extension.yaml").exists()
    assert (new / "local_b" / "extension.yaml").exists()


def test_apply_does_not_delete_source(tmp_path):
    from lab_executor.extension_migration import apply_extension_copy_plan
    legacy = tmp_path / "legacy"
    new = tmp_path / "new"
    logs = tmp_path / "logs"
    _make_pack(legacy, "local.a")
    before = _snapshot(legacy)
    apply_extension_copy_plan(
        paths=_make_paths(legacy, new), log_dir=logs,
    )
    after = _snapshot(legacy)
    assert before == after, "legacy (source) が変更された"


def test_apply_does_not_overwrite_target(tmp_path):
    """target が既に存在する場合は copy せず skipped + status=blocked
    (copy_plan 段階で全 target_exists なら blocked)"""
    from lab_executor.extension_migration import apply_extension_copy_plan
    legacy = tmp_path / "legacy"
    new = tmp_path / "new"
    logs = tmp_path / "logs"
    _make_pack(legacy, "local.a")
    # target を先に作っておく
    (new / "local_a").mkdir(parents=True)
    (new / "local_a" / "marker.txt").write_text("preexisting",
                                                  encoding="utf-8")
    target_before = _snapshot(new / "local_a")
    res = apply_extension_copy_plan(
        paths=_make_paths(legacy, new), log_dir=logs,
    )
    # copy_plan 段階で target_exists により blocked になる
    assert res.status == "blocked"
    assert res.copied == []
    # 既存 target は触られていない
    assert _snapshot(new / "local_a") == target_before


def test_apply_fails_when_duplicate_exists(tmp_path):
    from lab_executor.extension_migration import apply_extension_copy_plan
    legacy = tmp_path / "legacy"
    new = tmp_path / "new"
    logs = tmp_path / "logs"
    _make_pack(legacy, "local.dup")
    _make_pack(new, "local.dup")
    res = apply_extension_copy_plan(
        paths=_make_paths(legacy, new), log_dir=logs,
    )
    assert res.status == "blocked"
    rcs = [r["reason_class"] for r in res.blocked_reasons]
    # copy_plan_not_ready または duplicate 系のいずれかは入る
    assert any("duplicate" in rc or "not_ready" in rc for rc in rcs)
    assert res.copied == []


def test_apply_fails_when_invalid_metadata(tmp_path):
    from lab_executor.extension_migration import apply_extension_copy_plan
    legacy = tmp_path / "legacy"
    new = tmp_path / "new"
    logs = tmp_path / "logs"
    legacy.mkdir(parents=True)
    p = legacy / "broken"
    p.mkdir()
    (p / "extension.yaml").write_text(
        yaml.safe_dump({"version": "0.1"}), encoding="utf-8",
    )
    res = apply_extension_copy_plan(
        paths=_make_paths(legacy, new), log_dir=logs,
    )
    assert res.status == "blocked"
    assert res.copied == []


def test_apply_writes_manifest(tmp_path):
    from lab_executor.extension_migration import apply_extension_copy_plan
    legacy = tmp_path / "legacy"
    new = tmp_path / "new"
    logs = tmp_path / "logs"
    _make_pack(legacy, "local.a")
    res = apply_extension_copy_plan(
        paths=_make_paths(legacy, new), log_dir=logs,
    )
    assert res.manifest_path is not None
    assert res.manifest_path.exists()
    data = json.loads(res.manifest_path.read_text(encoding="utf-8"))
    assert data["schema_version"] == "v2.7"
    assert data["operation"] == "extension_copy_apply"
    assert data["status"] == "ok"
    assert data["delete_performed"] is False
    assert data["overwrite_performed"] is False
    assert len(data["copied"]) == 1


def test_apply_writes_manifest_even_when_blocked(tmp_path):
    """blocked でも manifest は残す (audit のため)"""
    from lab_executor.extension_migration import apply_extension_copy_plan
    legacy = tmp_path / "legacy"
    new = tmp_path / "new"
    logs = tmp_path / "logs"
    _make_pack(legacy, "local.dup")
    _make_pack(new, "local.dup")
    res = apply_extension_copy_plan(
        paths=_make_paths(legacy, new), log_dir=logs,
    )
    assert res.status == "blocked"
    assert res.manifest_path is not None
    assert res.manifest_path.exists()


def test_apply_recomputes_plan_before_copy(tmp_path):
    """apply 内部で plan_extension_migration() を再呼出している
    (実装上 contract)。filesystem を直前に変更すれば挙動が変わる
    ことで間接確認。"""
    from lab_executor.extension_migration import apply_extension_copy_plan
    legacy = tmp_path / "legacy"
    new = tmp_path / "new"
    logs = tmp_path / "logs"
    _make_pack(legacy, "local.a")
    # apply 前に legacy.a を消す
    import shutil as _sh
    _sh.rmtree(legacy / "local_a")
    res = apply_extension_copy_plan(
        paths=_make_paths(legacy, new), log_dir=logs,
    )
    # candidates が再計算で 0 件になる → blocked
    assert res.status == "blocked"
    assert res.copied == []


def test_apply_no_overwrite_performed_flag(tmp_path):
    from lab_executor.extension_migration import apply_extension_copy_plan
    legacy = tmp_path / "legacy"
    new = tmp_path / "new"
    logs = tmp_path / "logs"
    _make_pack(legacy, "local.a")
    res = apply_extension_copy_plan(
        paths=_make_paths(legacy, new), log_dir=logs,
    )
    assert res.delete_performed is False
    assert res.overwrite_performed is False


# ============================================================
# CLI: --apply requires --copy-plan
# ============================================================


def test_cli_apply_requires_copy_plan(monkeypatch):
    """--apply 単独は exit 2"""
    from lab_executor.cli import main
    import io
    from contextlib import redirect_stderr
    buf = io.StringIO()
    with redirect_stderr(buf):
        rc = main(["extension", "migration-plan", "--apply"])
    assert rc == 2
    assert "requires --copy-plan" in buf.getvalue()


def test_cli_apply_ok(tmp_path, monkeypatch):
    from lab_executor import extension_paths as ep_mod
    from lab_executor import extension_discovery as disc_mod
    from lab_executor import extension_migration as mig_mod

    legacy = tmp_path / "legacy"
    new = tmp_path / "new"
    logs = tmp_path / "logs"
    _make_pack(legacy, "local.a")
    fake = _make_paths(legacy, new)
    monkeypatch.setattr(ep_mod, "get_extension_paths", lambda: fake)
    monkeypatch.setattr(disc_mod, "get_extension_paths", lambda: fake)
    monkeypatch.setattr(mig_mod, "get_extension_paths", lambda: fake)
    # migration_logs を tmp に向ける
    monkeypatch.setattr(mig_mod, "_migration_log_dir", lambda: logs)

    from lab_executor.cli import main
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["extension", "migration-plan", "--copy-plan",
                    "--apply", "--json"])
    data = json.loads(buf.getvalue())
    assert data["status"] == "ok"
    assert len(data["copied"]) == 1
    assert data["delete_performed"] is False
    assert data["overwrite_performed"] is False
    assert rc == 0
    assert (new / "local_a" / "extension.yaml").exists()


def test_cli_apply_blocked_returns_1(tmp_path, monkeypatch):
    from lab_executor import extension_paths as ep_mod
    from lab_executor import extension_discovery as disc_mod
    from lab_executor import extension_migration as mig_mod
    legacy = tmp_path / "legacy"
    new = tmp_path / "new"
    logs = tmp_path / "logs"
    _make_pack(legacy, "local.dup")
    _make_pack(new, "local.dup")
    fake = _make_paths(legacy, new)
    monkeypatch.setattr(ep_mod, "get_extension_paths", lambda: fake)
    monkeypatch.setattr(disc_mod, "get_extension_paths", lambda: fake)
    monkeypatch.setattr(mig_mod, "get_extension_paths", lambda: fake)
    monkeypatch.setattr(mig_mod, "_migration_log_dir", lambda: logs)

    from lab_executor.cli import main
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["extension", "migration-plan", "--copy-plan",
                    "--apply", "--json"])
    data = json.loads(buf.getvalue())
    assert data["status"] == "blocked"
    assert rc == 1


# ============================================================
# Boundary / regression
# ============================================================


def test_no_pyvisa_visa_mcp_import_for_apply():
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
        "apply_extension_copy_plan\n"
        "import lab_executor.extension_migration as m\n"
        "assert hasattr(m, 'ExtensionCopyApplyResult')\n"
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


def test_v27_version():
    import lab_executor
    parts = lab_executor.__version__.split(".")
    assert int(parts[0]) >= 2 and int(parts[1]) >= 7


def test_mcp_tool_surface_unchanged_v27():
    from lab_executor import stability
    stable = [t for ts in stability.STABLE_TOOLS.values() for t in ts]
    exp = [t for ts in stability.EXPERIMENTAL_TOOLS.values() for t in ts]
    assert len(stable) == 43
    assert len(exp) == 7


def test_install_default_unchanged_v27():
    from lab_executor.extension_install import default_extensions_dir
    from lab_executor.extension_paths import get_extension_paths
    assert ".visa-mcp" in str(default_extensions_dir())
    assert get_extension_paths().write_default == default_extensions_dir()
