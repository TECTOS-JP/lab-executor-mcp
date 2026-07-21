"""v0.6.1: Barrier / Stagger テスト

実装方針 (visa_mcp_v0.6.1の実装方針.md) の必須テスト 3 件:
- test_barrier_does_not_hold_target_resource_lock_deadlock
- test_stagger_starts_targets_in_input_order
- test_partial_failure_with_barrier
"""
import asyncio
import textwrap
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from lab_executor.experiment_ir import (
    BarrierStep, CommandStep, Plan, WaitStep,
)
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
# IR validation
# =========================================================


def test_barrier_step_validates_name_nonempty():
    with pytest.raises(Exception):
        BarrierStep(name="")


def test_barrier_step_validates_timeout_positive():
    with pytest.raises(Exception):
        BarrierStep(name="b", timeout_s=0)


def test_command_step_stagger_ms_validation():
    with pytest.raises(Exception):
        CommandStep(command="x", stagger_ms=-1)
    # 上限超過 (10 分 = 600000ms 超)
    with pytest.raises(Exception):
        CommandStep(command="x", stagger_ms=700_000)
    # OK
    s = CommandStep(command="x", stagger_ms=100)
    assert s.stagger_ms == 100


# =========================================================
# BarrierCoordinator unit tests
# =========================================================


@pytest.mark.asyncio
async def test_barrier_coordinator_all_arrive():
    """全 target 到達で event 立ち、両方 success"""
    coord = BarrierCoordinator()
    coord.register_targets(["t1", "t2"])

    async def t(name):
        return await coord.arrive("b1", 0, name, timeout_s=5.0)

    r1, r2 = await asyncio.gather(t("t1"), t("t2"))
    assert r1["success"] is True
    assert r2["success"] is True
    assert r1["total_expected"] == 2
    assert r1["arrived"] == 2


@pytest.mark.asyncio
async def test_barrier_coordinator_timeout():
    """1 target だけ到達、もう 1 target は来ない → timeout"""
    coord = BarrierCoordinator()
    coord.register_targets(["t1", "t2"])
    r = await coord.arrive("b1", 0, "t1", timeout_s=0.2)
    assert r["success"] is False
    assert r["error"] == "timeout"
    assert r.get("interrupted_by_timeout") is True


@pytest.mark.asyncio
async def test_barrier_coordinator_excluded_target():
    """exclude_target で除外したら、残り全到達で event 立つ"""
    coord = BarrierCoordinator()
    coord.register_targets(["t1", "t2", "t3"])
    # t2 は到達せず exclude
    coord.exclude_target("t2")

    async def arrive(tid):
        return await coord.arrive("b1", 0, tid, timeout_s=2.0)

    r1, r3 = await asyncio.gather(arrive("t1"), arrive("t3"))
    assert r1["success"] is True
    assert r3["success"] is True
    assert r1["total_expected"] == 2  # t2 除外


@pytest.mark.asyncio
async def test_barrier_coordinator_cancel():
    coord = BarrierCoordinator()
    coord.register_targets(["t1", "t2"])
    cancel_flag = {"v": False}

    async def trigger():
        await asyncio.sleep(0.1)
        cancel_flag["v"] = True

    asyncio.create_task(trigger())
    r = await coord.arrive(
        "b1", 0, "t1", timeout_s=5.0,
        cancel_check=lambda: "cancel" if cancel_flag["v"] else None,
    )
    assert r["success"] is False
    assert r["error"] == "cancel"


# =========================================================
# 必須テスト 1: barrier 中の lock 解放で deadlock しないこと
# =========================================================


@pytest.mark.asyncio
async def test_barrier_does_not_hold_target_resource_lock_deadlock(monkeypatch):
    """**最重要 (v0.6.1)**

    2 targets が同じ resource を共有し、barrier を含む plan を持つ場合、
    barrier 中に target-level lock が解放されないと deadlock する。

    シナリオ:
      target1: psu001 set_voltage → barrier b → measure
      target2: psu001 set_voltage → barrier b → measure

    target1 が lock を持ったまま barrier 待ちすると、target2 は lock を取れず
    barrier に到達できない → deadlock。本テストはこれが解消されていることを
    確認する (5 秒 timeout で完走)。
    """
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    visa = MagicMock()
    visa.write = AsyncMock(return_value=None)
    visa.query = AsyncMock(return_value="1.0")
    session = _psu_session("psu001")

    def _plan_with_barrier(v: float):
        return Plan(
            steps=[
                CommandStep(command="set_voltage", args={"voltage": v}),
                BarrierStep(name="b", timeout_s=2.0),
                CommandStep(command="measure_voltage"),
            ],
            required_resources=["psu001"],
        )

    targets = [
        TargetExecution(
            target_id=f"t{i}", plan=_plan_with_barrier(1.0 + i),
            required_resources=["psu001"], bindings={},
        )
        for i in range(2)
    ]
    ex = GroupExecutor(visa, session_resolver=lambda n: session if n == "psu001" else None)
    t0 = time.monotonic()
    result = await asyncio.wait_for(ex.run(targets, concurrency=2), timeout=5.0)
    elapsed = time.monotonic() - t0
    assert elapsed < 5.0, f"deadlock 疑い: {elapsed}s かかった"
    assert result["status"] == "ok", result
    # barrier step が両 target で success
    for r in result["results"]:
        barrier_steps = [
            s for s in r["steps_executed"]
            if s.get("step_type") == "barrier"
        ]
        assert len(barrier_steps) == 1
        assert barrier_steps[0]["success"] is True


# =========================================================
# 必須テスト 2: stagger は target 入力順
# =========================================================


@pytest.mark.asyncio
async def test_stagger_starts_targets_in_input_order(monkeypatch):
    """**必須**: stagger_ms を指定した step は target 入力順に遅延起動される。

    asyncio.as_completed() の完了順ではなく、入力 target 順 (target_index)
    で stagger 適用されることを確認。
    """
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    visa = MagicMock()

    # 各 target ごとの set_output 呼び出し時刻を記録
    call_times: list[tuple[str, float]] = []

    sessions = {f"r{i}": _psu_session(f"r{i}") for i in range(5)}

    # write 呼び出し時に target の resource_name を見て時刻記録
    async def w(res, *a, **kw):
        call_times.append((res, time.monotonic()))
        await asyncio.sleep(0.005)
        return None

    visa.write = w
    visa.query = AsyncMock(return_value="1.0")

    plans = []
    for i in range(5):
        plans.append(
            Plan(
                steps=[
                    # stagger_ms=100 を持つ step
                    CommandStep(
                        command="set_output",
                        args={"state": "ON"},
                        stagger_ms=100,
                    ),
                ],
                required_resources=[f"r{i}"],
            )
        )
    targets = [
        TargetExecution(
            target_id=f"t{i}", plan=plans[i],
            required_resources=[f"r{i}"], bindings={},
        )
        for i in range(5)
    ]
    ex = GroupExecutor(visa, session_resolver=lambda n: sessions.get(n))
    t0 = time.monotonic()
    result = await ex.run(targets, concurrency=5)
    assert result["status"] == "ok"
    # call_times を resource 名でソートして、tN 開始時刻 ≒ t0 + 100ms*N を確認
    # 完了順ではなく resource 名 (= target 入力順) で確認
    by_resource = {r: t - t0 for (r, t) in call_times}
    # t0 → r0 ≈ 0、r1 ≈ 0.1、r2 ≈ 0.2、...
    for i in range(5):
        expected = i * 0.1
        actual = by_resource[f"r{i}"]
        assert abs(actual - expected) < 0.05, (
            f"r{i}: expected ≈{expected:.2f}s, actual {actual:.3f}s"
        )


@pytest.mark.asyncio
async def test_stagger_zero_means_no_delay(monkeypatch):
    """stagger_ms=None または 0 のとき遅延なし"""
    visa = MagicMock()
    call_times = []

    async def w(res, *a, **kw):
        call_times.append(time.monotonic())
        return None

    visa.write = w
    session = _psu_session("r0")
    targets = [
        TargetExecution(
            target_id=f"t{i}",
            plan=Plan(steps=[
                CommandStep(command="set_output", args={"state": "ON"}),  # stagger なし
            ]),
            required_resources=[f"r{i}"], bindings={},
        )
        for i in range(3)
    ]
    sessions = {f"r{i}": _psu_session(f"r{i}") for i in range(3)}
    ex = GroupExecutor(visa, session_resolver=lambda n: sessions.get(n))
    t0 = time.monotonic()
    result = await ex.run(targets, concurrency=3)
    assert result["status"] == "ok"
    # 全 call が 50ms 以内に発生 (stagger なしなら同時起動)
    spread = max(call_times) - min(call_times)
    assert spread < 0.1, f"stagger 無しなのに広がった: {spread}s"


# =========================================================
# 必須テスト 3: partial_failure と barrier の連携
# =========================================================


@pytest.mark.asyncio
async def test_partial_failure_with_barrier_continue(monkeypatch):
    """**必須**: failure_policy=continue では失敗 target を barrier 対象から除外。

    3 targets で 1 つ目が set_voltage で失敗、残り 2 つは barrier に到達。
    失敗 target が barrier participants から除外されるので、残り 2 target だけで
    barrier 成立。Job 全体は partial_failure。
    """
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    from lab_visa_mcp.visa_manager import VisaError

    visa = MagicMock()

    async def w(res, *a, **kw):
        if res == "r0":  # 1 つ目だけ失敗
            raise VisaError("simulated write fail")
        return None

    visa.write = w
    visa.query = AsyncMock(return_value="1.0")

    sessions = {f"r{i}": _psu_session(f"r{i}") for i in range(3)}
    plans = [
        Plan(steps=[
            CommandStep(command="set_voltage", args={"voltage": 5}),
            BarrierStep(name="b", timeout_s=2.0),
            CommandStep(command="measure_voltage"),
        ], required_resources=[f"r{i}"])
        for i in range(3)
    ]
    targets = [
        TargetExecution(
            target_id=f"t{i}", plan=plans[i],
            required_resources=[f"r{i}"], bindings={},
        )
        for i in range(3)
    ]
    ex = GroupExecutor(visa, session_resolver=lambda n: sessions.get(n))
    t0 = time.monotonic()
    result = await asyncio.wait_for(
        ex.run(
            targets, concurrency=3,
            failure_policy=FailurePolicy(mode="continue", retry=0),
        ),
        timeout=4.0,
    )
    elapsed = time.monotonic() - t0
    # t0 が失敗しても、残り 2 target が deadlock せず barrier 通過し完走
    assert elapsed < 4.0, f"barrier deadlock: {elapsed}s"
    assert result["status"] == "partial_failure"
    assert result["summary"]["success"] == 2
    assert result["summary"]["failed"] == 1
    # t1, t2 は barrier 成功
    for r in result["results"]:
        if r["target_id"] in ("t1", "t2"):
            assert r["status"] == "ok"


# =========================================================
# その他
# =========================================================


@pytest.mark.asyncio
async def test_barrier_timeout_in_executor(monkeypatch):
    """barrier 待ち中に他 target が到達しないと timeout で failed"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")

    # t1 のみ barrier に到達するが、t2 は set_voltage が異常に遅く到達できない
    visa = MagicMock()
    call_n = {"n": 0}

    async def w(res, *a, **kw):
        if res == "r1":
            await asyncio.sleep(2.0)  # t1 がさっさと barrier に来る間に間に合わない
        return None

    visa.write = w
    visa.query = AsyncMock(return_value="1.0")

    sessions = {"r0": _psu_session("r0"), "r1": _psu_session("r1")}
    plans = [
        Plan(steps=[
            CommandStep(command="set_voltage", args={"voltage": 5}),
            BarrierStep(name="b", timeout_s=0.3),  # 短い timeout
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
    ex = GroupExecutor(visa, session_resolver=lambda n: sessions.get(n))
    result = await asyncio.wait_for(
        ex.run(targets, concurrency=2,
               failure_policy=FailurePolicy(mode="continue", retry=0)),
        timeout=5.0,
    )
    # t0 は barrier timeout で failed、t1 は set_voltage 中
    # 少なくとも 1 つは failed (barrier timeout) で partial_failure or error
    assert result["status"] in ("partial_failure", "error")
    assert result["summary"]["failed"] >= 1


@pytest.mark.asyncio
async def test_stagger_respects_cancel(monkeypatch):
    """stagger 中の cancel に即応"""
    visa = MagicMock()
    visa.write = AsyncMock(return_value=None)
    sessions = {f"r{i}": _psu_session(f"r{i}") for i in range(10)}

    cancel_flag = {"v": False}

    async def trigger():
        await asyncio.sleep(0.15)
        cancel_flag["v"] = True

    plans = [
        Plan(steps=[
            CommandStep(command="set_output", args={"state": "ON"},
                        stagger_ms=500),  # 500ms × 9 targets = 4.5s
        ], required_resources=[f"r{i}"])
        for i in range(10)
    ]
    targets = [
        TargetExecution(
            target_id=f"t{i}", plan=plans[i],
            required_resources=[f"r{i}"], bindings={},
        )
        for i in range(10)
    ]
    ex = GroupExecutor(visa, session_resolver=lambda n: sessions.get(n))
    asyncio.create_task(trigger())
    t0 = time.monotonic()
    result = await asyncio.wait_for(
        ex.run(
            targets, concurrency=10,
            cancel_check=lambda: "cancel" if cancel_flag["v"] else None,
        ),
        timeout=3.0,
    )
    elapsed = time.monotonic() - t0
    # cancel が効けば、4.5s 待つことなく早期終了
    assert elapsed < 2.0, f"stagger 中 cancel が効いていない: {elapsed}s"
    # 一部は cancelled
    assert result["summary"]["cancelled"] + result["summary"]["skipped"] >= 1


@pytest.mark.asyncio
async def test_get_job_status_reports_barrier_progress(monkeypatch):
    """on_progress callback で barrier 進捗が伝わる"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    visa = MagicMock()
    visa.write = AsyncMock(return_value=None)
    visa.query = AsyncMock(return_value="1.0")

    # 2 targets だが 1 つだけ barrier に到達 (もう 1 つは遅い)
    async def w(res, *a, **kw):
        if res == "r1":
            await asyncio.sleep(0.3)
        return None

    visa.write = w
    sessions = {"r0": _psu_session("r0"), "r1": _psu_session("r1")}

    plans = [
        Plan(steps=[
            CommandStep(command="set_voltage", args={"voltage": 5}),
            BarrierStep(name="b", timeout_s=2.0),
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
    progress_log: list[dict] = []
    ex = GroupExecutor(visa, session_resolver=lambda n: sessions.get(n))
    await ex.run(
        targets, concurrency=2,
        on_progress=lambda p: progress_log.append(p),
    )

    # progress のうち 1 件以上に barrier 情報が含まれる
    with_barrier = [p for p in progress_log if "barrier" in p]
    assert len(with_barrier) >= 1, (
        f"barrier progress が公開されていない: progress_log={progress_log}"
    )
    br = with_barrier[0]["barrier"]
    assert br["type"] == "barrier"
    assert br["barrier_name"] == "b"
    assert "arrived" in br
    assert "total_expected" in br


@pytest.mark.asyncio
async def test_execute_recipe_rejects_barrier_step():
    """同期 execute_recipe で barrier step を含む recipe を実行しようとすると
    AsyncStepRequiresJob で reject (start_map_recipe_job 誘導)"""
    YAML_WITH_BARRIER = YAML_PSU + textwrap.dedent("""
        recipes:
          with_barrier:
            parameters: []
            steps:
              - { command: set_voltage, args: { voltage: 5 } }
              - barrier: { name: b1, timeout_s: 10 }
              - { command: measure_voltage }
        """)
    from lab_executor.recipe_executor import execute_recipe
    d = InstrumentDefinition(**yaml.safe_load(YAML_WITH_BARRIER))
    session = InstrumentSession(
        resource_name="r0", idn_response="<x>",
        idn_parsed={}, definition=d,
    )
    visa = MagicMock()
    visa.write = AsyncMock(return_value=None)
    res = await execute_recipe(visa, session, "with_barrier", {})
    assert res["success"] is False
    assert res["error"] == "AsyncStepRequiresJob"
    assert "start_map_recipe_job" in res["message"]
    assert res["recommended_action"]["tool"] == "start_map_recipe_job"
