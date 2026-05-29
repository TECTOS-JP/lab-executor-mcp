"""v2.13.2 integration: 実 JobStore に `raw_response` 付き step を
保存し、`_extract_result_rows` が rows を返すことを確認する。

Codex のレビュー P2 への応答 (source string 検査だけでは
キー名不一致 bug を捕まえられなかったため)。
"""
from __future__ import annotations
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lab_executor.job.store import JobStore
from lab_executor.tools.export import _extract_result_rows


def _build_mgr_with_store(store: JobStore):
    mgr = MagicMock()
    mgr.store = store
    return mgr


def test_extract_result_rows_reads_raw_response_from_actual_store(tmp_path: Path):
    """step_executor が `raw_response` キーで保存した query 結果が、
    `_extract_result_rows` で 1 行に変換されること。"""
    db = tmp_path / "results.db"
    store = JobStore(str(db))

    # job を作って step を 1 つ INSERT + UPDATE
    job_id = "job_test_v2_13_2"
    # JobStore.create が必要かもしれないので簡易 INSERT
    store._connect().execute(
        "INSERT INTO jobs (job_id, owner, resource_name, status, "
        "current_step_index, created_at, updated_at) "
        "VALUES (?, '', '', 'completed', 0, '2026-01-01T00:00:00Z', "
        "'2026-01-01T00:00:00Z')",
        (job_id,),
    )

    row_id = store.record_step_started(job_id, 0, "command")
    store.record_step_completed(
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

    mgr = _build_mgr_with_store(store)
    rows = _extract_result_rows(mgr, job_id)

    assert len(rows) == 1, (
        f"raw_response 付き query step は 1 行に変換されるべき "
        f"(実際: {len(rows)} 行)")
    row = rows[0]
    assert row["measurement"] == "measure_voltage"
    assert row["value"] == "+1.234E+00"
    assert row["step_index"] == 0


def test_extract_result_rows_reads_parsed_dict_alias(tmp_path: Path):
    """step_executor が `parsed` キーで dict を保存しても展開されること。
    旧名 `response_parsed` だけでなく `parsed` も読むことの保証。"""
    db = tmp_path / "results2.db"
    store = JobStore(str(db))
    job_id = "job_parsed_test"
    store._connect().execute(
        "INSERT INTO jobs (job_id, owner, resource_name, status, "
        "current_step_index, created_at, updated_at) "
        "VALUES (?, '', '', 'completed', 0, '2026-01-01T00:00:00Z', "
        "'2026-01-01T00:00:00Z')",
        (job_id,),
    )
    row_id = store.record_step_started(job_id, 0, "command")
    store.record_step_completed(
        row_id,
        status="ok",
        result={
            "command": "measure_voltage",
            "parsed": {"value": 1.234, "unit": "V"},
            "raw_response": "+1.234E+00",
            "success": True,
        },
    )

    mgr = _build_mgr_with_store(store)
    rows = _extract_result_rows(mgr, job_id)

    # parsed dict は各 key で 1 行 (value, unit の 2 行)
    assert len(rows) >= 2
    measurements = {r["measurement"] for r in rows}
    assert "value" in measurements
    assert "unit" in measurements


def test_extract_result_rows_falls_back_to_legacy_keys(tmp_path: Path):
    """後方互換: 旧名 `response_raw` / `response_parsed` も読めること。"""
    db = tmp_path / "results_legacy.db"
    store = JobStore(str(db))
    job_id = "job_legacy"
    store._connect().execute(
        "INSERT INTO jobs (job_id, owner, resource_name, status, "
        "current_step_index, created_at, updated_at) "
        "VALUES (?, '', '', 'completed', 0, '2026-01-01T00:00:00Z', "
        "'2026-01-01T00:00:00Z')",
        (job_id,),
    )
    row_id = store.record_step_started(job_id, 0, "command")
    store.record_step_completed(
        row_id,
        status="ok",
        result={
            "command": "old_cmd",
            "response_raw": "legacy_value",
            "success": True,
        },
    )
    mgr = _build_mgr_with_store(store)
    rows = _extract_result_rows(mgr, job_id)
    assert len(rows) == 1
    assert rows[0]["value"] == "legacy_value"
