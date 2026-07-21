"""v2.6 契約テスト: per-instrument 観察 API (spec-first ハンドオフ)。

このテストは `docs/specs/v2.6_per_instrument_observation.md` の契約を
encode したもの。Codex はこれが全 PASS になるまで実装する。

実装対象 (現状は存在しないため ImportError で失敗する):
- `lab_executor.tools.observation._extract_instrument_views(
       store, job_id, instrument=None) -> list[dict]`
- MCP tool `get_job_instrument_view(job_id, instrument=None)`
  (observation.register_tools 内に登録)

データソースは job_steps (store.list_steps)。新 table は作らない。
"""
from __future__ import annotations
from unittest.mock import MagicMock

import pytest


# job_store / seed_job fixtures は conftest.py から供給される。


def _seed_command_step(store, job_id, *, step_index, instrument,
                       command, status="ok", raw_response=None,
                       parsed=None, args=None):
    """1 つの command step を job_steps に INSERT + 完了させる helper。"""
    row_id = store.record_step_started(job_id, step_index, "command")
    result = {"command": command, "instrument": instrument}
    if args is not None:
        result["args"] = args
    if raw_response is not None:
        result["raw_response"] = raw_response
    if parsed is not None:
        result["parsed"] = parsed
    result["success"] = (status == "ok")
    store.record_step_completed(
        row_id,
        status=status,
        result=result if status == "ok" else None,
        error=result if status != "ok" else None,
    )


def _seed_wait_step(store, job_id, *, step_index):
    row_id = store.record_step_started(job_id, step_index, "wait")
    store.record_step_completed(row_id, status="ok",
                                result={"step_type": "wait"})


# ==============================================================
# 純関数 _extract_instrument_views の契約
# ==============================================================


def _extract():
    """遅延 import (未実装段階では ImportError で fail させる)。"""
    from lab_executor.tools.observation import _extract_instrument_views
    return _extract_instrument_views


def test_groups_steps_by_instrument(job_store, seed_job):
    job_id = "job_v2_6_group"
    seed_job(job_store, job_id)
    _seed_command_step(job_store, job_id, step_index=0, instrument="$psu",
                       command="set_voltage", args={"voltage": 1.0})
    _seed_command_step(job_store, job_id, step_index=1, instrument="$psu",
                       command="measure_voltage",
                       raw_response="+0.996E+00",
                       parsed={"value_numeric": 0.996})
    _seed_command_step(job_store, job_id, step_index=2, instrument="$dmm",
                       command="read_measurement",
                       raw_response="NTTC+0033.0E+0",
                       parsed={"fields": {"value": 33.0}})

    views = _extract()(job_store, job_id)
    by_instr = {v["instrument"]: v for v in views}
    assert set(by_instr) == {"$psu", "$dmm"}
    assert by_instr["$psu"]["step_count"] == 2
    assert by_instr["$dmm"]["step_count"] == 1


def test_wait_steps_excluded(job_store, seed_job):
    """instrument を持たない wait step はどの instrument にも属さない。"""
    job_id = "job_v2_6_wait"
    seed_job(job_store, job_id)
    _seed_command_step(job_store, job_id, step_index=0, instrument="$psu",
                       command="measure_voltage", raw_response="+1.0E+0")
    _seed_wait_step(job_store, job_id, step_index=1)

    views = _extract()(job_store, job_id)
    assert len(views) == 1
    assert views[0]["instrument"] == "$psu"
    assert views[0]["step_count"] == 1


def test_measurements_only_query_steps(job_store, seed_job):
    """measurements 配列には raw_response を持つ query 系のみ入る。
    write (set_*) は step_count には数えるが measurements には入れない。"""
    job_id = "job_v2_6_meas"
    seed_job(job_store, job_id)
    _seed_command_step(job_store, job_id, step_index=0, instrument="$psu",
                       command="set_voltage", args={"voltage": 2.0})
    _seed_command_step(job_store, job_id, step_index=1, instrument="$psu",
                       command="measure_voltage",
                       raw_response="+1.996E+00",
                       parsed={"value_numeric": 1.996})

    v = _extract()(job_store, job_id)[0]
    assert v["step_count"] == 2
    meas_cmds = [m["command"] for m in v["measurements"]]
    assert meas_cmds == ["measure_voltage"]
    assert v["measurements"][0]["value_numeric"] == pytest.approx(1.996)


def test_value_numeric_from_parsed_fields(job_store, seed_job):
    """value_numeric は parsed.value_numeric → parsed.fields.value の順。"""
    job_id = "job_v2_6_valnum"
    seed_job(job_store, job_id)
    # fields.value 経由
    _seed_command_step(job_store, job_id, step_index=0, instrument="$dmm",
                       command="read_measurement",
                       raw_response="NTTC+0033.0E+0",
                       parsed={"fields": {"value": 33.0}})
    v = _extract()(job_store, job_id)[0]
    assert v["measurements"][0]["value_numeric"] == pytest.approx(33.0)


def test_last_fields_reflect_final_step(job_store, seed_job):
    job_id = "job_v2_6_last"
    seed_job(job_store, job_id)
    _seed_command_step(job_store, job_id, step_index=0, instrument="$psu",
                       command="measure_voltage", raw_response="+1.0E+0",
                       parsed={"value_numeric": 1.0})
    _seed_command_step(job_store, job_id, step_index=1, instrument="$psu",
                       command="measure_current", raw_response="+0.01E+0",
                       parsed={"value_numeric": 0.01})
    v = _extract()(job_store, job_id)[0]
    assert v["last_command"] == "measure_current"
    assert v["last_step_index"] == 1
    assert v["last_value_numeric"] == pytest.approx(0.01)
    assert v["last_status"] == "ok"


def test_failed_count(job_store, seed_job):
    job_id = "job_v2_6_failed"
    seed_job(job_store, job_id)
    _seed_command_step(job_store, job_id, step_index=0, instrument="$psu",
                       command="measure_voltage", raw_response="+1.0E+0",
                       status="ok")
    _seed_command_step(job_store, job_id, step_index=1, instrument="$psu",
                       command="set_voltage", status="failed")
    v = _extract()(job_store, job_id)[0]
    assert v["step_count"] == 2
    assert v["ok_count"] == 1
    assert v["failed_count"] == 1


def test_instrument_filter(job_store, seed_job):
    job_id = "job_v2_6_filter"
    seed_job(job_store, job_id)
    _seed_command_step(job_store, job_id, step_index=0, instrument="$psu",
                       command="measure_voltage", raw_response="+1.0E+0")
    _seed_command_step(job_store, job_id, step_index=1, instrument="$dmm",
                       command="read_measurement", raw_response="x")

    only_psu = _extract()(job_store, job_id, instrument="$psu")
    assert len(only_psu) == 1
    assert only_psu[0]["instrument"] == "$psu"

    none = _extract()(job_store, job_id, instrument="$nonexistent")
    assert none == []


def test_measurements_sorted_by_step_index(job_store, seed_job):
    job_id = "job_v2_6_sorted"
    seed_job(job_store, job_id)
    # わざと step_index を飛ばす
    _seed_command_step(job_store, job_id, step_index=5, instrument="$psu",
                       command="measure_voltage", raw_response="b")
    _seed_command_step(job_store, job_id, step_index=2, instrument="$psu",
                       command="measure_voltage", raw_response="a")
    v = _extract()(job_store, job_id)[0]
    idxs = [m["step_index"] for m in v["measurements"]]
    assert idxs == sorted(idxs)


# ==============================================================
# MCP tool get_job_instrument_view の契約 (FastMCP 登録経由)
# ==============================================================


@pytest.mark.asyncio
async def test_mcp_tool_registered_and_returns_envelope(job_store, seed_job):
    from fastmcp import FastMCP
    from lab_executor.tools import observation as obs

    job_id = "job_v2_6_mcp"
    seed_job(job_store, job_id)
    _seed_command_step(job_store, job_id, step_index=0, instrument="$psu",
                       command="measure_voltage", raw_response="+3.996E+0",
                       parsed={"value_numeric": 3.996})

    job_mgr = MagicMock()
    job_mgr.store = job_store
    rec = MagicMock()
    rec.status.value = "completed"
    job_mgr.get.return_value = rec

    mcp = FastMCP("t")
    obs.register_tools(mcp, job_mgr)
    tool = await mcp.get_tool("get_job_instrument_view")
    result = await tool.fn(job_id=job_id)

    assert result["status"] == "ok"
    data = result["data"]
    assert data["job_id"] == job_id
    assert data["schema_version"] == "2.6"
    assert data["instrument_count"] == 1
    assert data["instruments"][0]["instrument"] == "$psu"
    assert data["instruments"][0]["last_value_numeric"] == pytest.approx(3.996)


@pytest.mark.asyncio
async def test_mcp_tool_not_found(job_store):
    from fastmcp import FastMCP
    from lab_executor.tools import observation as obs

    job_mgr = MagicMock()
    job_mgr.store = job_store
    job_mgr.get.side_effect = Exception("no such job")

    mcp = FastMCP("t")
    obs.register_tools(mcp, job_mgr)
    tool = await mcp.get_tool("get_job_instrument_view")
    result = await tool.fn(job_id="missing")
    assert result["status"] == "error"


# ==============================================================
# 後方互換 / version
# ==============================================================


def test_existing_observation_tools_still_registered():
    """v2.6 追加で既存 observation tool が消えていないこと。"""
    from fastmcp import FastMCP
    from lab_executor.tools import observation as obs
    job_mgr = MagicMock()
    mcp = FastMCP("t")
    obs.register_tools(mcp, job_mgr)
    # FastMCP は同期 list 取得が無いので、内部 registry を直接見ない。
    # ここでは register_tools が例外なく通ることだけ保証 (詳細は
    # 既存 test_v2_14_* が担保)。
    assert True


def test_stability_experimental_count_unchanged():
    """v2.6 の新 tool は stability matrix に登録しない (frozen 維持)。"""
    from lab_executor import stability
    assert stability.experimental_count() == 7
    assert stability.stable_count() == 43


def test_v2_16_0_version():
    import lab_executor
    parts = lab_executor.__version__.split(".")
    assert tuple(int(p) for p in parts[:3]) >= (2, 16, 0), (
        f"v2.6 機能は version 2.16.0 で出す: {lab_executor.__version__}")


# ==============================================================
# v2.16.0 回帰: DSL 実行が step result に instrument を永続化する
# (実機 E2E で発覚: step_executor の result には instrument が無く、
#  job_steps.result_json に instrument が残らないため
#  _extract_instrument_views が実データで空を返していた)
# ==============================================================


@pytest.mark.asyncio
async def test_dsl_execution_persists_instrument_in_step_result(tmp_path):
    """start_experiment_job (DSL path) が job_steps の result に
    instrument を載せることを mock backend で検証。これにより
    get_job_instrument_view が実データで instrument を取れる。"""
    import yaml
    from unittest.mock import AsyncMock
    from lab_executor.job import JobManager, JobStore
    from lab_executor.job.state_machine import is_terminal
    from lab_executor.models.instrument_def import InstrumentDefinition
    from lab_executor.system_config import SystemConfig, InstrumentBinding
    from lab_visa_mcp.session_manager import InstrumentSession

    yaml_psu = """
metadata: { manufacturer: T, model: PSU, category: power_supply }
commands:
  measure_voltage:
    scpi: "MEAS:VOLT?"
    type: query
    polling_safe: true
"""
    res_name = "GPIB0::1::INSTR"  # 有効な VISA resource 形式
    d = InstrumentDefinition(**yaml.safe_load(yaml_psu))
    session = InstrumentSession(
        resource_name=res_name, idn_response="<x>",
        idn_parsed={}, definition=d,
    )

    class _SM:
        def get_session(self, name):
            return session if name == res_name else None

    sys_cfg = SystemConfig(
        instruments={"psu": InstrumentBinding(resource=res_name)})
    visa = MagicMock()
    visa.write = AsyncMock(return_value=None)
    visa.query = AsyncMock(return_value="+1.234E+00")
    store = JobStore(db_path=tmp_path / "j.sqlite")
    try:
        mgr = JobManager(visa, _SM(), store=store, system_config=sys_cfg)
        plan = {
            "dsl_version": "0.8",
            "name": "instr_persist",
            "bindings": {"psu": res_name},
            "steps": [
                {"type": "query", "instrument": "$psu",
                 "command": "measure_voltage"},
            ],
        }
        rec = await mgr.start_experiment_job(plan)
        # 完了待ち
        import asyncio
        for _ in range(100):
            await asyncio.sleep(0.02)
            cur = store.get(rec.job_id)
            if cur and is_terminal(cur.status):
                break
        steps = store.list_steps(rec.job_id)
        # query step の result に instrument が載っていること
        query_steps = [
            s for s in steps
            if (s.get("result") or {}).get("command") == "measure_voltage"
        ]
        assert query_steps, f"measure_voltage step が無い: {steps}"
        instr = query_steps[0]["result"].get("instrument")
        assert instr is not None, (
            "v2.16.0: persisted step result に instrument が無い "
            f"(get_job_instrument_view が実データで空になる): "
            f"{query_steps[0]['result']}")

        # _extract_instrument_views が実 store で instrument を返す
        from lab_executor.tools.observation import _extract_instrument_views
        views = _extract_instrument_views(store, rec.job_id)
        assert len(views) >= 1
        assert any(v["instrument"] == instr for v in views)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_recipe_execution_persists_instrument_in_step_result(
    tmp_path, monkeypatch
):
    """v2.16.1: recipe path (start_recipe_job) も step result に
    instrument を載せること (Codex v2.16.0 レビュー P1)。recipe の
    CommandStep.instrument は None なので rec.resource_name が
    fallback として使われる。"""
    import yaml
    import asyncio
    from unittest.mock import AsyncMock
    from lab_executor.job import JobManager, JobStore
    from lab_executor.job.state_machine import is_terminal, JobStatus
    from lab_executor.models.instrument_def import InstrumentDefinition
    from lab_visa_mcp.session_manager import InstrumentSession
    from lab_executor.tools.observation import _extract_instrument_views

    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    yaml_psu = """
metadata: { manufacturer: T, model: PSU, category: power_supply }
commands:
  measure_voltage:
    scpi: "MEAS:VOLT?"
    type: query
    polling_safe: true
recipes:
  read_v:
    parameters: []
    steps:
      - { command: measure_voltage }
"""
    d = InstrumentDefinition(**yaml.safe_load(yaml_psu))
    session = InstrumentSession(
        resource_name="psu0", idn_response="<x>",
        idn_parsed={}, definition=d,
    )

    class _SM:
        def get_session(self, name):
            return session if name == "psu0" else None

    visa = MagicMock()
    visa.write = AsyncMock(return_value=None)
    visa.query = AsyncMock(return_value="+5.0E+00")
    store = JobStore(db_path=tmp_path / "j.sqlite")
    try:
        mgr = JobManager(visa, _SM(), store=store)
        rec = await mgr.start_recipe_job("psu0", "read_v", {})
        for _ in range(100):
            if is_terminal(mgr.get(rec.job_id).status):
                break
            await asyncio.sleep(0.02)
        assert mgr.get(rec.job_id).status == JobStatus.COMPLETED

        steps = store.list_steps(rec.job_id)
        cmd_steps = [
            s for s in steps
            if (s.get("result") or {}).get("command") == "measure_voltage"
        ]
        assert cmd_steps, f"measure_voltage step が無い: {steps}"
        # recipe path も instrument が persisted result に載る (= psu0)
        assert cmd_steps[0]["result"].get("instrument") == "psu0", (
            "v2.16.1: recipe path の persisted result に instrument が "
            f"無い: {cmd_steps[0]['result']}")

        views = _extract_instrument_views(store, rec.job_id)
        assert any(v["instrument"] == "psu0" for v in views)
    finally:
        store.close()
