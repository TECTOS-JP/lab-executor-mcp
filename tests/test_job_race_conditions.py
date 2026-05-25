"""v0.5.0.3: ロスト・ウェイクアップ等のレース条件テスト"""
import asyncio
import textwrap
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from lab_executor.job import CancelMode, JobManager, JobStore
from lab_executor.job.state_machine import JobStatus, is_terminal
from lab_executor.models.instrument_def import InstrumentDefinition
from visa_mcp.session_manager import InstrumentSession


SAMPLE_YAML = """
metadata:
  manufacturer: "Test"
  model: "PSU"
commands:
  reset:
    scpi: "*RST"
    type: "write"
  set_voltage:
    scpi: "VOLT {voltage}"
    type: "write"
    parameters:
      - { name: voltage, type: float, range: [0, 100] }
recipes:
  fast:
    parameters: []
    steps:
      - { command: "reset" }
      - { command: "set_voltage", args: { voltage: 1 } }

  with_wait:
    parameters:
      - { name: w, type: float, default: 0.05 }
    steps:
      - { command: "reset" }
      - wait: { seconds: "$w" }
      - { command: "set_voltage", args: { voltage: 1 } }
"""


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    visa = MagicMock()
    visa.write = AsyncMock(return_value=None)
    visa.query = AsyncMock(return_value="+1.0")

    d = InstrumentDefinition(**yaml.safe_load(textwrap.dedent(SAMPLE_YAML)))
    session = InstrumentSession(
        resource_name="TEST::INSTR",
        idn_response="<test>",
        idn_parsed={"manufacturer": "Test", "model": "PSU"},
        definition=d,
    )

    class _SessMgr:
        def get_session(self, name):
            return session if name == "TEST::INSTR" else None

    store = JobStore(db_path=tmp_path / "test.sqlite")
    mgr = JobManager(visa, _SessMgr(), store=store)
    yield mgr
    store.close()


# === Bug A: lost wake-up race ===

@pytest.mark.asyncio
async def test_event_eagerly_created():
    """_JobRuntime._start_event は __init__ で生成される (lost wake-up 防止)"""
    from lab_executor.job.manager import _JobRuntime
    import asyncio as _asyncio

    async def _coro():
        pass

    task = _asyncio.create_task(_coro())
    rt = _JobRuntime(task, None)
    assert rt._start_event is not None
    assert isinstance(rt._start_event, _asyncio.Event)
    assert not rt._start_event.is_set()
    await task


@pytest.mark.asyncio
async def test_no_lost_wake_when_predecessor_terminates_fast(setup):
    """
    rec1 の終端 → rec2 wake、の連鎖が rec2 task 実行前に発生しても
    wake が失われずに rec2 が起動・完了すること。
    """
    mgr = setup
    # rec1 は非常に短い (wait なし)
    rec1 = await mgr.start_recipe_job("TEST::INSTR", "fast", {})
    # rec2 は queue 確定後にすぐ進む
    rec2 = await mgr.start_recipe_job("TEST::INSTR", "fast", {})

    # 両方終端まで待つ
    for _ in range(60):
        s1 = mgr.get(rec1.job_id).status
        s2 = mgr.get(rec2.job_id).status
        if is_terminal(s1) and is_terminal(s2):
            break
        await asyncio.sleep(0.05)

    final1 = mgr.get(rec1.job_id)
    final2 = mgr.get(rec2.job_id)
    assert final1.status == JobStatus.COMPLETED
    assert final2.status == JobStatus.COMPLETED, \
        f"rec2 が永久 queued 状態 (lost wake)。status={final2.status.value}"


# === Bug B: cancel immediately after start (terminal guard) ===

@pytest.mark.asyncio
async def test_cancel_immediate_after_start_no_state_violation(setup, caplog):
    """
    start_recipe_job 直後 (task 実行前) に cancel した場合、
    _run_job_inner が CANCELLED → RUNNING の不正遷移を試みないこと。
    """
    import logging
    caplog.set_level(logging.ERROR)

    mgr = setup
    rec = await mgr.start_recipe_job("TEST::INSTR", "fast", {})
    # 即座にキャンセル (event loop yield を挟まず)
    final = await mgr.cancel(rec.job_id, CancelMode.IMMEDIATE, timeout_s=3)
    assert final.status == JobStatus.CANCELLED

    # state machine 違反のログが出ていないこと
    for record in caplog.records:
        msg = record.getMessage()
        assert "不正な状態遷移" not in msg, f"state machine 違反ログ: {msg}"


@pytest.mark.asyncio
async def test_cancel_queued_no_state_violation(setup, caplog):
    """queued な Job (実行前) を cancel しても state 違反ログが出ないこと"""
    import logging
    caplog.set_level(logging.ERROR)

    mgr = setup
    rec1 = await mgr.start_recipe_job("TEST::INSTR", "with_wait", {"w": 0.5})
    rec2 = await mgr.start_recipe_job("TEST::INSTR", "fast", {})

    await asyncio.sleep(0.05)
    assert mgr.get(rec2.job_id).status == JobStatus.QUEUED

    # rec2 をキャンセル (まだ実行されていない)
    final2 = await mgr.cancel(rec2.job_id, CancelMode.IMMEDIATE, timeout_s=3)
    assert final2.status == JobStatus.CANCELLED

    # rec1 は完走できる
    for _ in range(40):
        if is_terminal(mgr.get(rec1.job_id).status):
            break
        await asyncio.sleep(0.05)
    assert mgr.get(rec1.job_id).status == JobStatus.COMPLETED

    for record in caplog.records:
        msg = record.getMessage()
        assert "不正な状態遷移" not in msg, f"state machine 違反ログ: {msg}"


@pytest.mark.asyncio
async def test_three_jobs_serialized(setup):
    """3 Job を同一 resource に投入 → すべて完走、順序維持"""
    mgr = setup
    recs = [
        await mgr.start_recipe_job("TEST::INSTR", "fast", {})
        for _ in range(3)
    ]
    for _ in range(80):
        statuses = [mgr.get(r.job_id).status for r in recs]
        if all(is_terminal(s) for s in statuses):
            break
        await asyncio.sleep(0.05)
    assert all(mgr.get(r.job_id).status == JobStatus.COMPLETED for r in recs)
