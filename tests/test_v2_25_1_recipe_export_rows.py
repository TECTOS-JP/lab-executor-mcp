"""v2.25.1: レシピジョブ step result の export 結果行抽出 回帰テスト

recipe 実行ジョブ (_run_job_inner + step_executor) が保存する step result は
DSL の parsed.value_numeric (float) 形と異なり、
- result 直下に command / raw_response / value_numeric を持つ
- parsed.value_numeric / parsed.fields.value が数値文字列のまま残る
形がある。v2.25.0 まで _extract_result_rows はこれらから数値行を出せず、
asset check の L3 (raw_response <-> value_numeric pairing) に落ちていた。

修正: observation._value_numeric_from_result と同じ寛容な抽出で
`{command}.value_numeric` 行を補完する (既存の DSL/parsed 経路の行は不変)。
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

from lab_executor.asset import build_asset, check_asset
from lab_executor.tools import export as exp


def _mgr_with_store(store):
    m = MagicMock()
    m.store = store
    return m


def _seed_step(store, job_id: str, result: dict, step_index: int = 0) -> None:
    row_id = store.record_step_started(job_id, step_index, "command")
    store.record_step_completed(row_id, status="ok", result=result)


# ==============================================================
# _extract_result_rows: レシピジョブの step result 形
# ==============================================================


def test_recipe_top_level_value_numeric_emits_paired_rows(job_store, seed_job):
    """result 直下の value_numeric から raw 行と数値行の両方が出る。"""
    job_id = "job_recipe_vn_top"
    seed_job(job_store, job_id)
    _seed_step(job_store, job_id, {
        "command": "read_temp",
        "instrument": "GPIB0::3::INSTR",
        "raw_response": "+00027.2E+0",
        "value_numeric": 27.2,
        "success": True,
    })
    rows = exp._extract_result_rows(_mgr_with_store(job_store), job_id)
    assert len(rows) == 2
    by_meas = {r["measurement"]: r for r in rows}
    assert by_meas["read_temp"]["value"] == "+00027.2E+0"
    assert by_meas["read_temp.value_numeric"]["value"] == 27.2
    # 列契約: 標準 columns のみで構成できる
    for r in rows:
        for col in exp.RESULT_COLUMNS:
            assert col in r


def test_recipe_parsed_value_numeric_string_is_coerced(job_store, seed_job):
    """parsed.value_numeric が数値文字列でも数値行が出る (UI と同じ寛容さ)。"""
    job_id = "job_recipe_vn_str"
    seed_job(job_store, job_id)
    _seed_step(job_store, job_id, {
        "command": "read_temp",
        "instrument": "GPIB0::3::INSTR",
        "raw_response": "+00027.2E+0",
        "parsed": {"matched": False, "fields": {}, "raw": "+00027.2E+0",
                   "value_numeric": "27.2"},
        "success": True,
    })
    rows = exp._extract_result_rows(_mgr_with_store(job_store), job_id)
    values = {r["measurement"]: r["value"] for r in rows}
    assert values["read_temp"] == "+00027.2E+0"
    assert values["read_temp.value_numeric"] == 27.2


def test_recipe_parsed_fields_value_string_is_coerced(job_store, seed_job):
    """parsed.fields.value が数値文字列でも数値行が出る。"""
    job_id = "job_recipe_fields_str"
    seed_job(job_store, job_id)
    _seed_step(job_store, job_id, {
        "command": "read_temp",
        "instrument": "GPIB0::3::INSTR",
        "raw_response": "NTKC+00027.2E+0",
        "parsed": {"matched": True, "raw": "NTKC+00027.2E+0",
                   "fields": {"status": "Normal", "value": "27.2"}},
        "success": True,
    })
    rows = exp._extract_result_rows(_mgr_with_store(job_store), job_id)
    values = {r["measurement"]: r["value"] for r in rows}
    assert values["read_temp"] == "NTKC+00027.2E+0"
    assert values["read_temp.value_numeric"] == 27.2


def test_recipe_raw_only_rows_unchanged(job_store, seed_job):
    """数値が導出できない step は従来通り raw 行 1 本のみ (行を捏造しない)。"""
    job_id = "job_recipe_raw_only"
    seed_job(job_store, job_id)
    _seed_step(job_store, job_id, {
        "command": "read_temp",
        "instrument": "GPIB0::3::INSTR",
        "raw_response": "GARBAGE",
        "success": True,
    })
    rows = exp._extract_result_rows(_mgr_with_store(job_store), job_id)
    assert len(rows) == 1
    assert rows[0]["measurement"] == "read_temp"
    assert rows[0]["value"] == "GARBAGE"


def test_recipe_bool_value_numeric_not_coerced(job_store, seed_job):
    """value_numeric が bool の場合は数値扱いしない。"""
    job_id = "job_recipe_bool_vn"
    seed_job(job_store, job_id)
    _seed_step(job_store, job_id, {
        "command": "check_output",
        "instrument": "GPIB0::3::INSTR",
        "raw_response": "ON",
        "value_numeric": True,
        "success": True,
    })
    rows = exp._extract_result_rows(_mgr_with_store(job_store), job_id)
    assert [r["measurement"] for r in rows] == ["check_output"]


def test_dsl_parsed_rows_unchanged(job_store, seed_job):
    """DSL 形 (parsed.value_numeric が float / fields が float) の行は
    v2.25.0 と同一 (補完行が重複しない)。"""
    job_id = "job_dsl_regression"
    seed_job(job_store, job_id)
    _seed_step(job_store, job_id, {
        "command": "measure_voltage",
        "instrument": "GPIB0::1::INSTR",
        "raw_response": "JPPC+0029*1A+0",
        "parsed": {"matched": False, "fields": {}, "raw": "JPPC+0029*1A+0",
                   "value_numeric": 29.0, "fallback_used": "numeric_extract"},
        "success": True,
    }, step_index=0)
    _seed_step(job_store, job_id, {
        "command": "read_temp",
        "instrument": "GPIB0::1::INSTR",
        "raw_response": "NTKC+00027.2E+0",
        "parsed": {"matched": True, "raw": "NTKC+00027.2E+0",
                   "matched_pattern_index": 1,
                   "fields": {"status": "Normal", "value": 27.2}},
        "success": True,
    }, step_index=1)
    rows = exp._extract_result_rows(_mgr_with_store(job_store), job_id)
    assert sorted(r["measurement"] for r in rows) == [
        "measure_voltage.value_numeric", "read_temp.value",
    ]


# ==============================================================
# asset E2E: 実レシピジョブ形の資産が L3 に到達する
# ==============================================================

_MOCK_TC = """
metadata:
  manufacturer: visa-mcp
  model: MockTC
  category: temperature_controller
  support_level: tested
commands:
  read_temp:
    scpi: "MEAS?"
    type: query
    polling_safe: true
recipes:
  measure_once:
    description: read temperature once
    steps:
      - { command: read_temp }
"""


def test_asset_check_recipe_job_reaches_l3(job_store, tmp_path):
    """recipe 実行ジョブの step result 形 (直下 command/raw_response/
    value_numeric) の資産が asset check で L3 に到達する。"""
    job_id = "job_recipe_l3"
    job_store._connect().execute(
        "INSERT INTO jobs (job_id, owner, resource_name, recipe, "
        "parameters_json, status, current_step_index, created_at, "
        "updated_at) VALUES (?, 'tester', 'GPIB0::3::INSTR', "
        "'measure_once', ?, 'completed', 1, "
        "'2026-07-01T00:00:00Z', '2026-07-01T00:00:00Z')",
        (job_id, json.dumps({"n": 1})),
    )
    _seed_step(job_store, job_id, {
        "command": "read_temp",
        "instrument": "GPIB0::3::INSTR",
        "raw_response": "+00027.2E+0",
        "value_numeric": 27.2,
        "success": True,
    })
    job_store.record_event(job_id, "job_completed")

    instr_dir = tmp_path / "instruments"
    instr_dir.mkdir()
    (instr_dir / "mock_tc.yaml").write_text(_MOCK_TC, encoding="utf-8")
    analysis = tmp_path / "README.md"
    analysis.write_text("# 再解析手順", encoding="utf-8")

    out = tmp_path / "recipe_l3.asset.zip"
    build_asset(
        job_id=job_id, db_path=tmp_path / "jobs.db",
        instruments_dir=instr_dir, out_path=out,
        analysis_path=analysis,
        conditions={"calibration": "not_recorded",
                    "environment": "not_recorded"},
    )
    rep = check_asset(out)
    assert rep.levels["L3"]["ok"], rep.levels["L3"]
    assert rep.level_verified >= 3

    # results.jsonl に raw 行と数値行の両方が残っている
    with zipfile.ZipFile(out) as zf:
        lines = [json.loads(x) for x in
                 zf.read("bundle/results.jsonl").decode("utf-8").splitlines()
                 if x.strip()]
    values = {r["measurement"]: r["value"] for r in lines}
    assert values["read_temp"] == "+00027.2E+0"
    assert values["read_temp.value_numeric"] == 27.2
