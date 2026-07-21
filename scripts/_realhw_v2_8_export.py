"""v2.8 実機 E2E: export の sweep_index / sweep_value / instrument 列。

ユーザ承認済み (レジスタ加熱配線): PMX35-3A を 1.0V -> 2.0V に sweep し、
各点で measure_voltage / measure_current / 7563 温度を読む。
- OVP 6.0V / OCP 1.5A、各点 ON 1.5s、末尾で出力 OFF + safe_shutdown

検証:
  (1) get_experiment_results の columns に sweep_index / sweep_value
  (2) measure_voltage 行が sweep_index [0,1] と instrument(=PMX) を持つ
  (3) VISA_MCP_EXPORT_DIR を tmp に向けて CSV export -> ヘッダに sweep 列、
      行に値が出る
"""
from __future__ import annotations
import asyncio
import csv
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
VISA_SRC = ROOT.parent / "visa-mcp" / "src"
sys.path.insert(0, str(VISA_SRC))

import lab_executor  # noqa
import lab_visa_mcp  # noqa
print(f"[versions] lab_executor={lab_executor.__version__}, "
      f"lab_visa_mcp={lab_visa_mcp.__version__}")

from lab_visa_mcp.visa_manager import VisaManager
from lab_visa_mcp.session_manager import SessionManager
from lab_visa_mcp.instrument_registry import InstrumentRegistry
from lab_executor.job.manager import JobManager
from lab_executor.job.store import JobStore
from lab_executor.tools import export as exp
from fastmcp import FastMCP


async def main() -> int:
    tmpdb = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmpdb.close()
    export_dir = Path(tempfile.mkdtemp(prefix="v2_8_exports_"))
    os.environ["VISA_MCP_EXPORT_DIR"] = str(export_dir)
    print(f"[db] {tmpdb.name}")
    print(f"[export_dir] {export_dir}")

    visa_examples = ROOT.parent / "visa-mcp" / "examples" / "instruments"
    registry = InstrumentRegistry(str(visa_examples))
    registry.reload()

    visa = VisaManager()
    sessions = SessionManager(visa, registry)

    pmx = "USB0::0x0B3E::0x1029::ZM000463::INSTR"
    dmm = "GPIB0::2::INSTR"
    s_pmx = await sessions.identify(pmx)
    assert s_pmx.definition is not None
    print(f"[step 1] identified PMX: {s_pmx.definition.metadata.model}")
    s_dmm = sessions.bind_manually(dmm, "Yokogawa", "7563")
    assert s_dmm is not None and s_dmm.definition is not None
    print(f"[step 2] bound DMM:    {s_dmm.definition.metadata.model}")

    store = JobStore(tmpdb.name)
    mgr = JobManager(backend=visa, session_mgr=sessions, store=store)

    plan = {
        "dsl_version": "0.8",
        "name": "v2_8_export_sweep",
        "bindings": {"psu": pmx, "dmm": dmm},
        "safe_shutdown": {
            "targets": [
                {"resource": "$psu",
                 "commands": [{"command": "set_output",
                               "args": {"state": "0"}}]}
            ]
        },
        "steps": [
            {"type": "command", "instrument": "$psu",
             "command": "set_voltage_protection", "args": {"voltage": 6.0}},
            {"type": "command", "instrument": "$psu",
             "command": "set_current_protection", "args": {"current": 1.5}},
            {"type": "command", "instrument": "$psu",
             "command": "set_voltage", "args": {"voltage": 0.0}},
            {"type": "command", "instrument": "$psu",
             "command": "set_current", "args": {"current": 1.0}},
            {"type": "command", "instrument": "$psu",
             "command": "set_output", "args": {"state": "1"}},
            {"type": "sweep", "parameter": "v",
             "values": {"values": [1.0, 2.0]},
             "body": [
                 {"type": "command", "instrument": "$psu",
                  "command": "set_voltage", "args": {"voltage": "{v}"}},
                 {"type": "wait", "seconds": 1.5},
                 {"type": "query", "instrument": "$psu",
                  "command": "measure_voltage"},
                 {"type": "query", "instrument": "$psu",
                  "command": "measure_current"},
                 {"type": "query", "instrument": "$dmm",
                  "command": "read_measurement"},
             ]},
            {"type": "command", "instrument": "$psu",
             "command": "set_output", "args": {"state": "0"}},
        ],
    }

    print("\n[step 3] start_experiment_job (sweep 1->2V)")
    rec = await mgr.start_experiment_job(plan)
    job_id = rec.job_id
    for _ in range(300):
        await asyncio.sleep(0.2)
        cur = store.get(job_id)
        if cur and cur.status.value in (
            "completed", "failed", "cancelled", "timeout"
        ):
            break
    cur = store.get(job_id)
    print(f"  -> job_id={job_id} status={cur.status.value if cur else '?'}")
    if cur and cur.status.value != "completed":
        print(f"  -> result: {json.dumps(cur.result, default=str)[:800]}")
        return 1

    # (1)+(2) get_experiment_results 経由で列と sweep 値を確認
    print("\n[step 4] get_experiment_results 列 + sweep_index/instrument")
    mcp = FastMCP("e2e")
    exp.register_tools(mcp, mgr)
    tool = await mcp.get_tool("get_experiment_results")
    res = await tool.fn(job_id=job_id, limit=10000)
    data = res["data"]
    cols = data["columns"]
    print(f"  columns: {cols}")
    assert "sweep_index" in cols and "sweep_value" in cols, "列が無い"

    rows = data["rows"]
    mv = [r for r in rows if r.get("measurement") == "measure_voltage"]
    print(f"  measure_voltage rows: {len(mv)}")
    for r in mv:
        print(f"    sweep_index={r.get('sweep_index')} "
              f"sweep_value={r.get('sweep_value')} "
              f"instrument={r.get('instrument')} value={r.get('value')}")
    idxs = sorted(r.get("sweep_index") for r in mv)
    ok_idx = idxs == [0, 1]
    ok_instr = all(r.get("instrument") == pmx for r in mv)
    ok_sval = sorted(float(r.get("sweep_value")) for r in mv) == [1.0, 2.0]
    print(f"  sweep_index={idxs} ok={ok_idx} / instrument ok={ok_instr} "
          f"/ sweep_value ok={ok_sval}")

    # (3) CSV export -> VISA_MCP_EXPORT_DIR 配下 + sweep 列
    print("\n[step 5] export_experiment_results(csv) -> env export dir")
    etool = await mcp.get_tool("export_experiment_results")
    eres = await etool.fn(job_id=job_id, format="csv")
    edata = eres.get("data") or {}
    csv_path = edata.get("path")
    print(f"  -> path={csv_path}  rows={edata.get('rows')}")
    ok_under = bool(csv_path) and str(csv_path).startswith(str(export_dir))
    print(f"  under VISA_MCP_EXPORT_DIR={export_dir}: {ok_under}")
    ok_csv_cols = False
    ok_csv_vals = False
    if csv_path and Path(csv_path).exists():
        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            header = reader.fieldnames or []
            csv_rows = list(reader)
        ok_csv_cols = ("sweep_index" in header and "sweep_value" in header)
        csv_mv = [r for r in csv_rows
                  if r.get("measurement") == "measure_voltage"]
        ok_csv_vals = sorted(r.get("sweep_index") for r in csv_mv) == ["0", "1"]
        print(f"  csv header has sweep cols={ok_csv_cols} / "
              f"csv measure_voltage sweep_index="
              f"{sorted(r.get('sweep_index') for r in csv_mv)}")

    success = all([ok_idx, ok_instr, ok_sval, ok_under,
                   ok_csv_cols, ok_csv_vals])
    print(f"\n[verdict] {'PASS' if success else 'FAIL'}")
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
