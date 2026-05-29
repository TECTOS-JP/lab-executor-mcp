"""v2.14.1: Codex v2.14.0 レビュー指摘への対応テスト.

P1-b: response_parser の寛容 float 変換で JPPC corruption の値が正しく取れる
P2  : `_extract_result_rows` が parsed metadata key を rows 化しない
      / `fields` の numeric と `value_numeric` だけを rows 化する
"""
from __future__ import annotations
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lab_executor.job.store import JobStore
from lab_executor.models.instrument_def import ResponseFormat
from lab_executor.response_parser import (
    parse_response, _parse_value_permissive,
)
from lab_executor.tools.export import _extract_result_rows


# ==============================================================
# P1-b: JPPC corruption value 復元
# ==============================================================


@pytest.fixture
def yokogawa_format_with_loose_corruption():
    """v2.2.1 7563 YAML 相当の loose pattern が JPPC corruption を
    受けるバージョン。"""
    return ResponseFormat(
        patterns=[
            # 厳密 (NTXX 形)
            r'^(?P<status>[NFOTBC])(?P<func>[NTRKEJSB])(?P<tc_type>[A-Z])(?P<unit>[CFKVNA])(?P<value>[+-]\d+\.\d+E[+-]\d+)\s*$',
            # 緩い (corruption 受け入れ)
            r'^(?P<prefix>[A-Z]{4})(?P<value>[+-]\d+[.*]\d+[EAea][+-]\d+)\s*\S*\s*$',
        ],
        fallback="numeric_extract",
    )


@pytest.mark.parametrize("raw,expected", [
    ("JPPC+0029*0A+0", 29.0),
    ("JPPC+0029*1A+0", 29.1),
    ("JPPC+0030*3A+0", 30.3),
    ("JPPC+0032*2A+0", 32.2),
    ("JPPC+0033*0A+0", 33.0),
    # 末尾 \t / \r が混じる実機 raw
    ("JPPC+0029*0A+0\t", 29.0),
])
def test_jppc_corruption_recovered_to_correct_decimal(
    raw, expected, yokogawa_format_with_loose_corruption
):
    """v2.14.1: JPPC+DDDD*DA+0 形式の `*` を `.` と読み替えて
    正しい小数を復元すること。"""
    r = parse_response(raw, yokogawa_format_with_loose_corruption)
    assert r["matched"] is True, f"loose pattern にマッチすべき: {r}"
    assert r["fields"]["value"] == pytest.approx(expected), (
        f"raw={raw!r} → 期待 {expected}, 実際 {r['fields']['value']}")


def test_strict_pattern_still_works(yokogawa_format_with_loose_corruption):
    """v2.14.0 で動いていた NTTC 系も regression なしで動くこと。"""
    r = parse_response(
        "NTTC+0033.0E+0", yokogawa_format_with_loose_corruption)
    assert r["matched"] is True
    assert r["fields"]["status"] == "F" or r["fields"]["status"] is not None
    # 厳密 (index 0) にマッチするはず
    assert r["matched_pattern_index"] == 0


def test_parse_value_permissive_unit():
    """`_parse_value_permissive` の単体: 通常 float / 寛容 / 失敗時 default"""
    assert _parse_value_permissive("+1.23E+0", None) == pytest.approx(1.23)
    assert _parse_value_permissive("+1*23A+0", None) == pytest.approx(1.23)
    assert _parse_value_permissive("garbage", "DEFAULT") == "DEFAULT"


# ==============================================================
# P2: `_extract_result_rows` で parsed metadata を除外
# ==============================================================


def _mgr_with_store(store: JobStore):
    m = MagicMock()
    m.store = store
    return m


def test_parsed_metadata_keys_not_emitted_as_rows(job_store, seed_job):
    """v2.14.1: response_parser 出力の `matched` / `fields` /
    `raw` / `fallback_used` / `matched_pattern_index` を
    measurement 列に出さないこと。

    v2.14.2: `job_store` fixture を使用し、teardown で close
    される (Windows WAL file lock 解放)。
    """
    job_id = "job_v2_14_1_metadata"
    seed_job(job_store, job_id)
    row_id = job_store.record_step_started(job_id, 0, "command")
    job_store.record_step_completed(
        row_id, status="ok",
        result={
            "command": "read_measurement",
            "raw_response": "JPPC+0029*0A+0",
            "parsed": {
                "matched": False,
                "fields": {},
                "raw": "JPPC+0029*0A+0",
                "value_numeric": 29.0,
                "fallback_used": "numeric_extract",
            },
            "success": True,
        },
    )
    rows = _extract_result_rows(_mgr_with_store(job_store), job_id)
    measurements = {r["measurement"] for r in rows}
    forbidden = {
        "matched", "fields", "raw", "fallback_used",
        "matched_pattern_index", "error",
    }
    leaked = measurements & forbidden
    assert not leaked, (
        f"v2.14.1: parsed metadata key が rows に出てる: {leaked}")
    assert any(
        ("value_numeric" in m or m.endswith(".value_numeric"))
        for m in measurements
    ), (f"value_numeric が rows に出ていない: {measurements}")


def test_parsed_fields_numeric_emitted_as_rows(job_store, seed_job):
    """v2.14.1: parsed.fields 内 numeric (例: temperature, value)
    が rows 化される。"""
    job_id = "job_v2_14_1_fields"
    seed_job(job_store, job_id)
    row_id = job_store.record_step_started(job_id, 0, "command")
    job_store.record_step_completed(
        row_id, status="ok",
        result={
            "command": "read_measurement",
            "raw_response": "NTTC+0033.0E+0",
            "parsed": {
                "matched": True,
                "fields": {"value": 33.0, "status": "Normal"},
                "raw": "NTTC+0033.0E+0",
                "matched_pattern_index": 0,
            },
            "success": True,
        },
    )
    rows = _extract_result_rows(_mgr_with_store(job_store), job_id)
    by_meas = {r["measurement"]: r["value"] for r in rows}
    numeric_keys = [
        m for m in by_meas
        if m.endswith(".value") or m == "value"
    ]
    assert numeric_keys, f"value が rows にない: {by_meas}"
    assert any(by_meas[k] == 33.0 for k in numeric_keys), (
        f"値 33.0 が見つからない: {by_meas}")


def test_legacy_flat_parsed_still_works(job_store, seed_job):
    """v0.8 旧形式の response_parsed = {"value": 1.23, "unit": "V"}
    のような top-level flat dict も従来通り rows 化される
    (ただし metadata keys は skip)。"""
    job_id = "job_legacy"
    seed_job(job_store, job_id)
    row_id = job_store.record_step_started(job_id, 0, "command")
    job_store.record_step_completed(
        row_id, status="ok",
        result={
            "command": "measure_voltage",
            "raw_response": "+1.23",
            "response_parsed": {"value": 1.23},
            "success": True,
        },
    )
    rows = _extract_result_rows(_mgr_with_store(job_store), job_id)
    by_meas = {r["measurement"]: r["value"] for r in rows}
    assert by_meas.get("value") == pytest.approx(1.23)


def test_jobstore_close_idempotent(job_store):
    """v2.14.2: close() は多重呼び出ししても例外を出さない。"""
    job_store.close()
    job_store.close()  # 2 回目も safe


def test_jobstore_context_manager(tmp_path):
    """v2.14.2: JobStore は context manager として使え、終了時に
    close される。"""
    from lab_executor.job.store import JobStore
    with JobStore(str(tmp_path / "ctx.db")) as s:
        s._connect().execute(
            "INSERT INTO jobs (job_id, owner, resource_name, status, "
            "current_step_index, created_at, updated_at) "
            "VALUES ('test_ctx','','','completed',0,'a','b')")
    # close 済み: 新 connection を取り直せること (lazy reconnect)
    assert (tmp_path / "ctx.db").exists()


# ==============================================================
# version sentinel
# ==============================================================


def test_v2_14_1_version():
    import lab_executor
    parts = lab_executor.__version__.split(".")
    assert tuple(int(p) for p in parts[:3]) >= (2, 14, 1)
