"""v0.5.0.2: Job 単位排他 (interleave 防止) の統合テスト"""
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
  long:
    parameters:
      - { name: v, type: float }
    steps:
      - { command: "set_voltage", args: { voltage: "$v" } }
      - wait: { seconds: 0.3 }
      - { command: "set_voltage", args: { voltage: "$v" } }

  short:
    parameters: []
    steps:
      - { command: "reset" }
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


@pytest.mark.asyncio
async def test_second_job_queued_when_resource_busy(setup):
    """同一 resource への 2 つ目の Job は queued になる"""
    mgr = setup
    rec1 = await mgr.start_recipe_job("TEST::INSTR", "long", {"v": 5.0})
    rec2 = await mgr.start_recipe_job("TEST::INSTR", "long", {"v": 10.0})

    # rec1 は running/waiting、rec2 は queued のはず
    assert rec1.status in (JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.WAITING)
    # rec2 は queued
    await asyncio.sleep(0.05)
    cur2 = mgr.get(rec2.job_id)
    assert cur2.status == JobStatus.QUEUED

    # 両方終端まで待つ
    for _ in range(80):
        s1 = mgr.get(rec1.job_id).status
        s2 = mgr.get(rec2.job_id).status
        if is_terminal(s1) and is_terminal(s2):
            break
        await asyncio.sleep(0.1)

    final1 = mgr.get(rec1.job_id)
    final2 = mgr.get(rec2.job_id)
    assert final1.status == JobStatus.COMPLETED
    assert final2.status == JobStatus.COMPLETED


@pytest.mark.asyncio
async def test_reject_if_busy_returns_failed(setup):
    """queue_policy='reject_if_busy' なら busy 時に failed"""
    mgr = setup
    rec1 = await mgr.start_recipe_job("TEST::INSTR", "long", {"v": 5.0})
    await asyncio.sleep(0.05)
    rec2 = await mgr.start_recipe_job(
        "TEST::INSTR", "long", {"v": 10.0},
        queue_policy="reject_if_busy",
    )
    assert rec2.status == JobStatus.FAILED
    assert rec2.error_class == "blocked"

    # rec1 は完走
    for _ in range(40):
        if mgr.get(rec1.job_id).status == JobStatus.COMPLETED:
            break
        await asyncio.sleep(0.1)
    assert mgr.get(rec1.job_id).status == JobStatus.COMPLETED


@pytest.mark.asyncio
async def test_different_resources_run_in_parallel(setup, monkeypatch):
    """異なる resource は並列実行される"""
    mgr = setup
    # 2 つ目の resource を mock
    orig_session = mgr._sessions.get_session("TEST::INSTR")
    sess2 = InstrumentSession(
        resource_name="TEST::INSTR2",
        idn_response="<test>",
        idn_parsed={"manufacturer": "Test", "model": "PSU"},
        definition=orig_session.definition,
    )

    class _SessMgr2:
        def get_session(self, name):
            if name == "TEST::INSTR":
                return orig_session
            if name == "TEST::INSTR2":
                return sess2
            return None

    mgr._sessions = _SessMgr2()

    rec1 = await mgr.start_recipe_job("TEST::INSTR", "long", {"v": 5.0})
    rec2 = await mgr.start_recipe_job("TEST::INSTR2", "long", {"v": 5.0})

    # 両方 running 可能 (両方とも queued でない)
    await asyncio.sleep(0.05)
    s1 = mgr.get(rec1.job_id).status
    s2 = mgr.get(rec2.job_id).status
    assert s1 in (JobStatus.RUNNING, JobStatus.WAITING)
    assert s2 in (JobStatus.RUNNING, JobStatus.WAITING)


@pytest.mark.asyncio
async def test_cancel_queued_job(setup):
    """queued な Job をキャンセルすると cancelled になる"""
    mgr = setup
    rec1 = await mgr.start_recipe_job("TEST::INSTR", "long", {"v": 5.0})
    rec2 = await mgr.start_recipe_job("TEST::INSTR", "long", {"v": 10.0})

    await asyncio.sleep(0.05)
    assert mgr.get(rec2.job_id).status == JobStatus.QUEUED

    # rec2 をキャンセル
    final2 = await mgr.cancel(rec2.job_id, CancelMode.IMMEDIATE, timeout_s=3)
    assert final2.status == JobStatus.CANCELLED

    # rec1 は完走できる
    for _ in range(40):
        if mgr.get(rec1.job_id).status == JobStatus.COMPLETED:
            break
        await asyncio.sleep(0.1)
    assert mgr.get(rec1.job_id).status == JobStatus.COMPLETED


@pytest.mark.asyncio
async def test_queue_info_in_get_queue_info(setup):
    mgr = setup
    rec1 = await mgr.start_recipe_job("TEST::INSTR", "long", {"v": 5.0})
    rec2 = await mgr.start_recipe_job("TEST::INSTR", "long", {"v": 10.0})

    await asyncio.sleep(0.05)
    info = await mgr.scheduler.get_queue_info(rec2.job_id)
    assert info is not None
    assert info["blocking_job_id"] == rec1.job_id
    assert info["resources"] == ["TEST::INSTR"]


@pytest.mark.asyncio
async def test_queued_to_interrupted_on_restart(tmp_path, monkeypatch):
    """v0.5.0.2: queued も再起動時に interrupted へ"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    db_path = tmp_path / "test_restart.sqlite"
    store = JobStore(db_path=db_path)

    # queued の Job を 1 件作成 (実行しない)
    store.create_job(
        job_id="job_q1",
        owner="",
        resource_name="TEST::INSTR",
        recipe="long",
        parameters={"v": 5},
    )
    # status は queued のままで残る
    assert store.get("job_q1").status == JobStatus.QUEUED

    # 「再起動」を模す
    n = store.mark_interrupted_on_startup()
    assert n >= 1
    assert store.get("job_q1").status == JobStatus.INTERRUPTED
    store.close()
