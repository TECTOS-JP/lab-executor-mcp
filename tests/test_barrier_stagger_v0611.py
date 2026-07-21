"""v0.6.1.1: 外部レビュー P0 対応テスト + stagger progress

- P0: barrier timeout 後に後続 CommandStep が実行されないこと (visa.write 未呼出)
- P0: abort 済み barrier への late arrival が即失敗で返ること
- P1: stagger 中 progress に next_target_id / next_start_in_s 等が含まれること
"""
import asyncio
import textwrap
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from lab_executor.experiment_ir import BarrierStep, CommandStep, Plan
from lab_executor.group import FailurePolicy, TargetExecution
from lab_executor.group.barrier import BarrierCoordinator
from lab_executor.group.executor import GroupExecutor
from lab_executor.models.instrument_def import InstrumentDefinition
from lab_visa_mcp.session_manager import InstrumentSession


YAML_PSU = """
metadata: { manufacturer: T, model: PSU, category: power_supply }
commands:
  set_voltage:
    scpi: "VOLT {voltage}"
    type: write
    parameters:
      - { name: voltage, type: float, range: [0, 100] }
  set_output:
    scpi: "OUTP {state}"
    type: write
    parameters:
      - { name: state, type: enum, choices: ["ON", "OFF"] }
  measure_voltage:
    scpi: "MEAS:VOLT?"
    type: query
    polling_safe: true
"""


def _psu_session(resource: str):
    d = InstrumentDefinition(**yaml.safe_load(YAML_PSU))
    return InstrumentSession(
        resource_name=resource, idn_response="<x>",
        idn_parsed={}, definition=d,
    )


# =========================================================
# P0-1: barrier timeout 後に後続 step が実行されない
# =========================================================


@pytest.mark.asyncio
async def test_barrier_timeout_prevents_later_steps(monkeypatch):
    """barrier が timeout で abort された target は failed 終了し、
    後続 CommandStep (set_output) が実行されないことを確認"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")

    visa = MagicMock()

    # t0 のみ set_voltage を即完了。t1 は set_voltage で 5 秒掛かるので
    # t0 が barrier 到達 → t1 来ず → barrier timeout で t0 failed
    async def w(res, scpi, *a, **kw):
        if res == "r1" and scpi.startswith("VOLT"):
            await asyncio.sleep(2.0)
            return None
        # それ以外は即 OK
        return None

    visa.write = w
    visa.query = AsyncMock(return_value="1.0")

    sessions = {f"r{i}": _psu_session(f"r{i}") for i in range(2)}

    plans = [
        Plan(steps=[
            CommandStep(command="set_voltage", args={"voltage": 5}),
            BarrierStep(name="b", timeout_s=0.3),   # 短 timeout
            CommandStep(command="set_output", args={"state": "ON"}),  # 後続 step
        ], required_resources=[f"r{i}"])
        for i in range(2)
    ]
    targets = [
        TargetExecution(
            target_id=f"t{i}", plan=plans[i],
            required_resources=[f"r{i}"], bindings={},
        )
        for i in range(2)
    ]

    set_output_calls: list[str] = []
    orig_w = visa.write

    async def track_w(res, scpi, *a, **kw):
        if scpi.startswith("OUTP"):
            set_output_calls.append(res)
        return await orig_w(res, scpi, *a, **kw)

    visa.write = track_w

    ex = GroupExecutor(visa, session_resolver=lambda n: sessions.get(n))
    result = await asyncio.wait_for(
        ex.run(targets, concurrency=2,
               failure_policy=FailurePolicy(mode="continue", retry=0)),
        timeout=5.0,
    )

    # t0 は barrier timeout で failed (後続 set_output 未実行)
    # t1 は set_voltage 中に親が継続するが、いずれにしても set_output に至らない
    assert "r0" not in set_output_calls, (
        f"barrier timeout 後に t0 の後続 set_output が実行された: {set_output_calls}"
    )
    # t0 の status が failed であること
    t0_result = next(r for r in result["results"] if r["target_id"] == "t0")
    assert t0_result["status"] == "failed"
    # steps_executed に barrier step が記録され、success=False で halted
    barrier_step = next(
        s for s in t0_result["steps_executed"] if s.get("step_type") == "barrier"
    )
    assert barrier_step["success"] is False
    # 後続 set_output step は steps_executed に含まれない
    output_steps = [
        s for s in t0_result["steps_executed"]
        if s.get("command") == "set_output"
    ]
    assert output_steps == [], (
        f"t0 の steps_executed に後続 set_output が記録された: {output_steps}"
    )


# =========================================================
# P0-2: abort 済み barrier への late arrival 即失敗
# =========================================================


@pytest.mark.asyncio
async def test_late_arrival_after_barrier_timeout_fails_immediately():
    """timeout で abort 済みの barrier に後から到達した target は
    新たに wait に入らず、即 success=False, late_arrival=True で返る"""
    coord = BarrierCoordinator()
    coord.register_targets(["t1", "t2"])

    # t1 が到達して timeout で abort
    r1 = await coord.arrive("b1", 0, "t1", timeout_s=0.1)
    assert r1["success"] is False
    assert r1["error"] == "timeout"
    assert r1.get("interrupted_by_timeout") is True

    # t2 が late arrival
    t0 = time.monotonic()
    r2 = await coord.arrive("b1", 0, "t2", timeout_s=5.0)
    elapsed = time.monotonic() - t0
    # 即座に return (5 秒待たない)
    assert elapsed < 0.2, f"late arrival が wait に入っている: {elapsed}s"
    assert r2["success"] is False
    assert r2["error"] == "timeout"
    assert r2.get("late_arrival") is True


@pytest.mark.asyncio
async def test_late_arrival_after_barrier_cancel():
    """cancel で abort 後の late arrival も即失敗"""
    coord = BarrierCoordinator()
    coord.register_targets(["t1", "t2"])

    cancel_flag = {"v": False}

    async def trigger():
        await asyncio.sleep(0.05)
        cancel_flag["v"] = True

    asyncio.create_task(trigger())
    r1 = await coord.arrive(
        "b1", 0, "t1", timeout_s=2.0,
        cancel_check=lambda: "cancel" if cancel_flag["v"] else None,
    )
    assert r1["success"] is False
    assert r1["error"] == "cancel"

    # t2 late arrival
    t0 = time.monotonic()
    r2 = await coord.arrive("b1", 0, "t2", timeout_s=5.0)
    elapsed = time.monotonic() - t0
    assert elapsed < 0.2
    assert r2["success"] is False
    assert r2["error"] == "cancel"
    assert r2.get("late_arrival") is True


# =========================================================
# P1: stagger progress
# =========================================================


@pytest.mark.asyncio
async def test_stagger_progress_includes_next_target_id(monkeypatch):
    """on_progress に stagger 中の next_target_id / next_start_in_s が含まれる"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    visa = MagicMock()
    visa.write = AsyncMock(return_value=None)
    visa.query = AsyncMock(return_value="1.0")

    sessions = {f"r{i}": _psu_session(f"r{i}") for i in range(5)}
    plans = [
        Plan(steps=[
            CommandStep(command="set_output", args={"state": "ON"},
                        stagger_ms=200),  # 200ms × i
        ], required_resources=[f"r{i}"])
        for i in range(5)
    ]
    targets = [
        TargetExecution(
            target_id=f"t{i}", plan=plans[i],
            required_resources=[f"r{i}"], bindings={},
        )
        for i in range(5)
    ]

    progress_log: list[dict] = []

    def collect(p):
        # 浅いコピー (executor は同一 dict を更新)
        progress_log.append(dict(p))

    ex = GroupExecutor(visa, session_resolver=lambda n: sessions.get(n))
    await ex.run(targets, concurrency=5, on_progress=collect)

    with_stagger = [p for p in progress_log if "stagger" in p]
    assert len(with_stagger) >= 1, (
        f"stagger progress が公開されていない: {progress_log}"
    )
    sg = with_stagger[0]["stagger"]
    assert sg["command"] == "set_output"
    assert sg["stagger_ms"] == 200
    assert "next_target_id" in sg
    assert "next_start_in_s" in sg
    assert sg["next_target_id"].startswith("t")
    assert sg["in_stagger_count"] >= 1
