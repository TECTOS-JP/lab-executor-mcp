"""v2.13.2 integration: 実 JobStore に `raw_response` 付き step を
保存し、`_extract_result_rows` が rows を返すことを確認する。

Codex のレビュー P2 への応答 (source string 検査だけでは
キー名不一致 bug を捕まえられなかったため)。
v2.14.2 で `job_store` / `seed_job` fixture に refactor。
"""
from __future__ import annotations
from unittest.mock import MagicMock

import pytest

from lab_executor.job.store import JobStore
from lab_executor.tools.export import _extract_result_rows


def _build_mgr_with_store(store: JobStore):
    mgr = MagicMock()
    mgr.store = store
    return mgr


def test_extract_result_rows_reads_raw_response_from_actual_store(
    job_store, seed_job
):
    """step_executor が `raw_response` キーで保存した query 結果が、
    `_extract_result_rows` で 1 行に変換されること。"""
    job_id = "job_test_v2_13_2"
    seed_job(job_store, job_id)
    row_id = job_store.record_step_started(job_id, 0, "command")
    job_store.record_step_completed(
        row_id,
        status="ok",
        result={
            "command": "measure_voltage",
            "args": {},
            "scpi_sent": "MEAS:VOLT?",
            "raw_response": "+1.234E+00",
            "success": True,
        },
    )

    mgr = _build_mgr_with_store(job_store)
    rows = _extract_result_rows(mgr, job_id)

    assert len(rows) == 1, (
        f"raw_response 付き query step は 1 行に変換されるべき "
        f"(実際: {len(rows)} 行)")
    row = rows[0]
    assert row["measurement"] == "measure_voltage"
    assert row["value"] == "+1.234E+00"
    assert row["step_index"] == 0


def test_extract_result_rows_reads_parsed_dict_alias(job_store, seed_job):
    """step_executor が `parsed` キーで dict を保存しても展開されること。
    旧名 `response_parsed` だけでなく `parsed` も読むことの保証。"""
    job_id = "job_parsed_test"
    seed_job(job_store, job_id)
    row_id = job_store.record_step_started(job_id, 0, "command")
    job_store.record_step_completed(
        row_id,
        status="ok",
        result={
            "command": "measure_voltage",
            "parsed": {"value": 1.234, "unit": "V"},
            "raw_response": "+1.234E+00",
            "success": True,
        },
    )

    mgr = _build_mgr_with_store(job_store)
    rows = _extract_result_rows(mgr, job_id)

    # v2.14.1 で parsed metadata 除外 + numeric だけ row 化したため、
    # `value` (numeric) は出るが `unit` (str) は出ない。
    assert len(rows) >= 1
    measurements = {r["measurement"] for r in rows}
    # 旧名 keys が混入していないこと
    assert "matched" not in measurements
    assert "fields" not in measurements
    # `value` (numeric) は何らかの形で含まれる
    assert any("value" in m for m in measurements)


def test_extract_result_rows_falls_back_to_legacy_keys(job_store, seed_job):
    """後方互換: 旧名 `response_raw` / `response_parsed` も読めること。"""
    job_id = "job_legacy"
    seed_job(job_store, job_id)
    row_id = job_store.record_step_started(job_id, 0, "command")
    job_store.record_step_completed(
        row_id,
        status="ok",
        result={
            "command": "old_cmd",
            "response_raw": "legacy_value",
            "success": True,
        },
    )
    mgr = _build_mgr_with_store(job_store)
    rows = _extract_result_rows(mgr, job_id)
    assert len(rows) == 1
    assert rows[0]["value"] == "legacy_value"
