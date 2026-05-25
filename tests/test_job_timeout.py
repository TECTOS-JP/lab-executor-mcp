"""Job timeout (job_timeout_s) のテスト (v0.5.0)"""
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
  long_wait:
    parameters: []
    steps:
      - wait: { seconds: 10 }   # 長い wait
      - { command: "set_voltage", args: { voltage: 1 } }

  quick:
    parameters: []
    steps:
      - { command: "reset" }
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

    store = JobStore(db_path=tmp_path / "test_state.sqlite")
    mgr = JobManager(visa, _SessMgr(), store=store)
    yield mgr
    store.close()


@pytest.mark.asyncio
async def test_job_timeout_during_wait(setup):
    """wait 中に job_timeout_s 経過すると TIMEOUT 状態へ"""
    mgr = setup
    rec = await mgr.start_recipe_job(
        "TEST::INSTR", "long_wait", None,
        job_timeout_s=0.5,  # 0.5秒で timeout
    )
    # wait 10s が始まるが、0.5s で timeout すべき
    await asyncio.sleep(1.0)
    final = mgr.get(rec.job_id)
    assert final.status == JobStatus.TIMEOUT
    assert final.error_class == "timeout"
    assert "timed_out_at_step" in (final.result or {})


@pytest.mark.asyncio
async def test_job_timeout_at_wait_boundary(setup):
    """wait 開始直前にも timeout チェック (ごく短い timeout)"""
    mgr = setup
    rec = await mgr.start_recipe_job(
        "TEST::INSTR", "long_wait", None,
        job_timeout_s=0.001,  # wait 10s より遥かに短い
    )
    await asyncio.sleep(0.5)
    final = mgr.get(rec.job_id)
    assert final.status == JobStatus.TIMEOUT
    assert final.error_class == "timeout"


@pytest.mark.asyncio
async def test_no_timeout_for_fast_job(setup):
    """十分な job_timeout_s なら通常完了"""
    mgr = setup
    rec = await mgr.start_recipe_job(
        "TEST::INSTR", "quick", None,
        job_timeout_s=60.0,
    )
    # 完了待ち
    for _ in range(40):
        cur = mgr.get(rec.job_id)
        if is_terminal(cur.status):
            break
        await asyncio.sleep(0.05)
    final = mgr.get(rec.job_id)
    assert final.status == JobStatus.COMPLETED


@pytest.mark.asyncio
async def test_default_timeout_used_when_unset(setup):
    """job_timeout_s 未指定なら DEFAULT_JOB_TIMEOUT_S (24h) を使う"""
    mgr = setup
    rec = await mgr.start_recipe_job("TEST::INSTR", "quick", None)
    for _ in range(40):
        if is_terminal(mgr.get(rec.job_id).status):
            break
        await asyncio.sleep(0.05)
    assert mgr.get(rec.job_id).status == JobStatus.COMPLETED
