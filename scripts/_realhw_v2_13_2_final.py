"""v2.13.2 最終実機検証.

A. validate_experiment_plan (strict) errors=[]
B. start_experiment_job -> status=completed
C. get_experiment_results rows >= 12
D. get_experiment_timeline step_started=26 / step_completed=26
E. timeline step_completed event payload に raw_response / scpi_sent
F. 出力は最終 OFF
"""
from __future__ import annotations
import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
VISA_SRC = ROOT.parent / "visa-mcp" / "src"
sys.path.insert(0, str(VISA_SRC))

import lab_executor  # noqa
import lab_visa_mcp  # noqa

from lab_visa_mcp.visa_manager import VisaManager
from lab_visa_mcp.session_manager import SessionManager
from lab_visa_mcp.instrument_registry import InstrumentRegistry
from lab_executor.job.manager import JobManager
from lab_executor.job.store import JobStore
from lab_executor.dsl.compiler import validate_and_compile
from lab_executor.dsl.schema import ExperimentPlan
from lab_executor.system_config import SystemConfig
from lab_executor.tools.export import _extract_result_rows


async def main() -> int:
    print(f"[versions] lab_executor={lab_executor.__version__} "
          f"lab_visa_mcp={lab_visa_mcp.__version__}")
    tmpdb = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmpdb.close()

    registry = InstrumentRegistry(
        str(ROOT.parent / "visa-mcp" / "examples" / "instruments"))
    registry.reload()
    visa = VisaManager()
    sessions = SessionManager(visa, registry)

    pmx = "USB0::0x0B3E::0x1029::ZM000463::INSTR"
    dmm = "GPIB0::2::INSTR"
    await sessions.identify(pmx)
    sessions.bind_manually(dmm, "Yokogawa", "7563")

    store = JobStore(tmpdb.name)
    mgr = JobManager(backend=visa, session_mgr=sessions, store=store)

    plan_dict = {
        "dsl_version": "0.8",
        "name": "v2_13_2_final",
        "bindings": {"psu": pmx, "dmm": dmm},
        "safe_shutdown": {"targets": [
            {"resource": "$psu",
             "commands": [{"command": "set_output",
                           "args": {"state": "0"}}]}]},
        "steps": [
            {"type": "command", "instrument": "$psu",
             "command": "set_voltage_protection",
             "args": {"voltage": 6.0}},
            {"type": "command", "instrument": "$psu",
             "command": "set_current_protection",
             "args": {"current": 1.5}},
            {"type": "command", "instrument": "$psu",
             "command": "set_voltage", "args": {"voltage": 0.0}},
            {"type": "command", "instrument": "$psu",
             "command": "set_current", "args": {"current": 1.0}},
            {"type": "command", "instrument": "$psu",
             "command": "set_output", "args": {"state": "1"}},
            {"type": "sweep", "parameter": "v",
             "values": {"values": [1.0, 2.0, 3.0, 4.0]},
             "body": [
                 {"type": "command", "instrument": "$psu",
                  "command": "set_voltage",
                  "args": {"voltage": "{v}"}},
                 {"type": "wait", "seconds": 2.0},
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

    # A. validate strict via compiler
    sysconf = SystemConfig()
    cp = validate_and_compile(plan_dict, sessions, sysconf)
    a_pass = cp.valid and len(cp.errors) == 0
    print(f"[A] validate strict: errors={len(cp.errors)} "
          f"warnings={len(cp.warnings)}  {'PASS' if a_pass else 'FAIL'}")
    if cp.errors:
        for e in cp.errors[:3]:
            print(f"    {e}")

    # B. run
    rec = await mgr.start_experiment_job(plan_dict)
    job_id = rec.job_id
    for _ in range(300):
        await asyncio.sleep(0.2)
        cur = store.get(job_id)
        if cur and cur.status.value in (
            "completed", "failed", "cancelled", "timeout"):
            break
    b_pass = cur and cur.status.value == "completed"
    print(f"[B] start_experiment_job: status={cur.status.value} "
          f"job_id={job_id}  {'PASS' if b_pass else 'FAIL'}")

    # C. results rows
    rows = _extract_result_rows(mgr, job_id)
    c_pass = len(rows) >= 12
    print(f"[C] get_experiment_results rows={len(rows)} "
          f"(expect>=12)  {'PASS' if c_pass else 'FAIL'}")

    # D. timeline counts
    events = store.list_events(job_id)
    sc = sum(1 for e in events if e.get("event_type") == "step_completed")
    ss = sum(1 for e in events if e.get("event_type") == "step_started")
    d_pass = sc == 26 and ss == 26
    print(f"[D] timeline step_started={ss} step_completed={sc} "
          f"(expect 26/26)  {'PASS' if d_pass else 'FAIL'}")

    # E. step_completed payload has raw_response for query steps
    e_pass = False
    for ev in events:
        if ev.get("event_type") != "step_completed":
            continue
        p = ev.get("payload") or {}
        if isinstance(p, str):
            try:
                p = json.loads(p)
            except Exception:
                pass
        if isinstance(p, dict) and "raw_response" in p:
            e_pass = True
            break
    print(f"[E] step_completed payload has raw_response: "
          f"{'PASS' if e_pass else 'FAIL'}")

    # F. final output OFF — last step (index 25) is set_output state=0
    last_step = next(
        (r for r in rows if r.get("step_index") == 25), None)
    # set_output is write -> result not in rows; check last set_output OFF
    # via store steps
    steps = store.list_steps(job_id)
    last_off = next(
        (s for s in reversed(steps)
         if (s.get("result") or {}).get("command") == "set_output"
         and (s.get("result") or {}).get("args", {}).get("state") in ("0", 0)),
        None)
    f_pass = last_off is not None and last_off.get("status") == "ok"
    print(f"[F] final set_output OFF executed ok: "
          f"{'PASS' if f_pass else 'FAIL'}")

    # 測定値ダンプ
    print("\n--- 測定値 ---")
    for r in rows:
        print(f"  step={r.get('step_index'):>2} "
              f"meas={r.get('measurement'):<25} value={r.get('value')}")

    all_pass = a_pass and b_pass and c_pass and d_pass and e_pass and f_pass
    print(f"\n[verdict] {'PASS' if all_pass else 'FAIL'}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
