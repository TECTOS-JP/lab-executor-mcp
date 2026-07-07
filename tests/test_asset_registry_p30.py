"""v2.27.0: P3.0 資産レジストリ (asset publish / catalog + 共有ゲート) テスト

docs/asset_registry_p30_plan.md のテスト仕様を encode する。fixture / seeding
helper は test_asset_v01.py のものを再利用する。
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
import yaml

from lab_executor.asset import (
    AssetRegistryError,
    build_asset,
    catalog,
    check_asset,
    init_registry,
    load_index,
    publish_asset,
)

# v0.1 テストの fixture / seeding helper を再利用。
from tests.test_asset_v01 import (  # noqa: F401  (fixtures 使用)
    _MOCK_PSU_L3,
    _MOCK_PSU_L4L5,
    _seed_completed_job,
    _write_instruments_dir,
    analysis_file,
    store,
)


# ==============================================================
# 資産 zip 生成ヘルパ (L3 / L4 相当)
# ==============================================================


def _make_l4_asset(
    store, tmp_path, analysis_file, job_id="job_l4reg",
    *, out_name="l4.asset.zip", license_id="UNLICENSED", title="L4 asset",
) -> Path:
    """L4 相当 (requires 付きレシピ + capability 充足) の資産 zip を作る。"""
    _seed_completed_job(store, job_id)
    store.close()
    idir = _write_instruments_dir(tmp_path)
    out = tmp_path / out_name
    build_asset(
        job_id=job_id, db_path=tmp_path / "jobs.db",
        instruments_dir=idir, out_path=out, analysis_path=analysis_file,
        title=title, license_id=license_id,
        conditions={"calibration": "cal", "environment": "env"},
    )
    rep = check_asset(out)
    assert rep.level_verified == 4, rep.levels
    return out


def _make_l3_asset(
    store, tmp_path, analysis_file, job_id="job_l3reg",
    *, out_name="l3.asset.zip", license_id="UNLICENSED", title="L3 asset",
) -> Path:
    """L3 相当 (requires 無しレシピ) の資産 zip を作る。"""
    _seed_completed_job(store, job_id, recipe="measure_only")
    store.close()
    idir = _write_instruments_dir(tmp_path, _MOCK_PSU_L3, "mock_psu_l3")
    out = tmp_path / out_name
    build_asset(
        job_id=job_id, db_path=tmp_path / "jobs.db",
        instruments_dir=idir, out_path=out, analysis_path=analysis_file,
        title=title, license_id=license_id,
        conditions={"calibration": "cal", "environment": "env"},
    )
    rep = check_asset(out)
    assert rep.level_verified == 3, rep.levels
    return out


def _tamper_zip_in_place(zip_path: Path) -> None:
    """zip 内 1 ファイルを改変して再パック (contents checksum を壊す)。"""
    with zipfile.ZipFile(zip_path) as zf:
        data = {n: zf.read(n) for n in zf.namelist()}
    key = "bundle/results.csv" if "bundle/results.csv" in data else next(
        n for n in data if n.startswith("bundle/"))
    data[key] = data[key] + b"tampered\n"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for n, blob in data.items():
            zf.writestr(n, blob)
    zip_path.write_bytes(buf.getvalue())


# ==============================================================
# registry init
# ==============================================================


def test_registry_init_and_reject_double_init(tmp_path):
    reg = tmp_path / "reg"
    index = init_registry(reg, name="team-reg", visibility="team")
    assert index["visibility"] == "team"
    assert index["name"] == "team-reg"
    assert (reg / "INDEX.yaml").exists()
    assert (reg / "assets").is_dir()
    # 二重 init は拒否
    with pytest.raises(AssetRegistryError):
        init_registry(reg)


# ==============================================================
# publish (team)
# ==============================================================


def test_publish_to_team_registry(store, tmp_path, analysis_file):
    zp = _make_l4_asset(store, tmp_path, analysis_file, title="My L4")
    reg = tmp_path / "reg"
    init_registry(reg, visibility="team")
    res = publish_asset(zp, reg, tags=["psu", "ramp"])
    assert res["published"] is True
    assert res["level_verified"] == 4

    index = load_index(reg)
    assert len(index["assets"]) == 1
    entry = index["assets"][0]
    assert entry["id"] == res["id"]
    assert entry["title"] == "My L4"
    assert entry["level_verified"] == 4
    assert entry["tags"] == ["psu", "ramp"]
    assert entry["path"] == f"assets/{res['id']}.asset.zip"
    assert (reg / entry["path"]).exists()
    # requires_commands が資産内 recipe から反映される
    assert "set_voltage" in entry["requires_commands"]
    assert "query_voltage" in entry["requires_commands"]
    # 掲載された zip の sha256 が INDEX の値と一致
    import hashlib
    actual = hashlib.sha256((reg / entry["path"]).read_bytes()).hexdigest()
    assert actual == entry["sha256"]


def test_publish_requires_check_pass(store, tmp_path, analysis_file):
    zp = _make_l4_asset(store, tmp_path, analysis_file)
    _tamper_zip_in_place(zp)  # checksums を壊す
    reg = tmp_path / "reg"
    init_registry(reg, visibility="team")
    with pytest.raises(AssetRegistryError) as ei:
        publish_asset(zp, reg)
    assert "checksum" in str(ei.value).lower()
    # 掲載されていない
    assert load_index(reg)["assets"] == []


# ==============================================================
# external 共有ゲート
# ==============================================================


def test_external_gate_level_cap(store, tmp_path, analysis_file):
    reg = tmp_path / "reg"
    init_registry(reg, visibility="external")

    # L4 は拒否 (理由に L3 上限)
    zp4 = _make_l4_asset(
        store, tmp_path, analysis_file, license_id="CC-BY-4.0")
    with pytest.raises(AssetRegistryError) as ei:
        publish_asset(zp4, reg)
    assert "L3" in str(ei.value)
    assert load_index(reg)["assets"] == []


def test_external_gate_level_cap_l3_ok(store, tmp_path, analysis_file):
    reg = tmp_path / "reg"
    init_registry(reg, visibility="external")
    zp3 = _make_l3_asset(
        store, tmp_path, analysis_file, license_id="CC-BY-4.0")
    res = publish_asset(zp3, reg)
    assert res["published"] is True
    assert res["level_verified"] == 3


def test_external_gate_license(store, tmp_path, analysis_file):
    reg = tmp_path / "reg"
    init_registry(reg, visibility="external")

    # UNLICENSED L3 は拒否
    zp_bad = _make_l3_asset(
        store, tmp_path, analysis_file,
        out_name="l3_unl.asset.zip", license_id="UNLICENSED")
    with pytest.raises(AssetRegistryError) as ei:
        publish_asset(zp_bad, reg)
    assert "license" in str(ei.value).lower()
    assert load_index(reg)["assets"] == []


def test_external_gate_license_ccby_ok(store, tmp_path, analysis_file):
    reg = tmp_path / "reg"
    init_registry(reg, visibility="external")
    zp_ok = _make_l3_asset(
        store, tmp_path, analysis_file,
        out_name="l3_ccby.asset.zip", license_id="CC-BY-4.0")
    res = publish_asset(zp_ok, reg)
    assert res["published"] is True


# ==============================================================
# --force
# ==============================================================


def test_force_replaces_duplicate(store, tmp_path, analysis_file):
    zp = _make_l4_asset(store, tmp_path, analysis_file)
    reg = tmp_path / "reg"
    init_registry(reg, visibility="team")
    publish_asset(zp, reg, tags=["v1"])
    # 同 id 再 publish は拒否
    with pytest.raises(AssetRegistryError):
        publish_asset(zp, reg, tags=["v2"])
    # --force で置換
    res = publish_asset(zp, reg, tags=["v2"], force=True)
    index = load_index(reg)
    assert len(index["assets"]) == 1
    assert index["assets"][0]["tags"] == ["v2"]
    assert index["assets"][0]["id"] == res["id"]


def test_force_does_not_bypass_gate(store, tmp_path, analysis_file):
    # external + --force でも L4 はゲートで拒否される
    reg = tmp_path / "reg"
    init_registry(reg, visibility="external")
    zp4 = _make_l4_asset(
        store, tmp_path, analysis_file, license_id="CC-BY-4.0")
    with pytest.raises(AssetRegistryError) as ei:
        publish_asset(zp4, reg, force=True)
    assert "L3" in str(ei.value)
    assert load_index(reg)["assets"] == []


# ==============================================================
# catalog
# ==============================================================


def test_catalog_order_and_fields(store, tmp_path, analysis_file):
    reg = tmp_path / "reg"
    init_registry(reg, visibility="team")

    # L3 を先に publish、L4 を後に publish → catalog は L4 が先 (level 降順)
    zp3 = _make_l3_asset(
        store, tmp_path, analysis_file, job_id="jl3",
        out_name="c_l3.asset.zip")
    publish_asset(zp3, reg)

    # 別 store で L4 資産を作る (store は _make_* で close 済み)
    from lab_executor.job.store import JobStore
    s2 = JobStore(str(tmp_path / "jobs2.db"))
    _seed_completed_job(s2, "jl4")
    s2.close()
    idir = _write_instruments_dir(tmp_path)
    out4 = tmp_path / "c_l4.asset.zip"
    build_asset(
        job_id="jl4", db_path=tmp_path / "jobs2.db",
        instruments_dir=idir, out_path=out4, analysis_path=analysis_file,
        conditions={"calibration": "cal", "environment": "env"},
    )
    assert check_asset(out4).level_verified == 4
    publish_asset(out4, reg)

    entries = catalog(reg)
    assert len(entries) == 2
    assert entries[0]["level_verified"] == 4  # L4 が先
    assert entries[1]["level_verified"] == 3
    # requires_commands 反映 (L4 資産)
    assert "set_voltage" in entries[0]["requires_commands"]
    # L3 資産 (requires 無し) は空
    assert entries[1]["requires_commands"] == []


def test_catalog_recheck_detects_tamper(store, tmp_path, analysis_file):
    zp = _make_l4_asset(store, tmp_path, analysis_file)
    reg = tmp_path / "reg"
    init_registry(reg, visibility="team")
    res = publish_asset(zp, reg)

    # 掲載後の registry 内 zip を改変
    stored = reg / "assets" / f"{res['id']}.asset.zip"
    with zipfile.ZipFile(stored) as zf:
        data = {n: zf.read(n) for n in zf.namelist()}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for n, blob in data.items():
            zf.writestr(n, blob + b"x" if n == "asset.yaml" else blob)
    stored.write_bytes(buf.getvalue())

    entries = catalog(reg, recheck=True)
    assert entries[0]["integrity"] == "FAILED"
    # recheck 無しでは integrity 列は付かない
    assert "integrity" not in catalog(reg)[0]


# ==============================================================
# declare_level 自動宣言
# ==============================================================


@pytest.mark.parametrize("scenario", ["L2", "L3", "L4"])
def test_declare_level_auto(store, tmp_path, analysis_file, scenario):
    """declare_level 省略で export → check の verified と一致する。"""
    from lab_executor.job.store import JobStore

    idir_name = None
    if scenario == "L4":
        yaml_text, idir_name = _MOCK_PSU_L4L5, "mock_psu_l5"
        recipe = "ramp_voltage"
        analysis = analysis_file
        conditions = {"calibration": "cal", "environment": "env"}
        expected_verified = 4
    elif scenario == "L3":
        yaml_text, idir_name = _MOCK_PSU_L3, "mock_psu_l3"
        recipe = "measure_only"
        analysis = analysis_file
        conditions = {"calibration": "cal", "environment": "env"}
        expected_verified = 3
    else:  # L2: analysis 無し → L3 未満で止まる
        yaml_text, idir_name = _MOCK_PSU_L3, "mock_psu_l3"
        recipe = "measure_only"
        analysis = None
        conditions = {"calibration": "not_recorded",
                      "environment": "not_recorded"}
        expected_verified = 2

    _seed_completed_job(store, "jauto", recipe=recipe)
    store.close()
    idir = _write_instruments_dir(tmp_path, yaml_text, idir_name)
    out = tmp_path / f"auto_{scenario}.asset.zip"
    res = build_asset(
        job_id="jauto", db_path=tmp_path / "jobs.db",
        instruments_dir=idir, out_path=out, analysis_path=analysis,
        conditions=conditions,
        # declare_level は省略 (自動宣言)
    )
    rep = check_asset(out)
    assert rep.level_verified == expected_verified, rep.levels
    # 結合テストの核心: 自動宣言 == 検証レベル
    assert res["level_declared"] == rep.level_verified
    assert rep.level_declared == rep.level_verified


def test_declare_level_explicit_wins(store, tmp_path, analysis_file):
    """--declare-level 指定は自動判定に優先する。"""
    _seed_completed_job(store, "jexp")
    store.close()
    idir = _write_instruments_dir(tmp_path)
    out = tmp_path / "explicit.asset.zip"
    res = build_asset(
        job_id="jexp", db_path=tmp_path / "jobs.db",
        instruments_dir=idir, out_path=out, analysis_path=analysis_file,
        conditions={"calibration": "cal", "environment": "env"},
        declare_level=1,  # 資産自体は L4 相当だが 1 を宣言
    )
    assert res["level_declared"] == 1
    rep = check_asset(out)
    assert rep.level_declared == 1
    assert rep.level_verified == 4  # 検証は本来の値


# ==============================================================
# CLI roundtrip
# ==============================================================


def test_cli_roundtrip(store, tmp_path, analysis_file, capsys):
    from lab_executor.cli import main

    zp = _make_l4_asset(store, tmp_path, analysis_file, title="Roundtrip")
    reg = tmp_path / "clireg"

    rc = main([
        "asset", "registry-init", "--dir", str(reg),
        "--name", "cli-reg", "--visibility", "team",
    ])
    assert rc == 0
    assert (reg / "INDEX.yaml").exists()

    rc = main([
        "asset", "publish", str(zp),
        "--registry", str(reg), "--tags", "a,b", "--json",
    ])
    assert rc == 0

    rc = main([
        "asset", "catalog", "--registry", str(reg), "--check",
    ])
    assert rc == 0
    captured = capsys.readouterr()
    assert "Roundtrip" in captured.out
    assert "OK" in captured.out  # integrity 列
