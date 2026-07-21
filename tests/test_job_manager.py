"""JobManager の統合テスト (モック VISA) v0.5.0-rc2"""
import asyncio
import textwrap
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from lab_executor.job import CancelMode, JobManager, JobStore
from lab_executor.job.state_machine import JobStatus
from lab_executor.models.instrument_def import InstrumentDefinition
from lab_visa_mcp.session_manager import InstrumentSession, SessionManager


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
  set_output:
    scpi: "OUTP {state}"
    type: "write"
    parameters:
      - { name: state, type: enum, choices: ["ON", "OFF"] }
recipes:
  quick:
    parameters:
      - { name: v, type: float }
    steps:
      - { command: "reset" }
      - { command: "set_voltage", args: { voltage: "$v" } }
      - { command: "set_output", args: { state: "ON" } }

  with_wait:
    parameters:
      - { name: w, type: float, default: 0.5 }
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

    sess_mgr = _SessMgr()
    store = JobStore(db_path=tmp_path / "test_state.sqlite")
    mgr = JobManager(visa, sess_mgr, store=store)
    yield mgr, visa, sess_mgr, session
    store.close()


@pytest.mark.asyncio
async def test_start_recipe_job_returns_queued(setup):
    mgr, _, _, _ = setup
    rec = await mgr.start_recipe_job("TEST::INSTR", "quick", {"v": 5.0})
    assert rec.status in (JobStatus.QUEUED, JobStatus.RUNNING)
    assert rec.job_id.startswith("job_")
    assert rec.recipe == "quick"


@pytest.mark.asyncio
async def test_job_completes(setup):
    mgr, _, _, _ = setup
    rec = await mgr.start_recipe_job("TEST::INSTR", "quick", {"v": 5.0})
    # 完了まで待つ
    for _ in range(30):
        cur = mgr.get(rec.job_id)
        if cur.status in (JobStatus.COMPLETED, JobStatus.FAILED):
            break
        await asyncio.sleep(0.05)
    final = mgr.get(rec.job_id)
    assert final.status == JobStatus.COMPLETED
    assert final.result is not None
    assert final.result["success"] is True
    assert final.result["step_count"] == 3


@pytest.mark.asyncio
async def test_job_with_wait_step(setup):
    mgr, _, _, _ = setup
    rec = await mgr.start_recipe_job("TEST::INSTR", "with_wait", {"w": 0.3})
    # waiting 状態を観測できることもある (タイミングによる)
    await asyncio.sleep(0.5)
    final = mgr.get(rec.job_id)
    assert final.status == JobStatus.COMPLETED


@pytest.mark.asyncio
async def test_job_recipe_not_found_records_failure(setup):
    mgr, _, _, _ = setup
    rec = await mgr.start_recipe_job("TEST::INSTR", "nonexistent", {})
    assert rec.status == JobStatus.FAILED
    assert rec.error_class == "not_found"


@pytest.mark.asyncio
async def test_job_missing_required_param(setup):
    mgr, _, _, _ = setup
    rec = await mgr.start_recipe_job("TEST::INSTR", "quick", {})
    assert rec.status == JobStatus.FAILED
    assert rec.error_class == "validation"


@pytest.mark.asyncio
async def test_cancel_immediate(setup):
    mgr, _, _, _ = setup
    rec = await mgr.start_recipe_job("TEST::INSTR", "with_wait", {"w": 5.0})
    # 100ms 経過してから cancel
    await asyncio.sleep(0.1)
    final = await mgr.cancel(rec.job_id, CancelMode.IMMEDIATE, timeout_s=5)
    assert final.status in (JobStatus.CANCELLED, JobStatus.COMPLETED)
    # immediate なら cancelled 確率が高い
    if final.status == JobStatus.CANCELLED:
        assert final.error_class == "cancelled"


@pytest.mark.asyncio
async def test_cancel_after_current_step(setup):
    mgr, _, _, _ = setup
    rec = await mgr.start_recipe_job("TEST::INSTR", "with_wait", {"w": 2.0})
    await asyncio.sleep(0.1)
    final = await mgr.cancel(rec.job_id, CancelMode.AFTER_CURRENT_STEP, timeout_s=5)
    # wait 中に cancel が伝播してすぐ停止する
    assert final.status == JobStatus.CANCELLED


@pytest.mark.asyncio
async def test_list_jobs(setup):
    mgr, _, _, _ = setup
    r1 = await mgr.start_recipe_job("TEST::INSTR", "quick", {"v": 1})
    r2 = await mgr.start_recipe_job("TEST::INSTR", "quick", {"v": 2})
    # 完了待ち
    for _ in range(30):
        if mgr.get(r1.job_id).status == JobStatus.COMPLETED and \
           mgr.get(r2.job_id).status == JobStatus.COMPLETED:
            break
        await asyncio.sleep(0.05)
    recs = mgr.list_jobs()
    assert len(recs) >= 2


@pytest.mark.asyncio
async def test_session_not_found_records_failure(setup):
    mgr, _, _, _ = setup
    rec = await mgr.start_recipe_job("NONEXISTENT", "quick", {"v": 1})
    assert rec.status == JobStatus.FAILED
    assert rec.error_class == "not_found"


@pytest.mark.asyncio
async def test_runtimes_cleaned_after_terminal(setup):
    """v0.5.0.1: Job 終端後に self._runtimes から消える (メモリリーク防止)"""
    mgr, _, _, _ = setup
    rec = await mgr.start_recipe_job("TEST::INSTR", "quick", {"v": 5.0})
    assert rec.job_id in mgr._runtimes  # 実行中は存在
    # 完了まで待つ
    for _ in range(40):
        cur = mgr.get(rec.job_id)
        if cur.status == JobStatus.COMPLETED:
            break
        await asyncio.sleep(0.05)
    # 少し追加で待って finally の cleanup を確実に
    await asyncio.sleep(0.1)
    assert rec.job_id not in mgr._runtimes, "終端後も _runtimes に残っている (リーク)"


@pytest.mark.asyncio
async def test_runtimes_cleaned_after_immediate_failure(setup):
    """v0.5.0.1: validation 失敗で即 failed になった Job も _runtimes に登録されない"""
    mgr, _, _, _ = setup
    rec = await mgr.start_recipe_job("TEST::INSTR", "quick", {})  # 必須 v 欠落 → failed
    assert rec.status == JobStatus.FAILED
    # _record_immediate_failure 経路は _runtimes に追加しないので、そもそも入っていない
    assert rec.job_id not in mgr._runtimes
