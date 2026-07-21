"""v2.9 実機 E2E: export 結果の instrument / sweep_index フィルタ。

ユーザ承認済み (レジスタ加熱配線): PMX35-3A を 1.0V -> 2.0V -> 3.0V に
sweep し、各点で measure_voltage / measure_current / 7563 温度を読む。
OVP 6.0V / OCP 1.5A、各点 ON 1.2s、末尾で出力 OFF + safe_shutdown。

検証:
  (1) get_experiment_results(instrument=PMX, measurement=measure_voltage)
      が PMX の電圧測定 row だけ (3点) を返す
  (2) get_experiment_results(sweep_index=1) が 2 点目の row だけ
  (3) get_experiment_results(instrument=DMM) が DMM の row だけ
  (4) export_experiment_results(csv, instrument=PMX,
      measurement=measure_voltage) の CSV が 3 行
  (5) filters echo が response に載る
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
    export_dir = Path(tempfile.mkdtemp(prefix="v2_9_exports_"))
    os.environ["VISA_MCP_EXPORT_DIR"] = str(export_dir)
    print(f"[db] {tmpdb.name}\n[export_dir] {export_dir}")

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
        "dsl_version": "0.8", "name": "v2_9_filter_sweep",
        "bindings": {"psu": pmx, "dmm": dmm},
        "safe_shutdown": {"targets": [
            {"resource": "$psu", "commands": [
                {"command": "set_output", "args": {"state": "0"}}]}]},
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
             "values": {"values": [1.0, 2.0, 3.0]},
             "body": [
                 {"type": "command", "instrument": "$psu",
                  "command": "set_voltage", "args": {"voltage": "{v}"}},
                 {"type": "wait", "seconds": 1.2},
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

    print("\n[step 3] start_experiment_job (sweep 1->3V)")
    rec = await mgr.start_experiment_job(plan)
    job_id = rec.job_id
    for _ in range(400):
        await asyncio.sleep(0.2)
        cur = store.get(job_id)
        if cur and cur.status.value in (
                "completed", "failed", "cancelled", "timeout"):
            break
    cur = store.get(job_id)
    print(f"  -> job_id={job_id} status={cur.status.value if cur else '?'}")
    if not cur or cur.status.value != "completed":
        print(f"  -> {json.dumps(cur.result, default=str)[:600] if cur else ''}")
        return 1

    mcp = FastMCP("e2e")
    exp.register_tools(mcp, mgr)
    gtool = await mcp.get_tool("get_experiment_results")
    etool = await mcp.get_tool("export_experiment_results")

    # (1) PMX measure_voltage のみ
    r1 = await gtool.fn(job_id=job_id, instrument=pmx,
                        measurement="measure_voltage", limit=10000)
    rows1 = r1["data"]["rows"]
    ok1 = (len(rows1) == 3
           and all(r["instrument"] == pmx
                   and r["measurement"] == "measure_voltage" for r in rows1))
    print(f"\n[check1] PMX measure_voltage rows={len(rows1)} ok={ok1} "
          f"filters={r1['data']['filters']}")
    for r in rows1:
        print(f"    sweep_index={r['sweep_index']} value={r['value']}")

    # (2) sweep_index=1 のみ
    r2 = await gtool.fn(job_id=job_id, sweep_index=1, limit=10000)
    rows2 = r2["data"]["rows"]
    ok2 = bool(rows2) and all(r["sweep_index"] == 1 for r in rows2)
    print(f"[check2] sweep_index=1 rows={len(rows2)} ok={ok2}")

    # (3) DMM のみ
    r3 = await gtool.fn(job_id=job_id, instrument=dmm, limit=10000)
    rows3 = r3["data"]["rows"]
    ok3 = bool(rows3) and all(r["instrument"] == dmm for r in rows3)
    print(f"[check3] DMM rows={len(rows3)} ok={ok3}")

    # (4) filtered CSV export
    e = await etool.fn(job_id=job_id, format="csv", instrument=pmx,
                       measurement="measure_voltage")
    edata = e["data"]
    csv_path = edata.get("path")
    ok4 = edata.get("rows") == 3
    if csv_path and Path(csv_path).exists():
        with open(csv_path, newline="", encoding="utf-8") as f:
            csv_rows = list(csv.DictReader(f))
        ok4 = ok4 and len(csv_rows) == 3 and all(
            r["measurement"] == "measure_voltage" for r in csv_rows)
    print(f"[check4] filtered CSV rows={edata.get('rows')} ok={ok4} "
          f"filters={edata.get('filters')}")

    # (5) no-filter = 全件 (回帰)
    r0 = await gtool.fn(job_id=job_id, limit=10000)
    ok5 = r0["data"]["filters"] == {
        "instrument": None, "sweep_index": None, "measurement": None}
    print(f"[check5] no-filter total={r0['data']['pagination']['total']} "
          f"filters echo ok={ok5}")

    success = all([ok1, ok2, ok3, ok4, ok5])
    print(f"\n[verdict] {'PASS' if success else 'FAIL'}")
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
