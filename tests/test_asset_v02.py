"""v2.26.0: 実験資産 v0.2 (dry-run 接続 + --meta で CLI 単独 L5 到達) テスト

docs/asset_v02_plan.md のテスト仕様を encode する。fixture / seeding helper は
test_asset_v01.py のものを再利用する。
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
import yaml

from lab_executor.asset import build_asset, check_asset

# v0.1 テストの fixture / seeding helper を再利用 (requires 付き mock 定義 +
# completed job seeding)。
from tests.test_asset_v01 import (  # noqa: F401  (fixtures 使用)
    _MOCK_PSU_L4L5,
    _seed_completed_job,
    _write_instruments_dir,
    analysis_file,
    store,
)


# requires を持つが recipe_to_plan がコンパイルできない壊れたレシピ。
# args の式が未定義変数を参照し、resolve_arg が ExpressionError を投げる。
_MOCK_PSU_BROKEN = """
metadata:
  manufacturer: visa-mcp
  model: MockPSU-Broken
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
    description: broken ramp
    requires:
      commands: [set_voltage, query_voltage]
    steps:
      - { command: set_voltage, args: { voltage: "$undefined_var * 2" } }
      - { command: query_voltage }
"""


def _l5_meta() -> dict:
    """L5 到達に必要な hazards + expected_results を含む meta。"""
    return {
        "conditions": {
            "calibration": "2026-06 校正証明書 #1234",
            "environment": "23±1°C, 45%RH",
        },
        "hazards": {"none_declared": False, "voltage_max": 30},
        "expected_results": [
            {"command": "query_voltage", "value_min": 4.9, "value_max": 5.1},
        ],
        "sample": {"uuid": None, "metadata": {"description": "無負荷基線"}},
    }


def _read_manifest(zip_path: Path) -> dict:
    with zipfile.ZipFile(zip_path) as zf:
        return yaml.safe_load(zf.read("asset.yaml").decode("utf-8"))


# ==============================================================
# dry-run 接続
# ==============================================================


def test_dry_run_now_ok(store, tmp_path, analysis_file):
    _seed_completed_job(store, "job_dr_ok")
    store.close()
    idir = _write_instruments_dir(tmp_path)
    out = tmp_path / "dr_ok.asset.zip"
    res = build_asset(
        job_id="job_dr_ok", db_path=tmp_path / "jobs.db",
        instruments_dir=idir, out_path=out, analysis_path=analysis_file,
        conditions={"calibration": "cal", "environment": "env"},
        dry_run_now=True,
    )
    dr = res["dry_run"]
    assert dr is not None
    assert dr["ok"] is True
    assert dr["performed_at"]
    assert isinstance(dr["step_count"], int) and dr["step_count"] >= 1
    assert dr["method"] == "recipe_to_plan+validate@export"

    manifest = _read_manifest(out)
    assert manifest["dry_run"]["ok"] is True
    assert manifest["dry_run"]["step_count"] == dr["step_count"]


def test_dry_run_now_records_failure(store, tmp_path, analysis_file):
    _seed_completed_job(store, "job_dr_fail")
    store.close()
    idir = _write_instruments_dir(
        tmp_path, _MOCK_PSU_BROKEN, "mock_psu_broken")
    out = tmp_path / "dr_fail.asset.zip"
    res = build_asset(
        job_id="job_dr_fail", db_path=tmp_path / "jobs.db",
        instruments_dir=idir, out_path=out, analysis_path=analysis_file,
        conditions={"calibration": "cal", "environment": "env"},
        dry_run_now=True,
    )
    # export 自体は成功する
    assert Path(res["path"]).exists()
    dr = res["dry_run"]
    assert dr is not None
    assert dr["ok"] is False
    assert dr["error"]

    manifest = _read_manifest(out)
    assert manifest["dry_run"]["ok"] is False
    assert manifest["dry_run"]["error"]


# ==============================================================
# --meta マージ
# ==============================================================


def test_meta_file_merge(store, tmp_path, analysis_file):
    _seed_completed_job(store, "job_meta")
    store.close()
    idir = _write_instruments_dir(tmp_path)
    out = tmp_path / "meta.asset.zip"
    build_asset(
        job_id="job_meta", db_path=tmp_path / "jobs.db",
        instruments_dir=idir, out_path=out, analysis_path=analysis_file,
        meta=_l5_meta(),
    )
    manifest = _read_manifest(out)
    assert manifest["conditions"]["calibration"].startswith("2026-06")
    assert manifest["conditions"]["environment"].startswith("23")
    assert manifest["hazards"]["voltage_max"] == 30
    assert manifest["expected_results"][0]["command"] == "query_voltage"
    assert manifest["sample"]["metadata"]["description"] == "無負荷基線"


def test_meta_unknown_key_rejected(store, tmp_path, analysis_file):
    from lab_executor.cli import main

    _seed_completed_job(store, "job_meta_bad")
    store.close()
    idir = _write_instruments_dir(tmp_path)
    out = tmp_path / "meta_bad.asset.zip"
    meta_file = tmp_path / "bad_meta.yaml"
    meta_file.write_text(
        "conditions:\n  calibration: cal\n"
        "typo_section:\n  foo: bar\n",
        encoding="utf-8",
    )
    rc = main([
        "asset", "export", "--job", "job_meta_bad",
        "--db", str(tmp_path / "jobs.db"),
        "--instruments-dir", str(idir),
        "--out", str(out),
        "--meta", str(meta_file),
    ])
    assert rc == 1
    assert not out.exists()


# ==============================================================
# CLI 単独 L5 (end-to-end)
# ==============================================================


def test_cli_l5_end_to_end(store, tmp_path, analysis_file, capsys):
    from lab_executor.cli import main

    _seed_completed_job(store, "job_l5_cli")
    store.close()
    idir = _write_instruments_dir(tmp_path)
    out = tmp_path / "l5_cli.asset.zip"
    meta_file = tmp_path / "l5_meta.yaml"
    meta_file.write_text(
        yaml.safe_dump(_l5_meta(), allow_unicode=True), encoding="utf-8")

    rc = main([
        "asset", "export", "--job", "job_l5_cli",
        "--db", str(tmp_path / "jobs.db"),
        "--instruments-dir", str(idir),
        "--out", str(out),
        "--analysis", str(analysis_file),
        "--meta", str(meta_file),
        "--dry-run-now",
    ])
    assert rc == 0
    assert out.exists()

    rep = check_asset(out)
    assert rep.levels["L5"]["ok"] is True, rep.levels
    assert rep.level_verified == 5, rep.levels


def test_dry_run_not_requested_unchanged(store, tmp_path, analysis_file):
    _seed_completed_job(store, "job_no_dr")
    store.close()
    idir = _write_instruments_dir(tmp_path)
    out = tmp_path / "no_dr.asset.zip"
    res = build_asset(
        job_id="job_no_dr", db_path=tmp_path / "jobs.db",
        instruments_dir=idir, out_path=out, analysis_path=analysis_file,
        conditions={"calibration": "cal", "environment": "env"},
    )
    assert res["dry_run"] is None
    manifest = _read_manifest(out)
    # v0.1 と同形: dry_run は null のまま
    assert manifest["dry_run"] is None
