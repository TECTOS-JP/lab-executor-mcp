"""v2.25.0: 実験資産 v0.1 (asset export / check + L4/L5) テスト

docs/asset_v01_plan.md のテスト仕様を encode する。
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
import yaml

from lab_executor.asset import build_asset, check_asset, match_capabilities
from lab_executor.asset.manifest import AssetManifest
from lab_executor.job.store import JobStore
from lab_executor.models.instrument_def import (
    CapabilityRequirements,
    InstrumentDefinition,
    RangeSpec,
)


# ==============================================================
# fixtures / seeding helpers
# ==============================================================

RESOURCE = "GPIB0::1::INSTR"

# requires 付きレシピを持つ mock 定義 (既存 registry 定義は変更しない)
_MOCK_PSU_L4L5 = """
metadata:
  manufacturer: visa-mcp
  model: MockPSU-L5
  category: power_supply
  support_level: tested
commands:
  set_voltage:
    scpi: "VOLT {voltage}"
    type: write
    parameters:
      - { name: voltage, type: float, range: [0, 30] }
    verify:
      readback_command: query_voltage
      arg_key: voltage
      tolerance: 0.05
  query_voltage:
    scpi: "MEAS:VOLT?"
    type: query
    polling_safe: true
state_query:
  voltage:
    command: query_voltage
    unit: V
safety:
  ratings:
    voltage:
      rated: 30
      absolute_max: 33
safe_shutdown:
  - command: set_voltage
    args: { voltage: 0 }
recipes:
  ramp_voltage:
    description: ramp then measure
    requires:
      commands: [set_voltage, query_voltage]
      ranges:
        "set_voltage.voltage": { min: 0, max: 10 }
    steps:
      - { command: set_voltage, args: { voltage: 5 } }
      - { command: query_voltage }
"""


def _write_instruments_dir(tmp_path: Path, yaml_text: str = _MOCK_PSU_L4L5,
                           name: str = "mock_psu_l5") -> Path:
    d = tmp_path / "instruments"
    d.mkdir(exist_ok=True)
    (d / f"{name}.yaml").write_text(yaml_text, encoding="utf-8")
    return d


def _seed_completed_job(
    store: JobStore,
    job_id: str,
    *,
    recipe: str = "ramp_voltage",
    with_steps: bool = True,
    resource: str = RESOURCE,
) -> None:
    """recipe / parameters / created_at / resource_name を持つ completed job。"""
    store._connect().execute(
        "INSERT INTO jobs (job_id, owner, resource_name, recipe, "
        "parameters_json, status, current_step_index, created_at, "
        "updated_at) VALUES (?, 'tester', ?, ?, ?, 'completed', 1, "
        "'2026-07-01T00:00:00Z', '2026-07-01T00:00:00Z')",
        (job_id, resource, recipe, json.dumps({"voltage": 5})),
    )
    if with_steps:
        row_id = store.record_step_started(job_id, 0, "command")
        store.record_step_completed(
            row_id, status="ok",
            result={
                "command": "query_voltage",
                "instrument": resource,
                "raw_response": "+5.0E+0",
                "parsed": {"value_numeric": 5.0, "raw": "+5.0E+0",
                           "matched": True},
                "success": True,
            },
        )
    store.record_event(job_id, "job_completed")


@pytest.fixture
def store(tmp_path):
    s = JobStore(str(tmp_path / "jobs.db"))
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def analysis_file(tmp_path):
    p = tmp_path / "analysis.md"
    p.write_text("# Analysis\n\n1. load results.csv\n", encoding="utf-8")
    return p


# ==============================================================
# build_asset
# ==============================================================


def test_build_asset_basic(store, tmp_path, analysis_file):
    _seed_completed_job(store, "job_a")
    store.close()
    idir = _write_instruments_dir(tmp_path)
    out = tmp_path / "a.asset.zip"
    res = build_asset(
        job_id="job_a", db_path=tmp_path / "jobs.db",
        instruments_dir=idir, out_path=out, analysis_path=analysis_file,
        title="Basic",
    )
    assert Path(res["path"]).exists()
    assert res["contents_count"] >= 1
    assert len(res["asset_id"]) > 0

    # asset.yaml がスキーマ通り、contents の sha256 が実ファイルと一致
    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
        assert "asset.yaml" in names
        manifest = AssetManifest(
            **yaml.safe_load(zf.read("asset.yaml").decode("utf-8")))
        import hashlib
        for entry in manifest.contents:
            assert entry.path in names
            actual = hashlib.sha256(zf.read(entry.path)).hexdigest()
            assert actual == entry.sha256


# ==============================================================
# check_asset (levels)
# ==============================================================


# requires: を持たないレシピ定義 (L3 止まりの確認用)
_MOCK_PSU_L3 = """
metadata:
  manufacturer: visa-mcp
  model: MockPSU-L3
  category: dmm
  support_level: tested
commands:
  query_voltage:
    scpi: "MEAS:VOLT?"
    type: query
    polling_safe: true
state_query:
  voltage:
    command: query_voltage
    unit: V
recipes:
  measure_only:
    description: just measure
    steps:
      - { command: query_voltage }
"""


def test_check_l3_full_asset(store, tmp_path, analysis_file):
    _seed_completed_job(store, "job_l3", recipe="measure_only")
    store.close()
    idir = _write_instruments_dir(tmp_path, _MOCK_PSU_L3, "mock_psu_l3")
    out = tmp_path / "l3.asset.zip"
    build_asset(
        job_id="job_l3", db_path=tmp_path / "jobs.db",
        instruments_dir=idir, out_path=out, analysis_path=analysis_file,
        conditions={"calibration": "cal-2026", "environment": "23C"},
    )
    rep = check_asset(out)
    assert rep.schema_ok
    assert rep.checksums_ok
    assert rep.levels["L4"]["ok"] is False  # requires 無し
    assert rep.level_verified == 3, rep.levels


def test_check_l2_without_analysis(store, tmp_path):
    _seed_completed_job(store, "job_l2")
    store.close()
    idir = _write_instruments_dir(tmp_path)
    out = tmp_path / "l2.asset.zip"
    build_asset(
        job_id="job_l2", db_path=tmp_path / "jobs.db",
        instruments_dir=idir, out_path=out, analysis_path=None,
        conditions={"calibration": "not_recorded",
                    "environment": "not_recorded"},
    )
    rep = check_asset(out)
    assert rep.level_verified == 2, rep.levels


def test_check_l1_without_instrument(store, tmp_path, analysis_file):
    # instruments_dir に recipe を持つ定義が無い → instrument 同梱されない
    _seed_completed_job(store, "job_l1", recipe="unknown_recipe")
    store.close()
    empty = tmp_path / "empty_instruments"
    empty.mkdir()
    out = tmp_path / "l1.asset.zip"
    build_asset(
        job_id="job_l1", db_path=tmp_path / "jobs.db",
        instruments_dir=empty, out_path=out, analysis_path=analysis_file,
    )
    rep = check_asset(out)
    assert rep.level_verified == 1, rep.levels


def test_check_l0_minimal(store, tmp_path):
    # recipe / parameters なしの不完全 job → L1 不成立で L0 止まり
    store._connect().execute(
        "INSERT INTO jobs (job_id, owner, resource_name, status, "
        "current_step_index, created_at, updated_at) VALUES "
        "('job_l0', 'owner_only', '', 'completed', 0, "
        "'2026-07-01T00:00:00Z', '2026-07-01T00:00:00Z')",
    )
    row_id = store.record_step_started("job_l0", 0, "command")
    store.record_step_completed(
        row_id, status="ok",
        result={"command": "q", "instrument": "x",
                "raw_response": "1.0", "success": True},
    )
    store.close()
    empty = tmp_path / "empty"
    empty.mkdir()
    out = tmp_path / "l0.asset.zip"
    build_asset(
        job_id="job_l0", db_path=tmp_path / "jobs.db",
        instruments_dir=empty, out_path=out,
    )
    rep = check_asset(out)
    assert rep.level_verified == 0, rep.levels


def test_check_detects_tampering(store, tmp_path, analysis_file):
    _seed_completed_job(store, "job_t")
    store.close()
    idir = _write_instruments_dir(tmp_path)
    out = tmp_path / "t.asset.zip"
    build_asset(
        job_id="job_t", db_path=tmp_path / "jobs.db",
        instruments_dir=idir, out_path=out, analysis_path=analysis_file,
    )
    # zip 内 1 ファイルを改変して再パック
    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(out) as zin, zipfile.ZipFile(
        tampered, "w", zipfile.ZIP_DEFLATED
    ) as zout:
        for item in zin.namelist():
            data = zin.read(item)
            if item == "bundle/results.csv":
                data = data + b"tampered\n"
            zout.writestr(item, data)
    rep = check_asset(tampered)
    assert rep.checksums_ok is False
    assert rep.integrity_broken is True


# ==============================================================
# manifest schema
# ==============================================================


def test_manifest_schema_rejects_bad():
    with pytest.raises(Exception):
        AssetManifest(asset_id="x", level_declared=9)  # 範囲外


# ==============================================================
# requires backward-compat
# ==============================================================


def test_requires_optional_backcompat():
    """requires 無し既存 YAML の検証結果が不変 (mock registry 全定義)。"""
    from lab_executor.registry import validate_instrument_file

    mock_dir = Path("registry/instruments/mock")
    for p in sorted(mock_dir.glob("*.yaml")):
        rep = validate_instrument_file(p)
        # requires フィールドを足してもパース・検証結果が壊れない
        assert rep.status in ("ok", "warning"), (p, rep.errors)
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
        defn = InstrumentDefinition(**raw)
        for recipe in (defn.recipes or {}).values():
            assert recipe.requires is None


# ==============================================================
# capability matching
# ==============================================================


def test_match_capabilities():
    raw = yaml.safe_load(_MOCK_PSU_L4L5)
    defn = InstrumentDefinition(**raw)

    # 1. satisfied
    req_ok = CapabilityRequirements(
        commands=["set_voltage", "query_voltage"],
        ranges={"set_voltage.voltage": RangeSpec(min=0, max=10)},
    )
    m = match_capabilities(req_ok, defn)
    assert m["satisfied"] is True
    assert m["missing_commands"] == []

    # 2. missing_commands
    req_missing = CapabilityRequirements(commands=["set_current"])
    m2 = match_capabilities(req_missing, defn)
    assert m2["satisfied"] is False
    assert "set_current" in m2["missing_commands"]

    # 3. range violation (要求 max=50 > device max=30)
    req_range = CapabilityRequirements(
        commands=["set_voltage"],
        ranges={"set_voltage.voltage": RangeSpec(min=0, max=50)},
    )
    m3 = match_capabilities(req_range, defn)
    assert m3["satisfied"] is False
    assert m3["range_violations"]


# ==============================================================
# L4 / L5
# ==============================================================


def test_check_l4(store, tmp_path, analysis_file):
    _seed_completed_job(store, "job_l4")
    store.close()
    idir = _write_instruments_dir(tmp_path)
    out = tmp_path / "l4.asset.zip"
    build_asset(
        job_id="job_l4", db_path=tmp_path / "jobs.db",
        instruments_dir=idir, out_path=out, analysis_path=analysis_file,
        conditions={"calibration": "cal", "environment": "env"},
    )
    rep = check_asset(out)
    assert rep.levels["L4"]["ok"] is True, rep.levels
    assert rep.level_verified == 4, rep.levels


def test_check_l5(store, tmp_path, analysis_file):
    _seed_completed_job(store, "job_l5")
    store.close()
    idir = _write_instruments_dir(tmp_path)
    out = tmp_path / "l5.asset.zip"
    build_asset(
        job_id="job_l5", db_path=tmp_path / "jobs.db",
        instruments_dir=idir, out_path=out, analysis_path=analysis_file,
        conditions={"calibration": "cal", "environment": "env"},
        hazards={"none_declared": False, "voltage_max": 30,
                 "temperature_max": None, "chemicals": [], "notes": ""},
        expected_results=[{"command": "query_voltage", "value_min": 4.9,
                           "value_max": 5.1}],
    )
    # dry_run.ok は builder が書かないので、asset.yaml を書き換えて注入
    _inject_dry_run_ok(out, tmp_path)
    rep = check_asset(out / "" if False else out)
    assert rep.levels["L5"]["ok"] is True, rep.levels
    assert rep.level_verified == 5, rep.levels


def test_check_l5_missing_hazards(store, tmp_path, analysis_file):
    _seed_completed_job(store, "job_l5m")
    store.close()
    idir = _write_instruments_dir(tmp_path)
    out = tmp_path / "l5m.asset.zip"
    build_asset(
        job_id="job_l5m", db_path=tmp_path / "jobs.db",
        instruments_dir=idir, out_path=out, analysis_path=analysis_file,
        conditions={"calibration": "cal", "environment": "env"},
        hazards=None,  # hazards 欠落
        expected_results=[{"command": "query_voltage", "value_min": 4.9,
                           "value_max": 5.1}],
    )
    _inject_dry_run_ok(out, tmp_path)
    rep = check_asset(out)
    assert rep.levels["L5"]["ok"] is False
    assert any("hazards" in m for m in rep.levels["L5"]["missing"])
    assert rep.level_verified == 4, rep.levels


def _inject_dry_run_ok(zip_path: Path, tmp_path: Path) -> None:
    """asset.yaml に dry_run.ok=true を注入し、contents の asset.yaml 以外の
    sha256 はそのままに再パックする (asset.yaml は contents に含まれない)。"""
    import io
    with zipfile.ZipFile(zip_path) as zf:
        data = {n: zf.read(n) for n in zf.namelist()}
    manifest = yaml.safe_load(data["asset.yaml"].decode("utf-8"))
    manifest["dry_run"] = {"performed_at": "2026-07-01T00:00:00Z",
                           "ok": True}
    data["asset.yaml"] = yaml.safe_dump(
        manifest, allow_unicode=True, sort_keys=False).encode("utf-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for n, blob in data.items():
            zf.writestr(n, blob)
    zip_path.write_bytes(buf.getvalue())


# ==============================================================
# CLI
# ==============================================================


def test_cli_export_and_check(store, tmp_path, analysis_file, capsys):
    from lab_executor.cli import main

    _seed_completed_job(store, "job_cli")
    store.close()
    idir = _write_instruments_dir(tmp_path)
    out = tmp_path / "cli.asset.zip"

    rc = main([
        "asset", "export", "--job", "job_cli",
        "--db", str(tmp_path / "jobs.db"),
        "--instruments-dir", str(idir),
        "--out", str(out),
        "--analysis", str(analysis_file),
        "--json",
    ])
    assert rc == 0
    assert out.exists()

    rc2 = main(["asset", "check", str(out), "--json"])
    assert rc2 == 0
    captured = capsys.readouterr()
    # 最後の JSON 出力に level_verified が含まれる
    assert "level_verified" in captured.out


def test_cli_check_tampered_exit1(store, tmp_path, analysis_file):
    from lab_executor.cli import main

    _seed_completed_job(store, "job_cli2")
    store.close()
    idir = _write_instruments_dir(tmp_path)
    out = tmp_path / "cli2.asset.zip"
    build_asset(
        job_id="job_cli2", db_path=tmp_path / "jobs.db",
        instruments_dir=idir, out_path=out, analysis_path=analysis_file,
    )
    # tamper
    tampered = tmp_path / "cli2_tampered.zip"
    with zipfile.ZipFile(out) as zin, zipfile.ZipFile(
        tampered, "w", zipfile.ZIP_DEFLATED
    ) as zout:
        for item in zin.namelist():
            data = zin.read(item)
            if item == "bundle/results.jsonl":
                data = data + b'{"x":1}\n'
            zout.writestr(item, data)
    rc = main(["asset", "check", str(tampered)])
    assert rc == 1
