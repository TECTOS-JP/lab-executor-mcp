"""Real-hardware voltage sweep + temperature read.

ユーザ承認済み: PMX35-3A を 1.0V → 2.0V → 3.0V → 4.0V に段階的に
上げて、各点で 7563 (T 型熱電対) で温度を読む。
- OVP 6.0 V / OCP 1.5 A
- 各点 ON 2 秒 → measure_voltage / measure_current / 温度
- 最後に出力 OFF (safe_shutdown でも保険)
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
import visa_mcp  # noqa
print(f"[versions] lab_executor={lab_executor.__version__}, "
      f"visa_mcp={visa_mcp.__version__}")

from visa_mcp.visa_manager import VisaManager
from visa_mcp.session_manager import SessionManager
from visa_mcp.instrument_registry import InstrumentRegistry
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
        "name": "v2_13_1_voltage_sweep_temp_read",
        "bindings": {"psu": pmx, "dmm": dmm},
        "safe_shutdown": {
            "targets": [
                {"resource": "$psu",
                 "commands": [{"command": "set_output",
                               "args": {"state": "0"}}]}
            ]
        },
        "steps": [
            # 1. 保護設定 (OVP / OCP) - precondition 満たすため先
            {"type": "command", "instrument": "$psu",
             "command": "set_voltage_protection",
             "args": {"voltage": 6.0}},
            {"type": "command", "instrument": "$psu",
             "command": "set_current_protection",
             "args": {"current": 1.5}},
            # 2. 初期 0V / 電流リミット
            {"type": "command", "instrument": "$psu",
             "command": "set_voltage", "args": {"voltage": 0.0}},
            {"type": "command", "instrument": "$psu",
             "command": "set_current", "args": {"current": 1.0}},
            # 3. 出力 ON
            {"type": "command", "instrument": "$psu",
             "command": "set_output", "args": {"state": "1"}},
            # 4. sweep 1.0 → 4.0 V (4 点)
            {"type": "sweep",
             "parameter": "v",
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
            # 5. 出力 OFF
            {"type": "command", "instrument": "$psu",
             "command": "set_output", "args": {"state": "0"}},
        ],
    }

    print("\n[step 3] start_experiment_job (sweep 1→4V, 2s each)")
    rec = await mgr.start_experiment_job(plan)
    job_id = rec.job_id
    print(f"  -> job_id={job_id}")

    for _ in range(300):  # 60s 上限
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
        print(f"  -> result: {json.dumps(cur.result, default=str, indent=2)[:800]}")

    print("\n[step 4] persistence")
    steps = store.list_steps(job_id)
    print(f"  job_steps rows: {len(steps)}")

    events = store.list_events(job_id)
    kinds: dict[str, int] = {}
    for e in events:
        k = e.get("event_type", "?")
        kinds[k] = kinds.get(k, 0) + 1
    print(f"  job_events rows: {len(events)}  kinds: {kinds}")

    # 測定結果を抜く
    print("\n[step 5] 測定値抽出")
    for st in steps:
        if st.get("step_type") != "command":
            continue
        result = st.get("result")
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except Exception:
                pass
        if not isinstance(result, dict):
            continue
        # query 結果のみ拾う (parsed があるはず)
        parsed = result.get("parsed")
        cmd = result.get("command") or st.get("step_summary")
        if parsed is not None:
            print(f"    step={st.get('step_index'):>2} "
                  f"cmd={cmd!s:<30s} parsed={parsed}")

    success = (
        cur is not None
        and cur.status.value == "completed"
        and kinds.get("step_started", 0) >= 1
        and kinds.get("step_completed", 0) >= 1
    )
    print(f"\n[verdict] {'PASS' if success else 'FAIL'}")
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
