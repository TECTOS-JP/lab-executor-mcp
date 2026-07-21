"""v2.9 契約テスト: export 結果の instrument / sweep_index / measurement
フィルタ抽出。

`docs/specs/v2.9_export_result_filters.md` の契約を encode。
Codex はこれが全 PASS になるまで実装する。

実装対象 (現状未実装):
- export._filter_rows() 純ヘルパ
- get_experiment_results に instrument / sweep_index / measurement 引数
- export_experiment_results に同引数
- どちらもフィルタ未指定で従来と完全同一の挙動

教訓 (v2.6/v2.7/v2.8): contract test に実行系 (mock backend で実 DSL
sweep) を含める + 既存挙動 (フィルタ未指定) の回帰ガードを置く。
"""
from __future__ import annotations
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lab_executor.tools import export as exp


def _mgr_with_store(store):
    m = MagicMock()
    m.store = store
    return m


def _rows():
    return [
        {"instrument": "GPIB0::1::INSTR", "measurement": "measure_voltage",
         "sweep_index": 0, "sweep_value": 1.0, "value": "1.0"},
        {"instrument": "GPIB0::1::INSTR", "measurement": "measure_current",
         "sweep_index": 0, "sweep_value": 1.0, "value": "0.1"},
        {"instrument": "GPIB0::2::INSTR", "measurement": "measure_voltage",
         "sweep_index": 1, "sweep_value": 2.0, "value": "2.0"},
        {"instrument": "GPIB0::1::INSTR", "measurement": "measure_voltage",
         "sweep_index": None, "sweep_value": None, "value": "9.9"},
    ]


# ==============================================================
# 純ヘルパ _filter_rows
# ==============================================================


def test_filter_rows_by_instrument():
    out = exp._filter_rows(_rows(), instrument="GPIB0::1::INSTR")
    assert len(out) == 3
    assert all(r["instrument"] == "GPIB0::1::INSTR" for r in out)


def test_filter_rows_by_sweep_index():
    out = exp._filter_rows(_rows(), sweep_index=0)
    assert len(out) == 2
    assert all(r["sweep_index"] == 0 for r in out)


def test_filter_rows_sweep_index_zero_is_valid():
    """sweep_index=0 は有効な絞り込み (None と区別される)。"""
    out = exp._filter_rows(_rows(), sweep_index=0)
    # None 行は除外される
    assert all(r["sweep_index"] is not None for r in out)
    assert len(out) == 2


def test_filter_rows_by_measurement():
    out = exp._filter_rows(_rows(), measurement="measure_voltage")
    assert len(out) == 3
    assert all(r["measurement"] == "measure_voltage" for r in out)


def test_filter_rows_combined_and():
    out = exp._filter_rows(
        _rows(), instrument="GPIB0::1::INSTR",
        measurement="measure_voltage", sweep_index=0)
    assert len(out) == 1
    r = out[0]
    assert r["instrument"] == "GPIB0::1::INSTR"
    assert r["measurement"] == "measure_voltage"
    assert r["sweep_index"] == 0


def test_filter_rows_empty_filters_noop():
    rows = _rows()
    out = exp._filter_rows(rows, instrument="", sweep_index=None,
                           measurement="")
    assert out == rows


# ==============================================================
# MCP: get_experiment_results フィルタ
# ==============================================================


def _seed_two_sweep_points(job_store, seed_job, job_id):
    seed_job(job_store, job_id)
    for idx, (sidx, sval, mv) in enumerate(
            [(0, 1.0, "+1.0E+0"), (1, 2.0, "+2.0E+0")]):
        rid = job_store.record_step_started(job_id, idx, "command")
        job_store.record_step_completed(
            rid, status="ok",
            result={
                "command": "measure_voltage",
                "instrument": "GPIB0::1::INSTR",
                "raw_response": mv,
                "sweep_index": sidx, "sweep_param": "v", "sweep_value": sval,
                "success": True,
            },
        )


@pytest.mark.asyncio
async def test_get_experiment_results_instrument_filter(job_store, seed_job):
    from fastmcp import FastMCP
    job_id = "job_v2_9_instr"
    _seed_two_sweep_points(job_store, seed_job, job_id)
    job_mgr = MagicMock()
    job_mgr.store = job_store
    job_mgr.get.return_value = MagicMock()

    mcp = FastMCP("t")
    exp.register_tools(mcp, job_mgr)
    tool = await mcp.get_tool("get_experiment_results")

    res = await tool.fn(job_id=job_id, instrument="GPIB0::1::INSTR")
    rows = res["data"]["rows"]
    assert rows and all(r["instrument"] == "GPIB0::1::INSTR" for r in rows)
    assert res["data"]["filters"]["instrument"] == "GPIB0::1::INSTR"

    # 存在しない instrument → 0 件
    res2 = await tool.fn(job_id=job_id, instrument="GPIB0::99::INSTR")
    assert res2["data"]["rows"] == []
    assert res2["data"]["pagination"]["total"] == 0


@pytest.mark.asyncio
async def test_get_experiment_results_sweep_index_filter(job_store, seed_job):
    from fastmcp import FastMCP
    job_id = "job_v2_9_sweep"
    _seed_two_sweep_points(job_store, seed_job, job_id)
    job_mgr = MagicMock()
    job_mgr.store = job_store
    job_mgr.get.return_value = MagicMock()

    mcp = FastMCP("t")
    exp.register_tools(mcp, job_mgr)
    tool = await mcp.get_tool("get_experiment_results")

    res = await tool.fn(job_id=job_id, sweep_index=1)
    rows = res["data"]["rows"]
    assert len(rows) == 1
    assert rows[0]["sweep_index"] == 1
    assert rows[0]["sweep_value"] == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_get_experiment_results_no_filter_unchanged(job_store, seed_job):
    """フィルタ未指定で従来と同じ全件 (回帰ガード)。"""
    from fastmcp import FastMCP
    job_id = "job_v2_9_all"
    _seed_two_sweep_points(job_store, seed_job, job_id)
    job_mgr = MagicMock()
    job_mgr.store = job_store
    job_mgr.get.return_value = MagicMock()

    mcp = FastMCP("t")
    exp.register_tools(mcp, job_mgr)
    tool = await mcp.get_tool("get_experiment_results")

    res = await tool.fn(job_id=job_id)
    assert res["data"]["pagination"]["total"] == 2
    assert res["data"]["filters"] == {
        "instrument": None, "sweep_index": None, "measurement": None}


# ==============================================================
# MCP: export_experiment_results フィルタ
# ==============================================================


@pytest.mark.asyncio
async def test_export_experiment_results_filtered_csv(
    job_store, seed_job, tmp_path, monkeypatch
):
    import csv
    from fastmcp import FastMCP
    monkeypatch.setenv("VISA_MCP_EXPORT_DIR", str(tmp_path / "ex"))
    job_id = "job_v2_9_exp"
    _seed_two_sweep_points(job_store, seed_job, job_id)
    job_mgr = MagicMock()
    job_mgr.store = job_store
    job_mgr.get.return_value = MagicMock()

    mcp = FastMCP("t")
    exp.register_tools(mcp, job_mgr)
    tool = await mcp.get_tool("export_experiment_results")

    res = await tool.fn(job_id=job_id, format="csv", sweep_index=1)
    data = res["data"]
    assert data["rows"] == 1
    assert data["filters"]["sweep_index"] == 1
    p = Path(data["path"])
    with open(p, newline="", encoding="utf-8") as f:
        csv_rows = list(csv.DictReader(f))
    assert len(csv_rows) == 1
    assert csv_rows[0]["sweep_index"] == "1"


# ==============================================================
# 実行系 (mock backend で実 DSL sweep → filter)
# ==============================================================


@pytest.mark.asyncio
async def test_dsl_sweep_filter_execution_chain(tmp_path):
    import yaml
    import asyncio
    from unittest.mock import AsyncMock
    from fastmcp import FastMCP
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
            "dsl_version": "0.8", "name": "sweep_filter",
            "bindings": {"psu": res},
            "steps": [
                {"type": "sweep", "parameter": "v",
                 "values": {"values": [1.0, 2.0, 3.0]},
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
        assert store.get(rec.job_id).status == JobStatus.COMPLETED

        mcp = FastMCP("t")
        exp.register_tools(mcp, mgr)
        tool = await mcp.get_tool("get_experiment_results")

        # 全件: measure 3 点
        res_all = await tool.fn(
            job_id=rec.job_id, measurement="measure_voltage")
        assert len(res_all["data"]["rows"]) == 3

        # sweep_index=2 のみ
        res_one = await tool.fn(job_id=rec.job_id, sweep_index=2)
        meas = [r for r in res_one["data"]["rows"]
                if r.get("measurement") == "measure_voltage"]
        assert len(meas) == 1
        assert meas[0]["sweep_index"] == 2
        assert meas[0]["instrument"] == res
    finally:
        store.close()


# ==============================================================
# 後方互換 / version
# ==============================================================


def test_stability_unchanged():
    from lab_executor import stability
    assert stability.experimental_count() == 7
    assert stability.stable_count() == 43


def test_v2_19_0_version():
    import lab_executor
    parts = lab_executor.__version__.split(".")
    assert tuple(int(p) for p in parts[:3]) >= (2, 19, 0), (
        f"v2.9 機能は version 2.19.0 で出す: {lab_executor.__version__}")
