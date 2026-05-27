"""v2.8.0: Migration Log Inspection + Copied Pack Verification tests."""
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


def _apply(tmp_path: Path) -> Path:
    """legacy.a を copy して manifest path を返す"""
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


# ============================================================
# list / load
# ============================================================


def test_migration_log_list_empty(tmp_path):
    from lab_executor.extension_migration_log import (
        list_extension_migration_logs,
    )
    logs = list_extension_migration_logs(log_dir=tmp_path / "no_such")
    assert logs == []


def test_migration_log_list_after_apply(tmp_path):
    from lab_executor.extension_migration_log import (
        list_extension_migration_logs,
    )
    _apply(tmp_path)
    logs = list_extension_migration_logs(log_dir=tmp_path / "logs")
    assert len(logs) == 1
    assert logs[0].operation == "extension_copy_apply"
    assert logs[0].status == "ok"
    assert logs[0].copied_count == 1


def test_migration_log_inspect_manifest(tmp_path):
    from lab_executor.extension_migration_log import (
        load_extension_migration_log,
    )
    mp = _apply(tmp_path)
    m = load_extension_migration_log(mp)
    assert m.schema_version == "v2.7"
    assert m.operation == "extension_copy_apply"
    assert m.status == "ok"
    assert m.delete_performed is False
    assert m.overwrite_performed is False
    assert len(m.copied) == 1


def test_load_rejects_unsupported_schema(tmp_path):
    from lab_executor.extension_migration_log import (
        load_extension_migration_log,
    )
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({
        "schema_version": "v999",
        "operation": "extension_copy_apply",
    }), encoding="utf-8")
    with pytest.raises(ValueError):
        load_extension_migration_log(p)


def test_load_rejects_unsupported_operation(tmp_path):
    from lab_executor.extension_migration_log import (
        load_extension_migration_log,
    )
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({
        "schema_version": "v2.7",
        "operation": "extension_delete_apply",
    }), encoding="utf-8")
    with pytest.raises(ValueError):
        load_extension_migration_log(p)


# ============================================================
# verify
# ============================================================


def test_migration_log_verify_ok(tmp_path):
    from lab_executor.extension_migration_log import (
        verify_extension_migration_log,
    )
    mp = _apply(tmp_path)
    res = verify_extension_migration_log(mp)
    assert res.status == "ok"
    assert res.failed == []
    assert res.checked[0]["target_exists"] is True
    assert res.checked[0]["extension_yaml_readable"] is True
    assert res.checked[0]["extension_id_match"] is True


def test_migration_log_verify_target_missing_error(tmp_path):
    from lab_executor.extension_migration_log import (
        verify_extension_migration_log,
    )
    import shutil as _sh
    mp = _apply(tmp_path)
    # target を消す
    _sh.rmtree(tmp_path / "new" / "local_a")
    res = verify_extension_migration_log(mp)
    assert res.status == "error"
    rcs = [f["error_class"] for f in res.failed]
    assert "target_missing" in rcs


def test_migration_log_verify_extension_id_mismatch_error(tmp_path):
    from lab_executor.extension_migration_log import (
        verify_extension_migration_log,
    )
    mp = _apply(tmp_path)
    # target extension.yaml を別の id で書き換える
    target_yaml = tmp_path / "new" / "local_a" / "extension.yaml"
    target_yaml.write_text(
        yaml.safe_dump({"extension_id": "local.tampered",
                          "version": "0.1.0"}),
        encoding="utf-8",
    )
    res = verify_extension_migration_log(mp)
    assert res.status == "error"
    rcs = [f["error_class"] for f in res.failed]
    assert "extension_id_mismatch" in rcs


def test_migration_log_verify_source_missing_warning(tmp_path):
    """source は将来整理される可能性があるので warning 扱い"""
    from lab_executor.extension_migration_log import (
        verify_extension_migration_log,
    )
    import shutil as _sh
    mp = _apply(tmp_path)
    # source を消す (target は残す)
    _sh.rmtree(tmp_path / "legacy" / "local_a")
    res = verify_extension_migration_log(mp)
    # target が無事なら overall は warning
    assert res.status == "warning"
    wc = [w["warning_class"] for w in res.warnings]
    assert "source_missing" in wc


def test_migration_log_verify_delete_performed_true_error(tmp_path):
    """改ざんで delete_performed=true になっている manifest は error"""
    from lab_executor.extension_migration_log import (
        verify_extension_migration_log,
    )
    mp = _apply(tmp_path)
    data = json.loads(mp.read_text(encoding="utf-8"))
    data["delete_performed"] = True
    mp.write_text(json.dumps(data), encoding="utf-8")
    res = verify_extension_migration_log(mp)
    assert res.status == "error"
    rcs = [f["error_class"] for f in res.failed]
    assert "delete_performed_unexpected" in rcs


def test_migration_log_verify_overwrite_performed_true_error(tmp_path):
    from lab_executor.extension_migration_log import (
        verify_extension_migration_log,
    )
    mp = _apply(tmp_path)
    data = json.loads(mp.read_text(encoding="utf-8"))
    data["overwrite_performed"] = True
    mp.write_text(json.dumps(data), encoding="utf-8")
    res = verify_extension_migration_log(mp)
    assert res.status == "error"
    rcs = [f["error_class"] for f in res.failed]
    assert "overwrite_performed_unexpected" in rcs


# ============================================================
# manifest write failure -> partial_failure (v2.8.0 P0)
# ============================================================


def test_manifest_write_failure_marks_partial_failure(tmp_path,
                                                       monkeypatch):
    """_write_manifest が失敗すると status=partial_failure に格上げ +
    failed[] に manifest_write_failed を記録する"""
    from lab_executor.extension_migration import apply_extension_copy_plan
    from lab_executor import extension_migration as mig_mod

    legacy = tmp_path / "legacy"
    new = tmp_path / "new"
    logs = tmp_path / "logs"
    _make_pack(legacy, "local.a")

    def _broken(*a, **kw):
        raise OSError("disk full (test)")
    monkeypatch.setattr(mig_mod, "_write_manifest", _broken)

    res = apply_extension_copy_plan(
        paths=_make_paths(legacy, new), log_dir=logs,
    )
    assert res.status == "partial_failure"
    assert res.manifest_path is None
    err_classes = [f.get("error_class") for f in res.failed]
    assert "manifest_write_failed" in err_classes
    # copy 自体は完了している
    assert len(res.copied) == 1
    assert (new / "local_a" / "extension.yaml").exists()


# ============================================================
# CLI
# ============================================================


def test_cli_migration_log_help():
    res = subprocess.run(
        [sys.executable, "-m", "lab_executor.cli", "extension",
         "migration-log", "--help"],
        text=True, capture_output=True, encoding="utf-8",
    )
    assert res.returncode == 0
    for sub in ("list", "inspect", "verify"):
        assert sub in res.stdout


def test_cli_migration_log_list_json(tmp_path, monkeypatch):
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
        rc = main(["extension", "migration-log", "list", "--json"])
    data = json.loads(buf.getvalue())
    assert data["count"] == 1
    assert data["logs"][0]["operation"] == "extension_copy_apply"
    assert rc == 0


def test_cli_migration_log_inspect_json(tmp_path):
    mp = _apply(tmp_path)
    from lab_executor.cli import main
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["extension", "migration-log", "inspect",
                    str(mp), "--json"])
    data = json.loads(buf.getvalue())
    assert data["schema_version"] == "v2.7"
    assert data["delete_performed"] is False
    assert data["overwrite_performed"] is False
    assert rc == 0


def test_cli_migration_log_verify_json_ok(tmp_path):
    mp = _apply(tmp_path)
    from lab_executor.cli import main
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["extension", "migration-log", "verify",
                    str(mp), "--json"])
    data = json.loads(buf.getvalue())
    assert data["status"] == "ok"
    assert rc == 0


def test_cli_migration_log_verify_target_missing(tmp_path):
    import shutil as _sh
    mp = _apply(tmp_path)
    _sh.rmtree(tmp_path / "new" / "local_a")
    from lab_executor.cli import main
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["extension", "migration-log", "verify",
                    str(mp), "--json"])
    data = json.loads(buf.getvalue())
    assert data["status"] == "error"
    assert rc == 1


def test_cli_migration_log_verify_strict_on_warning(tmp_path):
    import shutil as _sh
    mp = _apply(tmp_path)
    _sh.rmtree(tmp_path / "legacy" / "local_a")
    from lab_executor.cli import main
    import io
    from contextlib import redirect_stdout
    # default: warning -> exit 0
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["extension", "migration-log", "verify",
                    str(mp), "--json"])
    assert rc == 0
    # --strict: warning -> exit 1
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["extension", "migration-log", "verify",
                    str(mp), "--json", "--strict"])
    assert rc == 1


# ============================================================
# Boundary / regression
# ============================================================


def test_no_pyvisa_visa_mcp_import_for_migration_log():
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
        "    list_extension_migration_logs,\n"
        "    load_extension_migration_log,\n"
        "    verify_extension_migration_log,\n"
        ")\n"
        "logs = list_extension_migration_logs()\n"
        "assert isinstance(logs, list)\n"
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


def test_v28_version():
    import lab_executor
    parts = lab_executor.__version__.split(".")
    assert int(parts[0]) >= 2 and int(parts[1]) >= 8


def test_mcp_tool_surface_unchanged_v28():
    from lab_executor import stability
    stable = [t for ts in stability.STABLE_TOOLS.values() for t in ts]
    exp = [t for ts in stability.EXPERIMENTAL_TOOLS.values() for t in ts]
    assert len(stable) == 43
    assert len(exp) == 7


def test_install_default_unchanged_v28():
    from lab_executor.extension_install import default_extensions_dir
    from lab_executor.extension_paths import get_extension_paths
    assert ".visa-mcp" in str(default_extensions_dir())
    assert get_extension_paths().write_default == default_extensions_dir()
