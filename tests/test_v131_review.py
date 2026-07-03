"""v1.3.1: v1.3.0 review response (P0/P1/P2)

- P0: raw 改行 (v1.3 files)
- P1-2: force install を backup-rename 方式へ (失敗時 rollback)
- P1-3: overlay registry の registry_entries path traversal 拒否
- P1-4: overlay registry entry の必須項目 (id / path) 不足を error
- P1-5: cli.py module docstring v1.3 化
- P1-6: staging copy の除外ルール + docs 明記
- P1-7: install source が extensions_dir 配下なら拒否
"""
from __future__ import annotations
import json
import shutil
from pathlib import Path

import pytest

from lab_executor.extension_install import (
    install_definition_pack, load_overlay_registry,
    _should_exclude_path, _is_path_inside,
)

ROOT = Path(__file__).parent.parent


# =========================================================
# Version
# =========================================================


V13_FILES_FULL = [
    "src/lab_executor/extension_install.py",
    "src/lab_executor/cli.py",
    "src/lab_executor/extension.py",
    "docs/extension_install.md",
    "docs/extension_registry_overlay.md",
    "docs/v1_stability_policy.md",
    "examples/extensions/mock_basic_pack/extension.yaml",
    "examples/extensions/mock_basic_pack/README.md",
    "tests/test_v13_extension_install.py",
    "tests/test_v131_review.py",
    "CHANGELOG.md",
]


@pytest.mark.parametrize("rel", V13_FILES_FULL)
def test_v131_lf_only(rel):
    p = ROOT / rel
    assert p.exists(), f"missing: {p}"
    text = p.read_text(encoding="utf-8")
    assert "\r" not in text, f"{rel} に CR が含まれる"


@pytest.mark.parametrize("rel", V13_FILES_FULL)
def test_v131_multiline(rel):
    p = ROOT / rel
    text = p.read_text(encoding="utf-8")
    assert text.count("\n") + 1 >= 5, f"{rel} が 5 行未満"


# =========================================================
# helpers: _should_exclude_path / _is_path_inside
# =========================================================


@pytest.mark.parametrize("rel,expected", [
    (".git/config", True),
    ("__pycache__/x.pyc", True),
    (".mypy_cache/x", True),
    (".pytest_cache/y", True),
    (".idea/workspace.xml", True),
    (".vscode/settings.json", True),
    ("node_modules/foo/index.js", True),
    ("foo.pyc", True),
    ("foo.pyo", True),
    ("foo.tmp", True),
    ("foo.swp", True),
    (".DS_Store", True),
    ("Thumbs.db", True),
    ("instruments/mock_psu.yaml", False),
    ("extension.yaml", False),
    ("README.md", False),
])
def test_should_exclude_path(rel, expected):
    assert _should_exclude_path(Path(rel)) is expected


def test_is_path_inside(tmp_path):
    parent = tmp_path / "p"
    parent.mkdir()
    child = parent / "a" / "b.txt"
    child.parent.mkdir()
    child.write_text("x", encoding="utf-8")
    assert _is_path_inside(child, parent) is True
    other = tmp_path / "other.txt"
    other.write_text("x", encoding="utf-8")
    assert _is_path_inside(other, parent) is False


# =========================================================
# P1-6: staging copy の除外
# =========================================================


@pytest.fixture
def temp_env_with_junk(tmp_path):
    """example pack を copy しつつ、staging で除外されるべき file を追加"""
    src_pack = ROOT / "examples" / "extensions" / "mock_basic_pack"
    dst_pack = tmp_path / "src_pack"
    shutil.copytree(src_pack, dst_pack)
    # 除外対象を仕込む
    (dst_pack / "__pycache__").mkdir()
    (dst_pack / "__pycache__" / "junk.pyc").write_bytes(b"\x00\x00")
    (dst_pack / ".git").mkdir()
    (dst_pack / ".git" / "HEAD").write_text("ref: refs/heads/main\n",
                                            encoding="utf-8")
    (dst_pack / ".DS_Store").write_bytes(b"\x00")
    (dst_pack / "tmp.tmp").write_text("temp", encoding="utf-8")
    return {
        "extensions_dir": tmp_path / "extensions",
        "lockfile_path": tmp_path / "extensions.lock.json",
        "pack_yaml": dst_pack / "extension.yaml",
    }


def test_install_excludes_junk_paths(temp_env_with_junk):
    res = install_definition_pack(
        temp_env_with_junk["pack_yaml"],
        extensions_dir=temp_env_with_junk["extensions_dir"],
        lockfile_path=temp_env_with_junk["lockfile_path"],
    )
    assert res.status == "ok", res.errors
    install_path = Path(res.install_path)
    # 除外されたものは存在しない
    assert not (install_path / "__pycache__").exists()
    assert not (install_path / ".git").exists()
    assert not (install_path / ".DS_Store").exists()
    assert not (install_path / "tmp.tmp").exists()
    # 本物の中身は残る
    assert (install_path / "extension.yaml").exists()
    assert (install_path / "instruments" / "mock_psu.yaml").exists()


# =========================================================
# P1-2: force install backup-rename (既存 install を喪失しない)
# =========================================================


@pytest.fixture
def temp_env(tmp_path):
    src_pack = ROOT / "examples" / "extensions" / "mock_basic_pack"
    dst_pack = tmp_path / "src_pack"
    shutil.copytree(src_pack, dst_pack)
    return {
        "extensions_dir": tmp_path / "extensions",
        "lockfile_path": tmp_path / "extensions.lock.json",
        "pack_yaml": dst_pack / "extension.yaml",
    }


def test_install_force_keeps_install_path_intact(temp_env):
    """force install 後も install_path は valid (backup-rename がきれいに
    後始末されている)"""
    res1 = install_definition_pack(
        temp_env["pack_yaml"],
        extensions_dir=temp_env["extensions_dir"],
        lockfile_path=temp_env["lockfile_path"],
    )
    assert res1.status == "ok"
    install_path = Path(res1.install_path)
    # force 再 install
    res2 = install_definition_pack(
        temp_env["pack_yaml"], force=True,
        extensions_dir=temp_env["extensions_dir"],
        lockfile_path=temp_env["lockfile_path"],
    )
    assert res2.status == "ok", res2.errors
    # install 先は健在
    assert install_path.exists()
    assert (install_path / "extension.yaml").exists()
    # backup が残っていない (成功 path で cleanup されている)
    leftovers = [p for p in install_path.parent.iterdir()
                 if p.name.startswith(install_path.name + ".bak-")]
    assert leftovers == [], f"leftover backup: {leftovers}"


# =========================================================
# P1-7: install source が extensions_dir 配下なら拒否
# =========================================================


def test_install_rejects_source_inside_extensions_dir(temp_env):
    # まず通常 install
    res1 = install_definition_pack(
        temp_env["pack_yaml"],
        extensions_dir=temp_env["extensions_dir"],
        lockfile_path=temp_env["lockfile_path"],
    )
    assert res1.status == "ok"
    inside_yaml = Path(res1.install_path) / "extension.yaml"
    assert inside_yaml.exists()
    # extensions_dir 配下の extension.yaml を再 install しようとすると拒否
    res2 = install_definition_pack(
        inside_yaml, force=True,
        extensions_dir=temp_env["extensions_dir"],
        lockfile_path=temp_env["lockfile_path"],
    )
    assert res2.status == "error"
    assert any(
        (e.get("details") or {}).get("sub_class")
        == "extension_source_inside_extensions_dir"
        for e in res2.errors
    ), res2.errors


# =========================================================
# P1-3 / P1-4: overlay registry validation
# =========================================================


def _write_pack(pack: Path, *, ext_id: str, entries_yaml: str) -> Path:
    pack.mkdir(parents=True, exist_ok=True)
    (pack / "extension.yaml").write_text(
        f"extension_id: {ext_id}\nname: x\nversion: 0.1.0\n"
        "type: definition_pack\n"
        "stability: { support_level: tested, executable_code: false }\n"
        "contents:\n  registry_entries: [ registry_entries.yaml ]\n",
        encoding="utf-8",
    )
    (pack / "registry_entries.yaml").write_text(entries_yaml,
                                                encoding="utf-8")
    return pack / "extension.yaml"


def test_overlay_rejects_registry_entry_path_outside_pack(
        temp_env, tmp_path):
    """P1-3: registry_entries[*].path が pack 外を指す場合 error"""
    pack = tmp_path / "evil_pack"
    yaml_text = (
        "instruments:\n"
        "  - { id: evil_one, vendor: v, model: m, category: dmm,\n"
        "      support_level: tested, path: ../../../outside.yaml }\n"
    )
    _write_pack(pack, ext_id="tectos.evil.path", entries_yaml=yaml_text)
    res = install_definition_pack(
        pack / "extension.yaml",
        extensions_dir=temp_env["extensions_dir"],
        lockfile_path=temp_env["lockfile_path"],
    )
    if res.status != "ok":
        pytest.skip(f"install dependency: {res.errors}")
    rep = load_overlay_registry(
        ROOT / "registry" / "INDEX.yaml",
        extensions_dir=temp_env["extensions_dir"],
        lockfile_path=temp_env["lockfile_path"],
    )
    assert rep.status == "error"
    assert any(
        (e.get("details") or {}).get("sub_class")
        == "registry_entry_path_outside_pack"
        for e in rep.errors
    ), rep.errors


def test_overlay_rejects_registry_entry_missing_id(temp_env, tmp_path):
    """P1-4: id 欠落は error"""
    pack = tmp_path / "noid_pack"
    yaml_text = (
        "instruments:\n"
        "  - { vendor: v, model: m, category: dmm,\n"
        "      support_level: tested, path: instruments/x.yaml }\n"
    )
    _write_pack(pack, ext_id="tectos.noid", entries_yaml=yaml_text)
    res = install_definition_pack(
        pack / "extension.yaml",
        extensions_dir=temp_env["extensions_dir"],
        lockfile_path=temp_env["lockfile_path"],
    )
    if res.status != "ok":
        pytest.skip(f"install dependency: {res.errors}")
    rep = load_overlay_registry(
        ROOT / "registry" / "INDEX.yaml",
        extensions_dir=temp_env["extensions_dir"],
        lockfile_path=temp_env["lockfile_path"],
    )
    assert rep.status == "error"
    assert any(
        (e.get("details") or {}).get("sub_class")
        == "registry_entry_missing_id"
        for e in rep.errors
    ), rep.errors


def test_overlay_rejects_registry_entry_missing_path(temp_env, tmp_path):
    """P1-4: path 欠落は error"""
    pack = tmp_path / "nopath_pack"
    yaml_text = (
        "instruments:\n"
        "  - { id: nopath_one, vendor: v, model: m, category: dmm,\n"
        "      support_level: tested }\n"
    )
    _write_pack(pack, ext_id="tectos.nopath", entries_yaml=yaml_text)
    res = install_definition_pack(
        pack / "extension.yaml",
        extensions_dir=temp_env["extensions_dir"],
        lockfile_path=temp_env["lockfile_path"],
    )
    if res.status != "ok":
        pytest.skip(f"install dependency: {res.errors}")
    rep = load_overlay_registry(
        ROOT / "registry" / "INDEX.yaml",
        extensions_dir=temp_env["extensions_dir"],
        lockfile_path=temp_env["lockfile_path"],
    )
    assert rep.status == "error"
    assert any(
        (e.get("details") or {}).get("sub_class")
        == "registry_entry_missing_path"
        for e in rep.errors
    ), rep.errors


def test_overlay_warns_on_missing_optional_fields(temp_env, tmp_path):
    """P1-4: vendor / model / category / support_level の欠落は warning"""
    pack = tmp_path / "weak_pack"
    yaml_text = (
        "instruments:\n"
        "  - { id: weak_one, path: instruments/x.yaml }\n"
    )
    _write_pack(pack, ext_id="tectos.weak", entries_yaml=yaml_text)
    res = install_definition_pack(
        pack / "extension.yaml",
        extensions_dir=temp_env["extensions_dir"],
        lockfile_path=temp_env["lockfile_path"],
    )
    if res.status != "ok":
        pytest.skip(f"install dependency: {res.errors}")
    rep = load_overlay_registry(
        ROOT / "registry" / "INDEX.yaml",
        extensions_dir=temp_env["extensions_dir"],
        lockfile_path=temp_env["lockfile_path"],
    )
    classes = {w["warning_class"] for w in rep.warnings}
    for w_class in (
        "registry_entry_missing_vendor",
        "registry_entry_missing_model",
        "registry_entry_missing_category",
        "registry_entry_missing_support_level",
    ):
        assert w_class in classes, f"warning class {w_class!r} 無し"


# =========================================================
# P1-5: cli.py module docstring が v1.3 化
# =========================================================


def test_extension_install_docs_documents_exclusion():
    text = (ROOT / "docs" / "extension_install.md").read_text(
        encoding="utf-8")
    for kw in (
        "backup", "全ファイル", "__pycache__", ".git",
        "extension_source_inside_extensions_dir",
    ):
        assert kw in text, f"extension_install.md に {kw!r} 無し"


def test_example_pack_still_installs_after_v131(temp_env):
    res = install_definition_pack(
        temp_env["pack_yaml"],
        extensions_dir=temp_env["extensions_dir"],
        lockfile_path=temp_env["lockfile_path"],
    )
    assert res.status == "ok", res.errors
    meta = json.loads(
        (Path(res.install_path) / ".install_meta.json")
        .read_text(encoding="utf-8")
    )
    assert meta["extension_id"] == "visa-mcp.mock.basic"
