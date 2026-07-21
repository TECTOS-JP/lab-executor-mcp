"""v0.9.0: Benchmark 基盤 + mock instruments テスト"""
from __future__ import annotations
import asyncio
import os
from pathlib import Path

import pytest

from lab_executor.testing.benchmark_task import (
    BenchmarkTask, load_benchmark_task, load_benchmark_tasks,
)
from lab_executor.testing.mock_instruments import (
    InstrumentScenario, MockVisaManager,
)
from lab_executor.testing.benchmark_runner import run_task_file

ROOT = Path(__file__).parent.parent
BENCHMARKS = ROOT / "benchmarks"


@pytest.fixture(autouse=True)
def _safety_mode(monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")


# =========================================================
# Benchmark task schema / loader
# =========================================================


def test_benchmark_task_schema_loads():
    p = BENCHMARKS / "tasks" / "task_001_basic_validate_dry_run.yaml"
    t = load_benchmark_task(p)
    assert t.id == "task_001_basic_validate_dry_run"
    assert t.layer == "dry_run"
    assert t.input.plan is not None


def test_benchmark_task_rejects_invalid_schema(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("id: 'bad id with spaces'\n", encoding="utf-8")
    with pytest.raises(Exception):
        load_benchmark_task(bad)


def test_load_all_tasks():
    tasks = load_benchmark_tasks(BENCHMARKS / "tasks")
    assert len(tasks) >= 5
    ids = [t.id for t in tasks]
    assert "task_001_basic_validate_dry_run" in ids
    assert "task_005_partial_failure_parallel" in ids


# =========================================================
# Mock instruments
# =========================================================


@pytest.mark.asyncio
async def test_mock_visa_query_constant():
    m = MockVisaManager()
    m.register("r1", InstrumentScenario(mode="constant", value="5.0"))
    r = await m.query("r1", "MEAS:VOLT?")
    assert r == "5.0"


@pytest.mark.asyncio
async def test_mock_visa_echo():
    m = MockVisaManager()
    m.register("r1",
               InstrumentScenario(command_pattern="VOLT.*", mode="echo"),
               InstrumentScenario(command_pattern="MEAS.*", mode="echo"))
    await m.write("r1", "VOLT 3.5")
    r = await m.query("r1", "MEAS:VOLT?")
    assert float(r) == 3.5


@pytest.mark.asyncio
async def test_mock_visa_timeout_raises():
    m = MockVisaManager()
    m.register("r1", InstrumentScenario(mode="timeout"))
    from lab_visa_mcp.visa_manager import VisaTimeoutError
    with pytest.raises(VisaTimeoutError):
        await m.query("r1", "X?")


@pytest.mark.asyncio
async def test_mock_flaky_recovers():
    """flaky: 最初 2 回 timeout、3 回目で成功"""
    m = MockVisaManager()
    m.register("r1", InstrumentScenario(
        mode="flaky", timeout_after_calls=2, value="ok",
    ))
    from lab_visa_mcp.visa_manager import VisaTimeoutError
    with pytest.raises(VisaTimeoutError):
        await m.query("r1", "X?")
    with pytest.raises(VisaTimeoutError):
        await m.query("r1", "X?")
    r = await m.query("r1", "X?")
    assert r == "ok"


@pytest.mark.asyncio
async def test_mock_verify_mismatch():
    m = MockVisaManager()
    m.register("r1", InstrumentScenario(
        mode="verify_mismatch", actual_offset=-0.2,
    ))
    await m.write("r1", "VOLT 5.0")
    r = await m.query("r1", "MEAS:VOLT?")
    assert abs(float(r) - 4.8) < 1e-6


# =========================================================
# Benchmark runner (3 layer execution)
# =========================================================


@pytest.mark.asyncio
async def test_benchmark_validate_dry_run_task_passes(tmp_path):
    res = await run_task_file(
        BENCHMARKS / "tasks" / "task_001_basic_validate_dry_run.yaml",
        BENCHMARKS, tmp_path,
    )
    assert res.status == "passed", [
        (c.name, c.status, c.message) for c in res.checks
    ]


@pytest.mark.asyncio
async def test_benchmark_unit_based_plan_passes(tmp_path):
    res = await run_task_file(
        BENCHMARKS / "tasks" / "task_002_unit_based_voltage_sweep.yaml",
        BENCHMARKS, tmp_path,
    )
    assert res.status == "passed", [
        (c.name, c.status, c.message) for c in res.checks
    ]
    assert res.artifacts.get("job_outcome") == "success"


@pytest.mark.asyncio
async def test_benchmark_template_override_passes(tmp_path):
    res = await run_task_file(
        BENCHMARKS / "tasks" / "task_003_template_override_run.yaml",
        BENCHMARKS, tmp_path,
    )
    assert res.status == "passed", [
        (c.name, c.status, c.message) for c in res.checks
    ]


@pytest.mark.asyncio
async def test_benchmark_partial_failure_detected(tmp_path):
    res = await run_task_file(
        BENCHMARKS / "tasks" / "task_005_partial_failure_parallel.yaml",
        BENCHMARKS, tmp_path,
    )
    # job_outcome=partial_failure を実際に検出できる
    assert res.artifacts.get("job_outcome") == "partial_failure"


# =========================================================
# Benchmark result dict 形式
# =========================================================


@pytest.mark.asyncio
async def test_benchmark_result_to_dict_shape(tmp_path):
    res = await run_task_file(
        BENCHMARKS / "tasks" / "task_001_basic_validate_dry_run.yaml",
        BENCHMARKS, tmp_path,
    )
    d = res.to_dict()
    assert d["task_id"] == "task_001_basic_validate_dry_run"
    assert d["status"] in ("passed", "failed")
    assert "checks" in d and isinstance(d["checks"], list)
    assert "tool_call_log" in d
