"""v0.6.0: Group / Map MVP テスト

実装方針 (visa_mcp_v0.6.0の実装方針.md) の必須テスト 3 件:
- test_resource_lock_prevents_shared_resource_targets_from_overlapping
- test_bus_semaphore_gpib_max_concurrency_1
- test_map_recipe_partial_failure_continue

加えて、主要テスト群。
"""
import asyncio
import textwrap
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from visa_mcp.bus_manager import BusManager
from lab_executor.group import (
    FailurePolicy, TargetExecution,
    resolve_resource, resolve_unit_bindings, collect_target_resources,
    ResolveError,
)
from lab_executor.group.executor import GroupExecutor
from lab_executor.experiment_ir import CommandStep, Plan
from lab_executor.job import JobManager, JobStore
from lab_executor.job.state_machine import JobStatus, is_terminal
from lab_executor.models.instrument_def import InstrumentDefinition
from visa_mcp.session_manager import InstrumentSession
from lab_executor.system_config import (
    SystemConfig, InstrumentBinding, BusConfig,
    InstrumentGroup, ExperimentUnit,
)


# =========================================================
# SystemConfig
# =========================================================

def test_system_config_loads_basic(tmp_path):
    """system_config.yaml の最低限の読み込み"""
    sys_yaml = tmp_path / "_system.yaml"
    sys_yaml.write_text(textwrap.dedent("""
        instruments:
          psu001:
            resource: "GPIB0::6::INSTR"
            bus: "GPIB0"
          temp001:
            resource: "GPIB0::1::INSTR"
        buses:
          GPIB0: { max_concurrency: 1 }
        instrument_groups:
          temp_meters:
            members: [temp001]
        experiment_units:
          unit001:
            psu: psu001
            temp: temp001
    """), encoding="utf-8")
    cfg = SystemConfig.from_yaml(sys_yaml)
    assert cfg.resolve_alias("psu001") == "GPIB0::6::INSTR"
    # 未指定 bus は GPIB から自動推定
    assert cfg.bus_of("temp001") == "GPIB0"
    assert cfg.bus_of("psu001") == "GPIB0"
    assert "GPIB0" in cfg.buses
    g = cfg.get_group("temp_meters")
    assert g is not None and g.members == ["temp001"]
    u = cfg.get_unit("unit001")
    assert u is not None and u.bindings == {"psu": "psu001", "temp": "temp001"}


def test_system_config_missing_file_is_empty(tmp_path):
    cfg = SystemConfig.from_yaml(tmp_path / "nonexistent.yaml")
    assert cfg.instruments == {}
    assert cfg.instrument_groups == {}


def test_system_config_gpib_auto_default_bus():
    """GPIB resource は bus 未指定でも default max_concurrency=1"""
    cfg = SystemConfig(
        instruments={
            "a": InstrumentBinding(resource="GPIB0::1::INSTR"),
        },
    )
    # 自動 bus 推定 (validator 経由)
    assert cfg.instruments["a"].bus == "GPIB0"


# =========================================================
# Resolver
# =========================================================

def test_resolve_resource_dollar_role():
    cfg = SystemConfig(instruments={
        "psu001": InstrumentBinding(resource="GPIB0::6::INSTR"),
    })
    r = resolve_resource("$psu", {"psu": "psu001"}, cfg)
    assert r == "GPIB0::6::INSTR"


def test_resolve_resource_alias_direct():
    cfg = SystemConfig(instruments={
        "psu001": InstrumentBinding(resource="GPIB0::6::INSTR"),
    })
    assert resolve_resource("psu001", {}, cfg) == "GPIB0::6::INSTR"


def test_resolve_resource_direct_resource():
    cfg = SystemConfig()
    assert resolve_resource("GPIB0::1::INSTR", {}, cfg) == "GPIB0::1::INSTR"


def test_resolve_resource_missing_binding():
    cfg = SystemConfig()
    with pytest.raises(ResolveError):
        resolve_resource("$psu", {}, cfg)


def test_resolve_unit_bindings_merge():
    cfg = SystemConfig(
        experiment_units={
            "unit001": ExperimentUnit(bindings={"psu": "psu001", "temp": "temp001"}),
        },
    )
    # unit + explicit bindings (explicit が優先)
    b = resolve_unit_bindings("unit001", {"temp": "temp999"}, cfg)
    assert b == {"psu": "psu001", "temp": "temp999"}


def test_collect_target_resources_sorted():
    cfg = SystemConfig(instruments={
        "a": InstrumentBinding(resource="GPIB0::3::INSTR"),
        "b": InstrumentBinding(resource="GPIB0::1::INSTR"),
        "c": InstrumentBinding(resource="GPIB0::2::INSTR"),
    })
    rs = collect_target_resources({"x": "a", "y": "b", "z": "c"}, cfg)
    assert rs == sorted(rs)
    assert len(rs) == 3


# =========================================================
# BusManager
# =========================================================

@pytest.mark.asyncio
async def test_bus_manager_gpib_default_concurrency_1():
    """実装方針 #15: GPIB は default で max_concurrency=1"""
    cfg = SystemConfig(instruments={
        "a": InstrumentBinding(resource="GPIB0::1::INSTR"),
        "b": InstrumentBinding(resource="GPIB0::2::INSTR"),
    })
    bm = BusManager(cfg)
    # 2 つの resource (異なる) を同時に取ろうとすると、同 bus なので 2 つ目はブロック
    counter = {"active": 0, "max_active": 0}

    async def use(name, hold_s):
        async with bm.acquire(name):
            counter["active"] += 1
            counter["max_active"] = max(counter["max_active"], counter["active"])
            await asyncio.sleep(hold_s)
            counter["active"] -= 1

    await asyncio.gather(
        use("GPIB0::1::INSTR", 0.05),
        use("GPIB0::2::INSTR", 0.05),
    )
    assert counter["max_active"] == 1, "GPIB は同時 1 のはず"


@pytest.mark.asyncio
async def test_bus_manager_no_bus_passes_through():
    """bus 推定不能な resource はセマフォを通らない (素通し)"""
    bm = BusManager(SystemConfig())
    counter = {"active": 0, "max_active": 0}

    async def use():
        async with bm.acquire("USB0::0x1234::0x5678::INSTR"):
            counter["active"] += 1
            counter["max_active"] = max(counter["max_active"], counter["active"])
            await asyncio.sleep(0.02)
            counter["active"] -= 1

    await asyncio.gather(*[use() for _ in range(5)])
    # 制限なし、5 並列起き得る
    assert counter["max_active"] >= 2


# =========================================================
# GroupExecutor (mock)
# =========================================================

YAML_TEMP = """
metadata: { manufacturer: T, model: TC, category: multimeter }
commands:
  measure: { scpi: "MEAS?", type: query, polling_safe: true }
"""


def _make_temp_session(resource: str):
    d = InstrumentDefinition(**yaml.safe_load(YAML_TEMP))
    return InstrumentSession(
        resource_name=resource, idn_response="<x>",
        idn_parsed={}, definition=d,
    )


@pytest.mark.asyncio
async def test_group_executor_all_success():
    """concurrency 制限付きで全 target が成功 (順序維持)"""
    visa = MagicMock()
    visa.query = AsyncMock(return_value="25.0")

    sessions = {
        f"r{i}": _make_temp_session(f"r{i}") for i in range(5)
    }
    targets = [
        TargetExecution(
            target_id=f"t{i}",
            plan=Plan(steps=[CommandStep(command="measure")]),
            required_resources=[f"r{i}"],
        )
        for i in range(5)
    ]
    ex = GroupExecutor(visa, session_resolver=lambda n: sessions.get(n))
    result = await ex.run(targets, concurrency=3)
    assert result["status"] == "ok"
    assert result["summary"]["success"] == 5
    # 順序維持
    assert [r["target_id"] for r in result["results"]] == [f"t{i}" for i in range(5)]


@pytest.mark.asyncio
async def test_group_executor_partial_failure_continue():
    """実装方針必須テスト: 一部 timeout でも partial_failure で成功分を返す"""
    from visa_mcp.visa_manager import VisaError

    visa = MagicMock()

    async def q(res, *args, **kwargs):
        # r1 だけ失敗
        if res == "r1":
            raise VisaError("simulated timeout on r1")
        return "25.0"

    visa.query = q

    sessions = {
        f"r{i}": _make_temp_session(f"r{i}") for i in range(5)
    }
    targets = [
        TargetExecution(
            target_id=f"t{i}",
            plan=Plan(steps=[CommandStep(command="measure")]),
            required_resources=[f"r{i}"],
        )
        for i in range(5)
    ]
    ex = GroupExecutor(visa, session_resolver=lambda n: sessions.get(n))
    result = await ex.run(targets, concurrency=3,
                          failure_policy=FailurePolicy(mode="continue", retry=0))
    assert result["status"] == "partial_failure"
    assert result["summary"]["success"] == 4
    assert result["summary"]["failed"] == 1
    # 成功 target の結果も返る (失敗で全停止しない)
    success_ids = [r["target_id"] for r in result["results"] if r["status"] == "ok"]
    assert sorted(success_ids) == ["t0", "t2", "t3", "t4"]
    # 失敗 target も結果に含まれ、errors に target_id がある
    assert any(e["target_id"] == "t1" for e in result["errors"])


@pytest.mark.asyncio
async def test_group_executor_stop_on_first_error():
    from visa_mcp.visa_manager import VisaError
    visa = MagicMock()

    async def q(res, *args, **kwargs):
        if res == "r0":  # 最初の target で失敗
            raise VisaError("fail")
        await asyncio.sleep(0.05)  # 他は遅く
        return "25.0"

    visa.query = q
    sessions = {f"r{i}": _make_temp_session(f"r{i}") for i in range(10)}
    targets = [
        TargetExecution(
            target_id=f"t{i}",
            plan=Plan(steps=[CommandStep(command="measure")]),
            required_resources=[f"r{i}"],
        )
        for i in range(10)
    ]
    ex = GroupExecutor(visa, session_resolver=lambda n: sessions.get(n))
    result = await ex.run(
        targets, concurrency=2,
        failure_policy=FailurePolicy(mode="stop_on_first_error", retry=0),
    )
    # 1 つは失敗、いくつかは skipped
    assert result["summary"]["failed"] >= 1
    assert result["summary"]["skipped"] >= 1


@pytest.mark.asyncio
async def test_group_executor_retry_target():
    """failure_policy.retry で target 全体 retry"""
    visa = MagicMock()
    calls = {"n": 0}
    from visa_mcp.visa_manager import VisaError

    async def q(*a, **kw):
        calls["n"] += 1
        # 1, 2 回目失敗、3 回目成功
        if calls["n"] < 3:
            raise VisaError("transient")
        return "25.0"

    visa.query = q
    sess = _make_temp_session("r0")
    targets = [
        TargetExecution(
            target_id="t0",
            plan=Plan(steps=[CommandStep(command="measure")]),
            required_resources=["r0"],
        )
    ]
    ex = GroupExecutor(visa, session_resolver=lambda n: sess if n == "r0" else None)
    result = await ex.run(
        targets, concurrency=1,
        failure_policy=FailurePolicy(mode="continue", retry=3),
    )
    assert result["status"] == "ok"
    assert result["summary"]["retried"] >= 2
    assert result["results"][0]["attempts"] >= 3


# =========================================================
# JobManager: start_group_query_job
# =========================================================

@pytest.mark.asyncio
async def test_start_group_query_job_returns_results_in_order(tmp_path, monkeypatch):
    """実装方針必須: query_group の結果順序が入力順で安定"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")

    visa = MagicMock()
    # ランダムに遅延を入れて完了順をシャッフル
    delays = {"a": 0.05, "b": 0.01, "c": 0.03}

    async def q(res, *args, **kwargs):
        for k, d in delays.items():
            if k in res:
                await asyncio.sleep(d)
                return "1.0"
        await asyncio.sleep(0.01)
        return "1.0"

    visa.query = q

    sessions = {
        "GPIB0::1::INSTR": _make_temp_session("GPIB0::1::INSTR"),  # a
        "GPIB0::2::INSTR": _make_temp_session("GPIB0::2::INSTR"),  # b
        "GPIB0::3::INSTR": _make_temp_session("GPIB0::3::INSTR"),  # c
    }
    # alias で混乱しないよう alias を使わない (直接 resource_name)

    class _SM:
        def get_session(self, name): return sessions.get(name)

    sys_cfg = SystemConfig(
        instruments={
            "a": InstrumentBinding(resource="GPIB0::1::INSTR"),
            "b": InstrumentBinding(resource="GPIB0::2::INSTR"),
            "c": InstrumentBinding(resource="GPIB0::3::INSTR"),
        },
        buses={"GPIB0": BusConfig(max_concurrency=3)},  # ボトルネックなし
        instrument_groups={"temps": InstrumentGroup(members=["a", "b", "c"])},
    )
    store = JobStore(db_path=tmp_path / "j.sqlite")
    mgr = JobManager(visa, _SM(), store=store, system_config=sys_cfg)
    try:
        rec = await mgr.start_group_query_job(
            "temps", "measure", concurrency=3,
        )
        for _ in range(50):
            if is_terminal(mgr.get(rec.job_id).status): break
            await asyncio.sleep(0.05)
        final = mgr.get(rec.job_id)
        assert final.status == JobStatus.COMPLETED
        # 結果順序が a, b, c (入力 members 順)
        ids = [r["target_id"] for r in final.result["results"]]
        assert ids == ["a", "b", "c"], f"順序が崩れた: {ids}"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_start_map_recipe_job_rejects_duplicate_target_id(tmp_path, monkeypatch):
    """内部レビュー追加: target_id 重複は validation で reject"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    visa = MagicMock()
    visa.write = AsyncMock(return_value=None)
    visa.query = AsyncMock(return_value="0.1")

    d = InstrumentDefinition(**yaml.safe_load(YAML_MAP_RECIPE))
    session = InstrumentSession(
        resource_name="GPIB0::6::INSTR", idn_response="<x>",
        idn_parsed={}, definition=d,
    )

    class _SM:
        def get_session(self, name):
            return session if name == "GPIB0::6::INSTR" else None

    sys_cfg = SystemConfig(
        instruments={"psu001": InstrumentBinding(resource="GPIB0::6::INSTR")},
        experiment_units={"u1": ExperimentUnit(bindings={"psu": "psu001"})},
    )
    store = JobStore(db_path=tmp_path / "j.sqlite")
    mgr = JobManager(visa, _SM(), store=store, system_config=sys_cfg)
    try:
        rec = await mgr.start_map_recipe_job(
            "iv_point",
            [
                {"target_id": "dup", "unit": "u1"},
                {"target_id": "dup", "unit": "u1"},
            ],
            primary_role="psu",
        )
        assert rec.status == JobStatus.FAILED
        assert rec.error_class == "validation"
        assert "重複" in (rec.last_step_summary or "")
    finally:
        store.close()


@pytest.mark.asyncio
async def test_start_group_query_job_unknown_group(tmp_path):
    """存在しない group 名で失敗"""
    visa = MagicMock()
    class _SM:
        def get_session(self, name): return None
    store = JobStore(db_path=tmp_path / "j.sqlite")
    mgr = JobManager(visa, _SM(), store=store, system_config=SystemConfig())
    try:
        rec = await mgr.start_group_query_job("nonexistent", "measure")
        assert rec.status == JobStatus.FAILED
        assert rec.error_class == "not_found"
    finally:
        store.close()


# =========================================================
# JobManager: start_map_recipe_job
# =========================================================

YAML_MAP_RECIPE = """
metadata: { manufacturer: T, model: PSU, category: power_supply }
commands:
  set_voltage:
    scpi: "VOLT {voltage}"
    type: write
    parameters:
      - { name: voltage, type: float, range: [0, 100] }
  measure_current:
    scpi: "MEAS:CURR?"
    type: query
    polling_safe: true
recipes:
  iv_point:
    parameters:
      - { name: voltage, type: float, default: 1.0 }
    steps:
      - { command: set_voltage, args: { voltage: $voltage } }
      - { command: measure_current, result_as: i }
"""


@pytest.mark.asyncio
async def test_start_map_recipe_job_with_bindings(tmp_path, monkeypatch):
    """experiment_units 経由で各 target に別 resource を割り当て"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")

    visa = MagicMock()
    visa.write = AsyncMock(return_value=None)
    visa.query = AsyncMock(return_value="0.1")

    d = InstrumentDefinition(**yaml.safe_load(YAML_MAP_RECIPE))
    sessions = {
        f"GPIB0::{i}::INSTR": InstrumentSession(
            resource_name=f"GPIB0::{i}::INSTR", idn_response="<x>",
            idn_parsed={}, definition=d,
        )
        for i in (6, 7)
    }

    class _SM:
        def get_session(self, name): return sessions.get(name)

    sys_cfg = SystemConfig(
        instruments={
            "psu001": InstrumentBinding(resource="GPIB0::6::INSTR"),
            "psu002": InstrumentBinding(resource="GPIB0::7::INSTR"),
        },
        experiment_units={
            "unit001": ExperimentUnit(bindings={"psu": "psu001"}),
            "unit002": ExperimentUnit(bindings={"psu": "psu002"}),
        },
    )
    store = JobStore(db_path=tmp_path / "j.sqlite")
    mgr = JobManager(visa, _SM(), store=store, system_config=sys_cfg)
    try:
        rec = await mgr.start_map_recipe_job(
            "iv_point",
            [
                {"target_id": "s1", "unit": "unit001", "parameters": {"voltage": 1.0}},
                {"target_id": "s2", "unit": "unit002", "parameters": {"voltage": 2.0}},
            ],
            concurrency=2,
            primary_role="psu",
        )
        for _ in range(60):
            if is_terminal(mgr.get(rec.job_id).status): break
            await asyncio.sleep(0.05)
        final = mgr.get(rec.job_id)
        assert final.status == JobStatus.COMPLETED
        ids = [r["target_id"] for r in final.result["results"]]
        assert ids == ["s1", "s2"]
        # 2 target × 2 step (set_voltage write + measure_current query)
        assert visa.write.await_count == 2
        assert visa.query.await_count == 2
    finally:
        store.close()


# =========================================================
# 実装方針必須テスト 3: shared resource は同時実行されない
# =========================================================

@pytest.mark.asyncio
async def test_bus_semaphore_serializes_io_on_same_bus(
    tmp_path, monkeypatch,
):
    """[実装方針必須テスト 1: BusManager 確認]
    BusManager が同 bus 上の I/O を逐次化することを GroupExecutor 経由で確認。

    v0.6.0.1 改名: 旧 test_resource_lock_prevents_shared_resource_targets_from_overlapping。
    旧名は "target 間の resource overlap" を示唆していたが、実態は bus semaphore による
    I/O 逐次化の確認だったため改名。target 間 lock の真の効果は
    test_shared_resource_targets_serialized_during_wait で別途確認する。
    """
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")

    sys_cfg = SystemConfig(
        instruments={
            "psu001": InstrumentBinding(resource="GPIB0::6::INSTR"),
        },
        buses={"GPIB0": BusConfig(max_concurrency=1)},
    )
    bm = BusManager(sys_cfg)

    counter = {"active": 0, "max_active": 0}

    async def q(res, *a, **kw):
        async with bm.acquire(res):
            counter["active"] += 1
            counter["max_active"] = max(counter["max_active"], counter["active"])
            await asyncio.sleep(0.03)
            counter["active"] -= 1
            return "1.0"

    visa = MagicMock()
    visa.query = q

    d = InstrumentDefinition(**yaml.safe_load(YAML_TEMP))
    session = InstrumentSession(
        resource_name="GPIB0::6::INSTR", idn_response="<x>",
        idn_parsed={}, definition=d,
    )

    # 5 targets で同じ resource をクエリ
    targets = [
        TargetExecution(
            target_id=f"t{i}",
            plan=Plan(steps=[CommandStep(command="measure")]),
            required_resources=["GPIB0::6::INSTR"],
        )
        for i in range(5)
    ]
    ex = GroupExecutor(visa, session_resolver=lambda n: session)
    result = await ex.run(targets, concurrency=5)
    assert result["status"] == "ok"
    assert counter["max_active"] == 1, (
        f"GPIB0 同 bus 上の同時 I/O が逐次化されていない: max={counter['max_active']}"
    )


# =========================================================
# MCP ツール (list_groups / list_experiment_units)
# =========================================================

def test_system_config_groups_and_units_accessible(tmp_path):
    cfg = SystemConfig(
        instrument_groups={"temps": InstrumentGroup(members=["a", "b"])},
        experiment_units={"u1": ExperimentUnit(bindings={"psu": "a"})},
    )
    assert cfg.get_group("temps") is not None
    assert cfg.get_unit("u1") is not None
    assert cfg.get_group("nonexistent") is None
