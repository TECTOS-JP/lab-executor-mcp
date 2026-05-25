"""v0.5.0.4: safe_shutdown 構造化結果 + category fallback 制限テスト"""
import asyncio
import textwrap
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from lab_executor.job import CancelMode, JobManager, JobStore
from lab_executor.job.state_machine import JobStatus, is_terminal
from lab_executor.models.instrument_def import InstrumentDefinition
from visa_mcp.session_manager import InstrumentSession


# === fallback 制限 ===

YAML_POWER_SUPPLY_NO_SHUTDOWN = """
metadata:
  manufacturer: "Test"
  model: "PSU"
  category: "power_supply"
commands:
  reset:
    scpi: "*RST"
    type: "write"
  set_output:
    scpi: "OUTP {state}"
    type: "write"
    parameters:
      - { name: state, type: enum, choices: ["ON", "OFF"] }
  set_voltage:
    scpi: "VOLT {voltage}"
    type: "write"
    parameters:
      - { name: voltage, type: float, range: [0, 100] }
recipes:
  run:
    parameters: []
    steps:
      - { command: "reset" }
      - wait: { seconds: 0.5 }
"""

YAML_NON_POWER_NO_SHUTDOWN = YAML_POWER_SUPPLY_NO_SHUTDOWN.replace(
    'category: "power_supply"', 'category: "multimeter"'
)

YAML_WITH_YAML_SHUTDOWN = """
metadata:
  manufacturer: "Test"
  model: "PSU"
  category: "power_supply"
commands:
  reset:
    scpi: "*RST"
    type: "write"
  set_output:
    scpi: "OUTP {state}"
    type: "write"
    parameters:
      - { name: state, type: enum, choices: ["ON", "OFF"] }
  set_voltage:
    scpi: "VOLT {voltage}"
    type: "write"
    parameters:
      - { name: voltage, type: float, range: [0, 100] }
safe_shutdown:
  - { command: "set_output", args: { state: "OFF" } }
  - wait: { seconds: 0.1 }
  - { command: "set_voltage", args: { voltage: 0 } }
recipes:
  run:
    parameters: []
    steps:
      - { command: "reset" }
      - wait: { seconds: 0.5 }
"""


def _setup_with_yaml(tmp_path, monkeypatch, yaml_str: str):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    visa = MagicMock()
    visa.write = AsyncMock(return_value=None)
    visa.query = AsyncMock(return_value="+1.0")

    d = InstrumentDefinition(**yaml.safe_load(textwrap.dedent(yaml_str)))
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
    return mgr, session, store


# === Tests ===

@pytest.mark.asyncio
async def test_fallback_used_for_power_supply(tmp_path, monkeypatch):
    """power_supply で YAML 未定義なら fallback が走る"""
    mgr, _, store = _setup_with_yaml(tmp_path, monkeypatch, YAML_POWER_SUPPLY_NO_SHUTDOWN)
    try:
        rec = await mgr.start_recipe_job("TEST::INSTR", "run", {})
        await asyncio.sleep(0.05)
        final = await mgr.cancel(rec.job_id, CancelMode.SAFE_SHUTDOWN, timeout_s=5)
        assert final.status == JobStatus.CANCELLED
        info = final.result["safe_shutdown"]
        assert info["attempted"] is True
        assert info["source"] == "fallback_power_supply"
        assert info["success"] is True
        # 2 step (set_output + set_voltage)
        assert len(info["steps"]) == 2
    finally:
        store.close()


@pytest.mark.asyncio
async def test_fallback_disabled_for_non_power_supply(tmp_path, monkeypatch):
    """multimeter 等の非 power_supply カテゴリでは fallback 無効 (skipped)"""
    mgr, _, store = _setup_with_yaml(tmp_path, monkeypatch, YAML_NON_POWER_NO_SHUTDOWN)
    try:
        rec = await mgr.start_recipe_job("TEST::INSTR", "run", {})
        await asyncio.sleep(0.05)
        final = await mgr.cancel(rec.job_id, CancelMode.SAFE_SHUTDOWN, timeout_s=5)
        assert final.status == JobStatus.CANCELLED
        info = final.result["safe_shutdown"]
        assert info["attempted"] is False
        assert info["source"] == "none"
        assert "fallback disabled" in info["skipped_reason"]
        assert info["steps"] == []
    finally:
        store.close()


@pytest.mark.asyncio
async def test_yaml_safe_shutdown_used_when_defined(tmp_path, monkeypatch):
    """YAML safe_shutdown が定義されていればそれが優先"""
    mgr, _, store = _setup_with_yaml(tmp_path, monkeypatch, YAML_WITH_YAML_SHUTDOWN)
    try:
        rec = await mgr.start_recipe_job("TEST::INSTR", "run", {})
        await asyncio.sleep(0.05)
        final = await mgr.cancel(rec.job_id, CancelMode.SAFE_SHUTDOWN, timeout_s=5)
        assert final.status == JobStatus.CANCELLED
        info = final.result["safe_shutdown"]
        assert info["source"] == "yaml"
        assert info["success"] is True
        # 3 step (set_output + wait + set_voltage)
        assert len(info["steps"]) == 3
        # wait step も含まれる
        kinds = [s["kind"] for s in info["steps"]]
        assert "wait" in kinds
    finally:
        store.close()


@pytest.mark.asyncio
async def test_yaml_safe_shutdown_wait_max_capped(tmp_path, monkeypatch):
    """YAML 内の wait は _SAFE_SHUTDOWN_WAIT_MAX_S で上限が効く"""
    long_wait_yaml = YAML_WITH_YAML_SHUTDOWN.replace(
        "wait: { seconds: 0.1 }", "wait: { seconds: 100 }"
    )
    mgr, _, store = _setup_with_yaml(tmp_path, monkeypatch, long_wait_yaml)
    try:
        rec = await mgr.start_recipe_job("TEST::INSTR", "run", {})
        await asyncio.sleep(0.05)
        import time
        t = time.time()
        # 上限 10 秒で打ち切られるはずだが、テストは 20 秒以内に終わる
        final = await mgr.cancel(rec.job_id, CancelMode.SAFE_SHUTDOWN, timeout_s=20)
        elapsed = time.time() - t
        assert final.status == JobStatus.CANCELLED
        assert elapsed < 15, f"safe_shutdown が上限を超えて長引いた: {elapsed}s"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_safe_shutdown_no_session_returns_no_op(tmp_path, monkeypatch):
    """session=None の場合は 'no session' で skip"""
    mgr, _, store = _setup_with_yaml(tmp_path, monkeypatch, YAML_POWER_SUPPLY_NO_SHUTDOWN)
    try:
        info = await mgr._best_effort_safe_shutdown(None)
        assert info["attempted"] is False
        assert info["source"] == "none"
        assert info["skipped_reason"] == "no session"
    finally:
        store.close()


# === scheduling info in MCP response ===

@pytest.mark.asyncio
async def test_scheduling_info_immediate(tmp_path, monkeypatch):
    """1 つ目の Job は immediate_start=True"""
    mgr, _, store = _setup_with_yaml(tmp_path, monkeypatch, YAML_POWER_SUPPLY_NO_SHUTDOWN)
    try:
        rec = await mgr.start_recipe_job("TEST::INSTR", "run", {})
        info = await mgr.scheduler.get_scheduling_info(rec.job_id)
        assert info["immediate_start"] is True
        assert info["blocked_by_job"] is None
        assert info["in_active"] is True
        assert info["in_queue"] is False
    finally:
        store.close()


@pytest.mark.asyncio
async def test_scheduling_info_queued(tmp_path, monkeypatch):
    """2 つ目の Job は immediate_start=False, blocked_by_job 設定"""
    mgr, _, store = _setup_with_yaml(tmp_path, monkeypatch, YAML_POWER_SUPPLY_NO_SHUTDOWN)
    try:
        rec1 = await mgr.start_recipe_job("TEST::INSTR", "run", {})
        rec2 = await mgr.start_recipe_job("TEST::INSTR", "run", {})
        await asyncio.sleep(0.02)
        info = await mgr.scheduler.get_scheduling_info(rec2.job_id)
        assert info["immediate_start"] is False
        assert info["blocked_by_job"] == rec1.job_id
        assert info["in_queue"] is True
    finally:
        # 終端まで待つ (DB lock release)
        for _ in range(40):
            if is_terminal(mgr.get(rec1.job_id).status) and is_terminal(mgr.get(rec2.job_id).status):
                break
            await asyncio.sleep(0.05)
        store.close()
