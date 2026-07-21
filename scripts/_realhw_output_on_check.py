"""Real-hardware OUTPUT-ON test for v2.13.1.

ユーザ承認済み: PMX35-3A 出力 3.0 V / 3 秒 ON。
- OVP 5.0 V / OCP 1.0 A
- 接続: 抵抗発熱測定用配線 (T 型熱電対で 7563 が温度測定)

確認したいこと:
- predicted history (v2.13.0) によって strict mode で
  set_voltage_protection → set_current_protection → set_output ON
  が validate を通ること
- persistence hooks (v2.13.1) で job_steps / job_events が
  全 step 分書き込まれること
- verify (set 後の read-back) が動くこと
- safe_shutdown が最後に出力 OFF を保証
"""
from __future__ import annotations
import asyncio
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


async def main() -> int:
    tmpdb = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmpdb.close()
    print(f"[db] {tmpdb.name}")

    visa_examples = ROOT.parent / "visa-mcp" / "examples" / "instruments"
    registry = InstrumentRegistry(str(visa_examples))
    registry.reload()

    visa = VisaManager()
    sessions = SessionManager(visa, registry)

    pmx = "USB0::0x0B3E::0x1029::ZM000463::INSTR"
    dmm = "GPIB0::2::INSTR"
    print(f"\n[step 1] identify({pmx})")
    s_pmx = await sessions.identify(pmx)
    assert s_pmx.definition is not None
    print(f"  -> {s_pmx.definition.metadata.model}")

    print(f"[step 2] bind_definition({dmm}, Yokogawa, 7563)")
    s_dmm = sessions.bind_manually(dmm, "Yokogawa", "7563")
    assert s_dmm is not None and s_dmm.definition is not None

    store = JobStore(tmpdb.name)
    mgr = JobManager(backend=visa, session_mgr=sessions, store=store)

    plan = {
        "dsl_version": "0.8",
        "name": "v2_13_1_output_on_check",
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
             "command": "set_voltage_protection",
             "args": {"voltage": 5.0}},
            {"type": "command", "instrument": "$psu",
             "command": "set_current_protection",
             "args": {"current": 1.0}},
            {"type": "command", "instrument": "$psu",
             "command": "set_voltage",
             "args": {"voltage": 3.0}},
            {"type": "command", "instrument": "$psu",
             "command": "set_current",
             "args": {"current": 1.0}},
            {"type": "command", "instrument": "$psu",
             "command": "set_output",
             "args": {"state": "1"}},
            {"type": "wait", "seconds": 3.0},
            {"type": "query", "instrument": "$psu",
             "command": "measure_voltage"},
            {"type": "query", "instrument": "$psu",
             "command": "measure_current"},
            {"type": "query", "instrument": "$dmm",
             "command": "read_measurement"},
            {"type": "command", "instrument": "$psu",
             "command": "set_output",
             "args": {"state": "0"}},
        ],
    }

    print("\n[step 3] start_experiment_job (OUTPUT ON 3.0V 3s)")
    rec = await mgr.start_experiment_job(plan)
    job_id = rec.job_id
    print(f"  -> job_id={job_id}")

    for _ in range(150):  # 30s 上限
        await asyncio.sleep(0.2)
        cur = store.get(job_id)
        if cur and cur.status.value in (
            "completed", "failed", "cancelled", "timeout"
        ):
            break
    cur = store.get(job_id)
    print(f"  -> status={cur.status.value if cur else 'unknown'}")
    print(f"  -> current_step_index={cur.current_step_index}")
    if cur and cur.status.value == "failed":
        print(f"  -> result keys: {list((cur.result or {}).keys())}")
        if cur.result:
            ve = cur.result.get("validation_errors")
            print(f"  -> validation_errors: {ve}")
            err = cur.result.get("error")
            print(f"  -> error: {err}")

    print("\n[step 4] persistence check")
    steps = store.list_steps(job_id)
    print(f"  job_steps rows: {len(steps)}")
    for st in steps:
        print(f"    step_index={st.get('step_index')} "
              f"step_type={st.get('step_type')} "
              f"status={st.get('status')}")

    events = store.list_events(job_id)
    kinds = {}
    for e in events:
        k = e.get("event_type", "?")
        kinds[k] = kinds.get(k, 0) + 1
    print(f"\n  job_events rows: {len(events)}")
    print(f"  event_type counts: {kinds}")

    # measurement 値を抽出
    print("\n[step 5] 測定値")
    for st in steps:
        if st.get("step_type") == "command":
            res = st.get("result_json") or st.get("result")
            if res:
                # JSON 文字列 or dict
                import json as _json
                if isinstance(res, str):
                    try:
                        res = _json.loads(res)
                    except Exception:
                        pass
                if isinstance(res, dict) and "parsed" in res:
                    print(f"    step={st.get('step_index')} "
                          f"parsed={res.get('parsed')}")

    success = (
        cur is not None
        and cur.status.value == "completed"
        and len(steps) >= 10
        and kinds.get("step_started", 0) >= 10
        and kinds.get("step_completed", 0) >= 10
    )
    print(f"\n[verdict] {'PASS' if success else 'FAIL'}")
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
