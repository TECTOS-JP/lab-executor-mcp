"""Real-hardware persistence check for v2.13.1 bug fix.

v2.13.0 まで _run_experiment_plan_job が record_step_started /
record_step_completed / step_started / step_completed events を呼び
忘れていたため、get_experiment_results が rows=0 になっていた。

このスクリプトは実機 (PMX35-3A USB + 7563 GPIB) を使い、
**出力 ON を一切行わない安全な query-only plan** を 1 つ走らせて、
v2.13.1 で persistence が動くようになったかを確認する。

安全方針:
- set_output / set_voltage / set_voltage_protection など WRITE は
  使わない
- query_voltage / query_output / 7563 measurement_data の query のみ
- 機器の状態を変えないため抵抗発熱配線が繋がっていても安全
"""
from __future__ import annotations
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

# 必ず local source を使う
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
# visa-mcp の local source も
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
    # 一時 DB を使う (本番 ~/.visa-mcp/jobs.db に影響しない)
    tmpdb = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmpdb.close()
    print(f"[db] tmp store: {tmpdb.name}")

    # registry を visa-mcp の examples から拾う
    visa_examples = ROOT.parent / "visa-mcp" / "examples" / "instruments"
    print(f"[registry] loading: {visa_examples}")
    registry = InstrumentRegistry(str(visa_examples))
    registry.reload()
    print(f"[registry] loaded {len(registry.list_definitions())} "
          f"definitions")

    visa = VisaManager()
    sessions = SessionManager(visa, registry)

    # 1. PMX35-3A を *IDN? で識別
    pmx_resource = "USB0::0x0B3E::0x1029::ZM000463::INSTR"
    print(f"\n[step 1] identify({pmx_resource})")
    s_pmx = await sessions.identify(pmx_resource)
    print(f"  -> definition={s_pmx.definition.metadata.model if s_pmx.definition else None}")
    assert s_pmx.definition is not None, "PMX35-3A 識別失敗"

    # 2. 7563 は bind_definition で手動
    dmm_resource = "GPIB0::2::INSTR"
    print(f"\n[step 2] bind_definition({dmm_resource}, Yokogawa, 7563)")
    s_dmm = sessions.bind_manually(dmm_resource, "Yokogawa", "7563")
    print(f"  -> definition={s_dmm.definition.metadata.model if s_dmm and s_dmm.definition else None}")
    assert s_dmm is not None and s_dmm.definition is not None, "7563 bind 失敗"

    # 3. JobManager 構築 + 安全な query-only plan を作る
    store = JobStore(tmpdb.name)
    mgr = JobManager(backend=visa, session_mgr=sessions, store=store)

    plan = {
        "dsl_version": "0.8",
        "name": "v2_13_1_persistence_check",
        "bindings": {
            "psu": pmx_resource,
            "dmm": dmm_resource,
        },
        "steps": [
            # 全部 query。WRITE 一切なし。出力 ON もなし。
            {"type": "query", "instrument": "$psu",
             "command": "query_output"},
            {"type": "query", "instrument": "$psu",
             "command": "query_voltage"},
            {"type": "wait", "seconds": 0.3},
            {"type": "query", "instrument": "$dmm",
             "command": "read_measurement"},
        ],
    }

    # 4. start_experiment_job
    print("\n[step 3] start_experiment_job (query only, no WRITE)")
    rec = await mgr.start_experiment_job(plan)
    job_id = rec.job_id
    print(f"  -> job_id={job_id}")

    # 5. 完了待ち
    for _ in range(50):
        await asyncio.sleep(0.2)
        cur = store.get(job_id)
        if cur and cur.status.value in ("completed", "failed",
                                          "cancelled", "timeout"):
            break
    cur = store.get(job_id)
    print(f"  -> status={cur.status.value if cur else 'unknown'}")
    print(f"  -> current_step_index={cur.current_step_index}")

    # 6. **核心: job_steps テーブルに行があるか?**
    # 失敗時は result を見る
    if cur and cur.status.value == "failed":
        print(f"  -> JOB FAILED. result keys: "
              f"{list((cur.result or {}).keys())}")
        if cur.result:
            print(f"  -> validation_errors: "
                  f"{cur.result.get('validation_errors')}")
            print(f"  -> error_class: "
                  f"{cur.result.get('error_class')}")

    print("\n[step 4] persistence check")
    steps = store.list_steps(job_id)
    print(f"  job_steps rows: {len(steps)}")
    for st in steps:
        print(f"    step_index={st.get('step_index')} "
              f"step_type={st.get('step_type')} "
              f"status={st.get('status')}")

    # 7. timeline (job_events) を見る
    events = store.list_events(job_id)
    print(f"\n  job_events rows: {len(events)}")
    kinds = {}
    for e in events:
        k = e.get("event_type", "?")
        kinds[k] = kinds.get(k, 0) + 1
    print(f"  event_type counts: {kinds}")

    # 8. summary 比較
    print(f"\n[summary]")
    print(f"  v2.13.0 期待 (bug あり): job_steps=0, "
          f"step_started/_completed events=0")
    print(f"  v2.13.1 期待 (修正後):   job_steps>=3, "
          f"step_started/_completed events present")
    print(f"  実測:                   job_steps={len(steps)}, "
          f"step_started={kinds.get('step_started', 0)}, "
          f"step_completed={kinds.get('step_completed', 0)}")

    success = (
        len(steps) >= 3
        and kinds.get("step_started", 0) >= 3
        and kinds.get("step_completed", 0) >= 3
    )
    print(f"\n[verdict] {'PASS' if success else 'FAIL'}")

    try:
        os.unlink(tmpdb.name)
    except Exception:
        pass

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
