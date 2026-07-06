"""v2.22.x: wait 系ジョブ result_json の日本語文字化け回帰テスト

背景:
  start_wait_job(wait_type="condition") が WaitConditionTimeout で終端した際、
  jobs.result_json 内 steps_executed[0].message の日本語が化ける、という報告があった
  (実機 = 日本語 Windows + NI-VISA)。

このテストは:
  1. condition timeout ジョブをフルパス (JobManager → JobStore/SQLite) で実行し、
     result_json 内 message と last_step_summary が JSON round-trip 後も
     バイト単位で妥当な UTF-8 (= 化けない) ことを検証する。
  2. JobStore の result 直列化が、非UTF-8 由来 (surrogate) を含む message でも
     クラッシュせず・欠落せず round-trip できることを検証する
     (実機 VisaError の CP932/surrogate 混入に対する防御)。
"""
import asyncio
import sqlite3
import textwrap

import pytest
import yaml

from lab_executor.job import JobManager, JobStore
from lab_executor.job.state_machine import JobStatus, is_terminal
from lab_executor.job.store import _dumps_utf8_safe
from lab_executor.models.instrument_def import InstrumentDefinition
from visa_mcp.session_manager import InstrumentSession
from unittest.mock import AsyncMock, MagicMock


def _make_session():
    yaml_str = """
metadata: { manufacturer: T, model: X, category: multimeter }
commands:
  measure: { scpi: "MEAS?", type: "query", polling_safe: true }
"""
    d = InstrumentDefinition(**yaml.safe_load(textwrap.dedent(yaml_str)))
    return InstrumentSession(
        resource_name="TEMP::INSTR", idn_response="<x>",
        idn_parsed={}, definition=d,
    )


@pytest.mark.asyncio
async def test_condition_timeout_japanese_message_roundtrip_utf8(tmp_path, monkeypatch):
    """WaitConditionTimeout の日本語 message が result_json / last_step_summary
    双方で JSON round-trip 後も妥当な UTF-8 のまま (= 化けない) ことを検証。"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    session = _make_session()
    visa = MagicMock()
    visa.query = AsyncMock(return_value="-0.001")  # 条件 (value > 100) は永遠に不達

    class _SM:
        def get_session(self, name):
            return session if name == "TEMP::INSTR" else None

    dbp = tmp_path / "j.sqlite"
    store = JobStore(db_path=dbp)
    mgr = JobManager(visa, _SM(), store=store)
    try:
        rec = await mgr.start_wait_job(
            wait_type="condition",
            params={
                "instrument": "TEMP::INSTR",
                "command": "measure",
                "condition_expr": "value > 100",
                "interval_s": 0.05,
                "timeout_s": 0.3,
            },
        )
        for _ in range(80):
            if is_terminal(mgr.get(rec.job_id).status):
                break
            await asyncio.sleep(0.05)

        final = mgr.get(rec.job_id)
        assert final.status == JobStatus.FAILED
        assert final.error_class == "WaitConditionTimeout"

        # ORM 経由 (json.loads) では両者とも正しい日本語
        result_msg = final.result["steps_executed"][0]["message"]
        assert "を超過" in result_msg
        assert "条件未達成" in result_msg
        assert "を超過" in final.last_step_summary
        # 同一文言なので message の日本語部分は last_step_summary と一致する
        assert final.last_step_summary == result_msg[:80]

        # --- DB の生バイト列を UTF-8 として検証 (化けていないこと) ---
        raw = sqlite3.connect(str(dbp))
        raw.text_factory = bytes
        row = raw.execute(
            "SELECT last_step_summary, result_json FROM jobs WHERE job_id=?",
            (rec.job_id,),
        ).fetchone()
        raw.close()
        lss_bytes, rj_bytes = row[0], row[1]

        # 妥当な UTF-8 であること (壊れていれば UnicodeDecodeError)
        assert lss_bytes.decode("utf-8") == final.last_step_summary
        assert "を超過".encode("utf-8") in rj_bytes
        assert "を超過".encode("utf-8") in lss_bytes
        # 化け signature (UTF-8 lead byte を CP932 誤読した '縺' 等) が
        # 生バイトに存在しないこと
        assert "縺".encode("utf-8") not in rj_bytes
        assert "縺".encode("utf-8") not in lss_bytes
    finally:
        store.close()


def test_dumps_utf8_safe_preserves_plain_japanese():
    """正常な日本語文字列は _dumps_utf8_safe で一切変化しない。"""
    obj = {"message": "timeout_s=25.0 を超過、条件未達成 (last_value=-0.001)"}
    dumped = _dumps_utf8_safe(obj)
    import json
    assert json.loads(dumped)["message"] == obj["message"]
    # 妥当な UTF-8 として encode できる (surrogate 混入なし)
    dumped.encode("utf-8")


def test_dumps_utf8_safe_survives_surrogate_message():
    """実機 VisaError 由来の surrogate 混入 message でも、SQLite TEXT 保存
    (UTF-8 encode) がクラッシュせず round-trip できることを検証。

    通常の json.dumps(ensure_ascii=False) 結果は UTF-8 encode で
    'surrogates not allowed' を投げるが、_dumps_utf8_safe は投げない。
    """
    # CP932 バイトを surrogateescape で抱えた str (孤立サロゲート)
    surro = "タイムアウト".encode("cp932").decode("utf-8", "surrogateescape")
    msg = f"timeout_s=25.0 {surro} を超過"
    obj = {"steps_executed": [{"message": msg}]}

    dumped = _dumps_utf8_safe(obj)
    # SQLite TEXT 保存に相当する UTF-8 encode がクラッシュしないこと
    encoded = dumped.encode("utf-8")

    # 実際に SQLite へ INSERT/SELECT round-trip できること
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t(result_json TEXT)")
    conn.execute("INSERT INTO t VALUES (?)", (dumped,))
    row = conn.execute("SELECT result_json FROM t").fetchone()
    conn.close()
    import json
    parsed = json.loads(row[0])
    # 日本語リテラル部分は保持されている (欠落していない)
    assert "を超過" in parsed["steps_executed"][0]["message"]
    assert "timeout_s=25.0" in parsed["steps_executed"][0]["message"]
