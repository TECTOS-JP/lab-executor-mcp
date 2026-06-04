"""v2.8 契約テスト: export dir の env 上書き + sweep/instrument 列。

`docs/specs/v2.8_export_dir_and_sweep_columns.md` の契約を encode。
Codex はこれが全 PASS になるまで実装する。

実装対象 (現状未実装):
- export._resolve_export_dir() (VISA_MCP_EXPORT_DIR 対応)
- _safe_export_path の mkdir 失敗を structured error 化
- RESULT_COLUMNS に sweep_index / sweep_value 追加
- _extract_result_rows が各 row に sweep_index / sweep_value を載せる

教訓 (v2.6/v2.7): contract test に実行系 (mock backend で実 DSL sweep)
を含める。
"""
from __future__ import annotations
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lab_executor.tools import export as exp


def _mgr_with_store(store):
    m = MagicMock()
    m.store = store
    return m


# ==============================================================
# 課題 A: export dir の env 上書き
# ==============================================================


def test_resolve_export_dir_env_override(monkeypatch, tmp_path):
    custom = tmp_path / "myexports"
    monkeypatch.setenv("VISA_MCP_EXPORT_DIR", str(custom))
    assert exp._resolve_export_dir() == custom


def test_resolve_export_dir_default(monkeypatch):
    monkeypatch.delenv("VISA_MCP_EXPORT_DIR", raising=False)
    p = exp._resolve_export_dir()
    assert p.name == "exports"
    assert ".visa-mcp" in p.parts


def test_resolve_export_dir_respects_default_constant_monkeypatch(
    monkeypatch, tmp_path
):
    """既存テストは DEFAULT_EXPORT_DIR を monkeypatch する。env 未設定時は
    その定数を尊重すること (v2.8 回帰防止)。"""
    monkeypatch.delenv("VISA_MCP_EXPORT_DIR", raising=False)
    monkeypatch.setattr(exp, "DEFAULT_EXPORT_DIR", tmp_path / "exports")
    assert exp._resolve_export_dir() == tmp_path / "exports"


def test_export_dir_used_by_safe_export_path(monkeypatch, tmp_path):
    """VISA_MCP_EXPORT_DIR を設定すると、default 出力先がその配下になる。"""
    custom = tmp_path / "ex"
    monkeypatch.setenv("VISA_MCP_EXPORT_DIR", str(custom))
    path, err = exp._safe_export_path(
        None, default_filename="out.csv", overwrite=True)
    assert err is None
    assert path is not None
    # 解決した path が custom 配下
    assert str(path).startswith(str(custom.resolve()))


def test_safe_export_path_mkdir_failure_returns_error(monkeypatch, tmp_path):
    """export dir の mkdir が失敗したら raise せず structured error。"""
    monkeypatch.setenv("VISA_MCP_EXPORT_DIR", str(tmp_path / "x"))

    def _boom(*a, **k):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "mkdir", _boom)
    path, err = exp._safe_export_path(
        None, default_filename="out.csv", overwrite=True)
    assert path is None
    assert err is not None
    assert err["error_class"] == "export_dir_not_writable"


# ==============================================================
# 課題 B: sweep_index / sweep_value 列
# ==============================================================


def test_result_columns_include_sweep():
    assert "sweep_index" in exp.RESULT_COLUMNS
    assert "sweep_value" in exp.RESULT_COLUMNS
    # 既存列の順序・存在は不変
    assert exp.RESULT_COLUMNS[:8] == (
        "timestamp", "target_id", "instrument", "measurement",
        "value", "unit", "step_index", "step_path")


def test_extract_result_rows_carries_sweep_and_instrument(job_store, seed_job):
    job_id = "job_v2_8_cols"
    seed_job(job_store, job_id)
    row_id = job_store.record_step_started(job_id, 7, "command")
    job_store.record_step_completed(
        row_id, status="ok",
        result={
            "command": "measure_voltage",
            "instrument": "GPIB0::1::INSTR",
            "raw_response": "+1.0E+0",
            "sweep_index": 2,
            "sweep_param": "v",
            "sweep_value": 3.0,
            "success": True,
        },
    )
    rows = exp._extract_result_rows(_mgr_with_store(job_store), job_id)
    assert rows, "row が抽出されていない"
    r = rows[0]
    assert r["instrument"] == "GPIB0::1::INSTR"
    assert r["sweep_index"] == 2
    assert r["sweep_value"] == pytest.approx(3.0)


def test_extract_result_rows_sweep_none_when_absent(job_store, seed_job):
    """sweep 文脈の無い step の row は sweep_index/value が None。"""
    job_id = "job_v2_8_nonsweep"
    seed_job(job_store, job_id)
    row_id = job_store.record_step_started(job_id, 0, "command")
    job_store.record_step_completed(
        row_id, status="ok",
        result={
            "command": "measure_voltage",
            "instrument": "GPIB0::1::INSTR",
            "raw_response": "+1.0E+0",
            "success": True,
        },
    )
    rows = exp._extract_result_rows(_mgr_with_store(job_store), job_id)
    assert rows
    assert rows[0]["sweep_index"] is None
    assert rows[0]["sweep_value"] is None


# ==============================================================
# MCP tool: get_experiment_results が新列を含む
# ==============================================================


@pytest.mark.asyncio
async def test_get_experiment_results_columns_include_sweep(
    job_store, seed_job
):
    from fastmcp import FastMCP

    job_id = "job_v2_8_mcp"
    seed_job(job_store, job_id)
    row_id = job_store.record_step_started(job_id, 1, "command")
    job_store.record_step_completed(
        row_id, status="ok",
        result={
            "command": "measure_voltage",
            "instrument": "GPIB0::1::INSTR",
            "raw_response": "+2.0E+0",
            "sweep_index": 1, "sweep_value": 2.0, "success": True,
        },
    )
    job_mgr = MagicMock()
    job_mgr.store = job_store
    job_mgr.get.return_value = MagicMock()

    mcp = FastMCP("t")
    exp.register_tools(mcp, job_mgr)
    tool = await mcp.get_tool("get_experiment_results")
    result = await tool.fn(job_id=job_id)
    cols = result["data"]["columns"]
    assert "sweep_index" in cols and "sweep_value" in cols


# ==============================================================
# 実行系 (mock backend で実 DSL sweep → export row に sweep_index)
# ==============================================================


@pytest.mark.asyncio
async def test_dsl_sweep_export_rows_carry_sweep_index(tmp_path):
    import yaml
    import asyncio
    from unittest.mock import AsyncMock
    from lab_executor.job import JobManager, JobStore
    from lab_executor.job.state_machine import is_terminal, JobStatus
    from lab_executor.models.instrument_def import InstrumentDefinition
    from lab_executor.system_config import SystemConfig, InstrumentBinding
    from visa_mcp.session_manager import InstrumentSession

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
            "dsl_version": "0.8", "name": "sweep_export",
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
        assert store.get(rec.job_id).status == JobStatus.COMPLETED

        rows = exp._extract_result_rows(
            _mgr_with_store(store), rec.job_id)
        meas = [r for r in rows if r.get("measurement") == "measure_voltage"]
        assert len(meas) == 2, f"measure row が 2 つ無い: {rows}"
        sweep_idxs = sorted(r["sweep_index"] for r in meas)
        assert sweep_idxs == [0, 1], (
            f"v2.8: export row に sweep_index が載っていない: {meas}")
        # instrument 列も実 Job で埋まる
        assert all(r["instrument"] == res for r in meas)
    finally:
        store.close()


# ==============================================================
# 後方互換 / version
# ==============================================================


def test_stability_unchanged():
    from lab_executor import stability
    assert stability.experimental_count() == 7
    assert stability.stable_count() == 43


def test_v2_18_0_version():
    import lab_executor
    parts = lab_executor.__version__.split(".")
    assert tuple(int(p) for p in parts[:3]) >= (2, 18, 0), (
        f"v2.8 機能は version 2.18.0 で出す: {lab_executor.__version__}")
