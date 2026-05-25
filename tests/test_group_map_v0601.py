"""v0.6.0.1: 外部レビュー対応テスト

- P0: 同一 Map Job 内部で同じ resource を共有する target が、wait を含む状況下でも
       同時実行されないことを確認 (GroupExecutor の target-level resource lock)
- P1: bindings 複数時の primary_role 必須化
- P1: start_group_query_job は write 系 command を拒否
"""
import asyncio
import textwrap
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from lab_executor.experiment_ir import CommandStep, Plan, WaitStep
from lab_executor.group import TargetExecution, FailurePolicy
from lab_executor.group.executor import GroupExecutor
from lab_executor.job import JobManager, JobStore
from lab_executor.job.state_machine import JobStatus, is_terminal
from lab_executor.models.instrument_def import InstrumentDefinition
from visa_mcp.session_manager import InstrumentSession
from lab_executor.system_config import (
    SystemConfig, InstrumentBinding, BusConfig,
    InstrumentGroup, ExperimentUnit,
)


YAML_PSU = """
metadata: { manufacturer: T, model: PSU, category: power_supply }
commands:
  set_voltage:
    scpi: "VOLT {voltage}"
    type: write
    parameters:
      - { name: voltage, type: float, range: [0, 100] }
  measure_voltage:
    scpi: "MEAS:VOLT?"
    type: query
    polling_safe: true
"""


def _make_psu_session(resource: str):
    d = InstrumentDefinition(**yaml.safe_load(YAML_PSU))
    return InstrumentSession(
        resource_name=resource, idn_response="<x>",
        idn_parsed={}, definition=d,
    )


# =========================================================
# P0: target-level resource lock の真の効果
# =========================================================


@pytest.mark.asyncio
async def test_shared_resource_targets_serialized_during_wait(monkeypatch):
    """**最重要 (v0.6.0.1 で追加)**

    同一 Map Job 内部で同じ resource を共有する 2 つの target が、
    wait を含むケースでも target 全体として overlap しないことを確認する。

    シナリオ:
      target1: psu001.set_voltage(1V) → wait 0.15s → psu001.measure_voltage
      target2: psu001.set_voltage(2V) → wait 0.15s → psu001.measure_voltage
      concurrency=2 (同時 2 並列で起動可能)

    GroupExecutor の target-level resource lock が無いと、target1 の wait 中に
    target2 が set_voltage で psu001 を上書きしてしまう (条件保持が壊れる)。
    target-level lock があれば、target2 の set_voltage は target1 が完了して
    から始まる。

    判定: target ごとの開始-終了区間が重ならない (start, end) でソートして overlap=0
    """
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")

    visa = MagicMock()
    visa.write = AsyncMock(return_value=None)
    visa.query = AsyncMock(return_value="1.0")

    session = _make_psu_session("psu001")

    # 各 target が実際に "占有していた" 時間区間を計測 (resource 視点)
    intervals: list[tuple[str, float, float]] = []  # (target_id, start, end)

    # GroupExecutor が target lock 取得後に _run_target_once を呼ぶ
    # → step 実行で wait を含む全 step が完了する間の区間を、wait 0.15s で擬似化
    plan1 = Plan(
        steps=[
            CommandStep(command="set_voltage", args={"voltage": 1.0}),
            WaitStep(seconds=0.15),
            CommandStep(command="measure_voltage"),
        ],
        required_resources=["psu001"],
    )
    plan2 = Plan(
        steps=[
            CommandStep(command="set_voltage", args={"voltage": 2.0}),
            WaitStep(seconds=0.15),
            CommandStep(command="measure_voltage"),
        ],
        required_resources=["psu001"],
    )
    targets = [
        TargetExecution(
            target_id="t1", plan=plan1,
            required_resources=["psu001"], bindings={},
        ),
        TargetExecution(
            target_id="t2", plan=plan2,
            required_resources=["psu001"], bindings={},
        ),
    ]

    # GroupExecutor を、_run_target_once を wrap して時間区間を記録するように
    ex = GroupExecutor(visa, session_resolver=lambda n: session if n == "psu001" else None)
    orig_run = ex._run_target_once

    async def _wrapped(target, **kwargs):
        t0 = time.monotonic()
        result = await orig_run(target, **kwargs)
        t1 = time.monotonic()
        intervals.append((target.target_id, t0, t1))
        return result

    ex._run_target_once = _wrapped

    result = await ex.run(targets, concurrency=2)
    assert result["status"] == "ok", result

    # 区間 overlap がないことを確認
    intervals.sort(key=lambda x: x[1])
    for i in range(len(intervals) - 1):
        _, _s_i, e_i = intervals[i]
        _, s_j, _ = intervals[i + 1]
        assert s_j >= e_i - 0.01, (
            f"target {intervals[i][0]} と {intervals[i+1][0]} の実行区間が overlap: "
            f"{intervals}"
        )


@pytest.mark.asyncio
async def test_disjoint_resource_targets_run_in_parallel(monkeypatch):
    """対照テスト: 異なる resource を持つ target は target-level lock で並列可

    target-level lock が canonical sorted で取得されるので、
    異なる resource を持つ target は並列実行される (壊れた lock になっていない確認)。
    """
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    visa = MagicMock()
    visa.write = AsyncMock(return_value=None)
    visa.query = AsyncMock(return_value="1.0")

    sessions = {
        "r1": _make_psu_session("r1"),
        "r2": _make_psu_session("r2"),
    }

    intervals: list[tuple[str, float, float]] = []
    plan_template = Plan(
        steps=[WaitStep(seconds=0.2)],
        required_resources=[],
    )
    targets = [
        TargetExecution(
            target_id="t1",
            plan=Plan(steps=[WaitStep(seconds=0.2)], required_resources=["r1"]),
            required_resources=["r1"], bindings={},
        ),
        TargetExecution(
            target_id="t2",
            plan=Plan(steps=[WaitStep(seconds=0.2)], required_resources=["r2"]),
            required_resources=["r2"], bindings={},
        ),
    ]
    ex = GroupExecutor(visa, session_resolver=lambda n: sessions.get(n))
    orig = ex._run_target_once

    async def w(target, **kw):
        t0 = time.monotonic()
        r = await orig(target, **kw)
        intervals.append((target.target_id, t0, time.monotonic()))
        return r

    ex._run_target_once = w
    t_start = time.monotonic()
    result = await ex.run(targets, concurrency=2)
    total = time.monotonic() - t_start
    assert result["status"] == "ok"
    # 直列なら ~0.4s、並列なら ~0.2s
    assert total < 0.35, (
        f"異なる resource の target が並列実行されていない: total={total:.3f}s"
    )


# =========================================================
# P1: bindings 複数時の primary_role 必須
# =========================================================


@pytest.mark.asyncio
async def test_map_recipe_requires_primary_role_when_multiple_bindings(
    tmp_path, monkeypatch,
):
    """bindings に role が複数あり primary_role 未指定なら validation error"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    visa = MagicMock()
    visa.write = AsyncMock(return_value=None)

    d = InstrumentDefinition(**yaml.safe_load(YAML_PSU))
    sess = InstrumentSession(
        resource_name="GPIB0::6::INSTR", idn_response="<x>",
        idn_parsed={}, definition=d,
    )

    class _SM:
        def get_session(self, name): return sess if name == "GPIB0::6::INSTR" else None

    sys_cfg = SystemConfig(
        instruments={
            "psu001": InstrumentBinding(resource="GPIB0::6::INSTR"),
            "temp001": InstrumentBinding(resource="GPIB0::6::INSTR"),
        },
        experiment_units={
            "u1": ExperimentUnit(bindings={"psu": "psu001", "temp": "temp001"}),
        },
    )
    store = JobStore(db_path=tmp_path / "j.sqlite")
    mgr = JobManager(visa, _SM(), store=store, system_config=sys_cfg)
    try:
        rec = await mgr.start_map_recipe_job(
            "nonexistent_recipe",
            [{"target_id": "s1", "unit": "u1"}],
            # primary_role を指定しない
        )
        assert rec.status == JobStatus.FAILED
        assert rec.error_class == "validation"
        assert "primary_role" in (rec.last_step_summary or "")
    finally:
        store.close()


@pytest.mark.asyncio
async def test_map_recipe_single_binding_auto_primary(tmp_path, monkeypatch):
    """単一 binding の場合は primary_role 自動推定"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    visa = MagicMock()
    visa.write = AsyncMock(return_value=None)
    visa.query = AsyncMock(return_value="1.0")

    YAML_WITH_RECIPE = YAML_PSU + textwrap.dedent("""
        recipes:
          simple:
            parameters: []
            steps:
              - { command: measure_voltage }
        """)
    d = InstrumentDefinition(**yaml.safe_load(YAML_WITH_RECIPE))
    sess = InstrumentSession(
        resource_name="GPIB0::6::INSTR", idn_response="<x>",
        idn_parsed={}, definition=d,
    )

    class _SM:
        def get_session(self, name): return sess if name == "GPIB0::6::INSTR" else None

    sys_cfg = SystemConfig(
        instruments={"psu001": InstrumentBinding(resource="GPIB0::6::INSTR")},
        experiment_units={"u1": ExperimentUnit(bindings={"psu": "psu001"})},
    )
    store = JobStore(db_path=tmp_path / "j.sqlite")
    mgr = JobManager(visa, _SM(), store=store, system_config=sys_cfg)
    try:
        rec = await mgr.start_map_recipe_job(
            "simple",
            [{"target_id": "s1", "unit": "u1"}],
            # primary_role 未指定でも単一 binding なので自動
        )
        # 即座に failed には**ならない** (started)
        assert rec.status != JobStatus.FAILED, rec.last_step_summary
        for _ in range(40):
            if is_terminal(mgr.get(rec.job_id).status): break
            await asyncio.sleep(0.05)
        final = mgr.get(rec.job_id)
        assert final.status == JobStatus.COMPLETED
    finally:
        store.close()


# =========================================================
# P1: start_group_query_job は write 系 command を拒否
# =========================================================


@pytest.mark.asyncio
async def test_start_group_query_job_rejects_write_command(tmp_path, monkeypatch):
    """set_voltage (write) を start_group_query_job で実行すると validation error"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    visa = MagicMock()
    d = InstrumentDefinition(**yaml.safe_load(YAML_PSU))
    sess = InstrumentSession(
        resource_name="GPIB0::6::INSTR", idn_response="<x>",
        idn_parsed={}, definition=d,
    )

    class _SM:
        def get_session(self, name): return sess if name == "GPIB0::6::INSTR" else None

    sys_cfg = SystemConfig(
        instruments={"psu001": InstrumentBinding(resource="GPIB0::6::INSTR")},
        instrument_groups={"psus": InstrumentGroup(members=["psu001"])},
    )
    store = JobStore(db_path=tmp_path / "j.sqlite")
    mgr = JobManager(visa, _SM(), store=store, system_config=sys_cfg)
    try:
        rec = await mgr.start_group_query_job(
            "psus", "set_voltage", args={"voltage": 1.0},
        )
        assert rec.status == JobStatus.FAILED
        assert rec.error_class == "validation"
        assert "query" in (rec.last_step_summary or "").lower()
    finally:
        store.close()


@pytest.mark.asyncio
async def test_start_group_query_job_accepts_query_command(tmp_path, monkeypatch):
    """対照: measure_voltage (query) は通る"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    visa = MagicMock()
    visa.query = AsyncMock(return_value="1.0")
    d = InstrumentDefinition(**yaml.safe_load(YAML_PSU))
    sess = InstrumentSession(
        resource_name="GPIB0::6::INSTR", idn_response="<x>",
        idn_parsed={}, definition=d,
    )

    class _SM:
        def get_session(self, name): return sess if name == "GPIB0::6::INSTR" else None

    sys_cfg = SystemConfig(
        instruments={"psu001": InstrumentBinding(resource="GPIB0::6::INSTR")},
        instrument_groups={"psus": InstrumentGroup(members=["psu001"])},
    )
    store = JobStore(db_path=tmp_path / "j.sqlite")
    mgr = JobManager(visa, _SM(), store=store, system_config=sys_cfg)
    try:
        rec = await mgr.start_group_query_job("psus", "measure_voltage")
        for _ in range(30):
            if is_terminal(mgr.get(rec.job_id).status): break
            await asyncio.sleep(0.05)
        final = mgr.get(rec.job_id)
        assert final.status == JobStatus.COMPLETED
    finally:
        store.close()
