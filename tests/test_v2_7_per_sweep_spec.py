"""v2.7 契約テスト: per-sweep-point 観察 API (spec-first ハンドオフ)。

`docs/specs/v2.7_per_sweep_observation.md` の契約を encode。
Codex はこれが全 PASS になるまで実装する。

実装対象 (現状未実装):
- experiment_ir.CommandStep に sweep_index / sweep_param / sweep_value
- compiler が sweep 展開時にそれをセット
- _run_experiment_plan_job が step result へ永続化
- observation._extract_sweep_views(store, job_id) -> list[dict]
- MCP tool get_job_sweep_view(job_id)

教訓 (v2.6): contract test は捏造 dict だけでなく **実行系
(mock backend で実 DSL sweep)** も通すこと。
"""
from __future__ import annotations
from unittest.mock import MagicMock

import pytest


def _seed_step(store, job_id, *, step_index, instrument, command,
               status="ok", raw_response=None, parsed=None,
               sweep_index=None, sweep_param=None, sweep_value=None):
    row_id = store.record_step_started(job_id, step_index, "command")
    result = {"command": command, "instrument": instrument}
    if raw_response is not None:
        result["raw_response"] = raw_response
    if parsed is not None:
        result["parsed"] = parsed
    if sweep_index is not None:
        result["sweep_index"] = sweep_index
        result["sweep_param"] = sweep_param
        result["sweep_value"] = sweep_value
    result["success"] = (status == "ok")
    store.record_step_completed(
        row_id, status=status,
        result=result if status == "ok" else None,
        error=result if status != "ok" else None,
    )


def _extract():
    from lab_executor.tools.observation import _extract_sweep_views
    return _extract_sweep_views


# ==============================================================
# 純関数 _extract_sweep_views
# ==============================================================


def test_groups_by_sweep_index(job_store, seed_job):
    job_id = "job_v2_7_group"
    seed_job(job_store, job_id)
    # sweep point 0
    _seed_step(job_store, job_id, step_index=1, instrument="$psu",
               command="measure_voltage", raw_response="+1.0E+0",
               parsed={"value_numeric": 1.0},
               sweep_index=0, sweep_param="v", sweep_value=1.0)
    _seed_step(job_store, job_id, step_index=2, instrument="$dmm",
               command="read_measurement", raw_response="NTTC+0028.0E+0",
               parsed={"fields": {"value": 28.0}},
               sweep_index=0, sweep_param="v", sweep_value=1.0)
    # sweep point 1
    _seed_step(job_store, job_id, step_index=4, instrument="$psu",
               command="measure_voltage", raw_response="+2.0E+0",
               parsed={"value_numeric": 2.0},
               sweep_index=1, sweep_param="v", sweep_value=2.0)

    pts = _extract()(job_store, job_id)
    assert len(pts) == 2
    by_idx = {p["sweep_index"]: p for p in pts}
    assert by_idx[0]["sweep_value"] == pytest.approx(1.0)
    assert by_idx[0]["step_count"] == 2
    assert by_idx[1]["sweep_value"] == pytest.approx(2.0)
    assert by_idx[1]["step_count"] == 1


def test_non_sweep_steps_excluded(job_store, seed_job):
    """sweep 文脈の無い step (pre/post) は除外。"""
    job_id = "job_v2_7_excl"
    seed_job(job_store, job_id)
    _seed_step(job_store, job_id, step_index=0, instrument="$psu",
               command="set_output", raw_response=None)  # sweep 外
    _seed_step(job_store, job_id, step_index=1, instrument="$psu",
               command="measure_voltage", raw_response="+1.0E+0",
               sweep_index=0, sweep_param="v", sweep_value=1.0)
    pts = _extract()(job_store, job_id)
    assert len(pts) == 1
    assert pts[0]["sweep_index"] == 0


def test_sweep_value_zero_is_kept(job_store, seed_job):
    """sweep_value=0.0 / sweep_index=0 も「値あり」として保存される。"""
    job_id = "job_v2_7_zero"
    seed_job(job_store, job_id)
    _seed_step(job_store, job_id, step_index=1, instrument="$psu",
               command="measure_voltage", raw_response="+0.0E+0",
               sweep_index=0, sweep_param="v", sweep_value=0.0)
    pts = _extract()(job_store, job_id)
    assert len(pts) == 1
    assert pts[0]["sweep_value"] == 0.0


def test_measurements_only_query_steps(job_store, seed_job):
    """measurements は raw_response を持つ query 系のみ。write は
    step_count に数えるが measurements に入れない。"""
    job_id = "job_v2_7_meas"
    seed_job(job_store, job_id)
    _seed_step(job_store, job_id, step_index=1, instrument="$psu",
               command="set_voltage", raw_response=None,
               sweep_index=0, sweep_param="v", sweep_value=1.0)
    _seed_step(job_store, job_id, step_index=2, instrument="$psu",
               command="measure_voltage", raw_response="+1.0E+0",
               parsed={"value_numeric": 1.0},
               sweep_index=0, sweep_param="v", sweep_value=1.0)
    pt = _extract()(job_store, job_id)[0]
    assert pt["step_count"] == 2
    assert [m["command"] for m in pt["measurements"]] == ["measure_voltage"]
    assert pt["measurements"][0]["value_numeric"] == pytest.approx(1.0)


def test_instruments_unique_in_order(job_store, seed_job):
    job_id = "job_v2_7_instr"
    seed_job(job_store, job_id)
    _seed_step(job_store, job_id, step_index=1, instrument="$psu",
               command="measure_voltage", raw_response="a",
               sweep_index=0, sweep_param="v", sweep_value=1.0)
    _seed_step(job_store, job_id, step_index=2, instrument="$dmm",
               command="read_measurement", raw_response="b",
               sweep_index=0, sweep_param="v", sweep_value=1.0)
    _seed_step(job_store, job_id, step_index=3, instrument="$psu",
               command="measure_current", raw_response="c",
               sweep_index=0, sweep_param="v", sweep_value=1.0)
    pt = _extract()(job_store, job_id)[0]
    assert pt["instruments"] == ["$psu", "$dmm"]


def test_measurements_sorted_by_step_index(job_store, seed_job):
    job_id = "job_v2_7_sort"
    seed_job(job_store, job_id)
    _seed_step(job_store, job_id, step_index=5, instrument="$psu",
               command="measure_voltage", raw_response="b",
               sweep_index=0, sweep_param="v", sweep_value=1.0)
    _seed_step(job_store, job_id, step_index=2, instrument="$psu",
               command="measure_voltage", raw_response="a",
               sweep_index=0, sweep_param="v", sweep_value=1.0)
    pt = _extract()(job_store, job_id)[0]
    idxs = [m["step_index"] for m in pt["measurements"]]
    assert idxs == sorted(idxs)


def test_no_sweep_returns_empty(job_store, seed_job):
    job_id = "job_v2_7_none"
    seed_job(job_store, job_id)
    _seed_step(job_store, job_id, step_index=0, instrument="$psu",
               command="measure_voltage", raw_response="x")  # sweep 文脈なし
    pts = _extract()(job_store, job_id)
    assert pts == []


# ==============================================================
# MCP tool get_job_sweep_view
# ==============================================================


@pytest.mark.asyncio
async def test_mcp_tool_returns_envelope(job_store, seed_job):
    from fastmcp import FastMCP
    from lab_executor.tools import observation as obs

    job_id = "job_v2_7_mcp"
    seed_job(job_store, job_id)
    _seed_step(job_store, job_id, step_index=1, instrument="$psu",
               command="measure_voltage", raw_response="+3.0E+0",
               parsed={"value_numeric": 3.0},
               sweep_index=2, sweep_param="v", sweep_value=3.0)

    job_mgr = MagicMock()
    job_mgr.store = job_store
    rec = MagicMock()
    rec.status.value = "completed"
    job_mgr.get.return_value = rec

    mcp = FastMCP("t")
    obs.register_tools(mcp, job_mgr)
    tool = await mcp.get_tool("get_job_sweep_view")
    result = await tool.fn(job_id=job_id)

    assert result["status"] == "ok"
    data = result["data"]
    assert data["is_sweep_job"] is True
    assert data["sweep_param"] == "v"
    assert data["sweep_point_count"] == 1
    assert data["schema_version"] == "2.7"
    assert data["sweep_points"][0]["sweep_index"] == 2
    assert data["sweep_points"][0]["sweep_value"] == pytest.approx(3.0)


@pytest.mark.asyncio
async def test_mcp_tool_non_sweep_job(job_store, seed_job):
    from fastmcp import FastMCP
    from lab_executor.tools import observation as obs

    job_id = "job_v2_7_nonsweep"
    seed_job(job_store, job_id)
    _seed_step(job_store, job_id, step_index=0, instrument="$psu",
               command="measure_voltage", raw_response="x")

    job_mgr = MagicMock()
    job_mgr.store = job_store
    rec = MagicMock(); rec.status.value = "completed"
    job_mgr.get.return_value = rec

    mcp = FastMCP("t")
    obs.register_tools(mcp, job_mgr)
    tool = await mcp.get_tool("get_job_sweep_view")
    result = await tool.fn(job_id=job_id)
    assert result["status"] == "ok"
    assert result["data"]["is_sweep_job"] is False
    assert result["data"]["sweep_points"] == []


@pytest.mark.asyncio
async def test_mcp_tool_not_found(job_store):
    from fastmcp import FastMCP
    from lab_executor.tools import observation as obs
    job_mgr = MagicMock()
    job_mgr.store = job_store
    job_mgr.get.side_effect = Exception("no job")
    mcp = FastMCP("t")
    obs.register_tools(mcp, job_mgr)
    tool = await mcp.get_tool("get_job_sweep_view")
    result = await tool.fn(job_id="missing")
    assert result["status"] == "error"


# ==============================================================
# 実行系 (mock backend で実 DSL sweep を流し sweep_index 永続化を検証)
# ==============================================================


@pytest.mark.asyncio
async def test_dsl_sweep_persists_sweep_context_in_step_result(tmp_path):
    """実 DSL sweep job を mock backend で実行し、job_steps の result に
    sweep_index / sweep_param / sweep_value が載ることを検証。
    (compiler → executor → 永続化 の chain を通す。捏造データでは
     検出できない箇所。)"""
    import yaml
    import asyncio
    from unittest.mock import AsyncMock
    from lab_executor.job import JobManager, JobStore
    from lab_executor.job.state_machine import is_terminal, JobStatus
    from lab_executor.models.instrument_def import InstrumentDefinition
    from lab_executor.system_config import SystemConfig, InstrumentBinding
    from lab_visa_mcp.session_manager import InstrumentSession

    res = "GPIB0::1::INSTR"
    yaml_psu = """
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
    d = InstrumentDefinition(**yaml.safe_load(yaml_psu))
    session = InstrumentSession(
        resource_name=res, idn_response="<x>", idn_parsed={}, definition=d)

    class _SM:
        def get_session(self, name):
            return session if name == res else None

    sys_cfg = SystemConfig(
        instruments={"psu": InstrumentBinding(resource=res)})
    visa = MagicMock()
    visa.write = AsyncMock(return_value=None)
    visa.query = AsyncMock(return_value="+1.0E+00")
    store = JobStore(db_path=tmp_path / "j.sqlite")
    try:
        mgr = JobManager(visa, _SM(), store=store, system_config=sys_cfg)
        plan = {
            "dsl_version": "0.8",
            "name": "sweep_persist",
            "bindings": {"psu": res},
            "steps": [
                {"type": "sweep", "parameter": "v",
                 "values": {"values": [1.0, 2.0]},
                 "body": [
                     {"type": "command", "instrument": "$psu",
                      "command": "set_voltage", "args": {"voltage": "{v}"}},
                     {"type": "query", "instrument": "$psu",
                      "command": "measure_voltage"},
                 ]},
            ],
        }
        rec = await mgr.start_experiment_job(plan)
        for _ in range(150):
            await asyncio.sleep(0.02)
            cur = store.get(rec.job_id)
            if cur and is_terminal(cur.status):
                break
        assert store.get(rec.job_id).status == JobStatus.COMPLETED, (
            f"job failed: {store.get(rec.job_id).result}")

        steps = store.list_steps(rec.job_id)
        meas = [
            s for s in steps
            if (s.get("result") or {}).get("command") == "measure_voltage"
        ]
        assert len(meas) == 2, f"sweep 2 点の measure step が無い: {steps}"
        # sweep_index が persisted result に載っている
        raw_idxs = [s["result"].get("sweep_index") for s in meas]
        assert all(x is not None for x in raw_idxs), (
            "v2.17.0: persisted result に sweep_index が無い "
            f"(get_job_sweep_view が空になる): {[s['result'] for s in meas]}")
        assert sorted(raw_idxs) == [0, 1]
        # sweep_param / sweep_value も
        assert meas[0]["result"].get("sweep_param") == "v"

        # _extract_sweep_views が 2 point 返す
        from lab_executor.tools.observation import _extract_sweep_views
        pts = _extract_sweep_views(store, rec.job_id)
        assert len(pts) == 2
        assert {p["sweep_index"] for p in pts} == {0, 1}
    finally:
        store.close()


# ==============================================================
# 後方互換 / version
# ==============================================================


def test_stability_unchanged():
    from lab_executor import stability
    assert stability.experimental_count() == 7
    assert stability.stable_count() == 43


def test_v2_17_0_version():
    import lab_executor
    parts = lab_executor.__version__.split(".")
    assert tuple(int(p) for p in parts[:3]) >= (2, 17, 0), (
        f"v2.7 機能は version 2.17.0 で出す: {lab_executor.__version__}")
