"""SP-4: pause (人間 / AI の呼び出し) のテスト (v2.30.0)

対象:
- pause 中の phase="paused" (JobStatus 8 状態は不変 — status は waiting のまま)
- expose 値の記録 / message の ${} 補間
- continue 応答で再開・後続実行 / abort 応答で failed
- pause timeout → on_timeout (abort / safe_shutdown)
- 同期経路 (execute_recipe) は AsyncStepRequiresJob で Job 化を促す
- control plane エンドポイント (token・audit)
- UI プロキシ + ジョブ詳細の pause パネル
- CLI respond-pause
- 後方互換 (pause 無しレシピの挙動不変)
"""
import asyncio
import textwrap
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from lab_executor.job import JobManager, JobStore
from lab_executor.job.state_machine import JobStatus
from lab_executor.models.instrument_def import InstrumentDefinition
from lab_executor.observation import PHASE_ENUM, compute_current_phase
from lab_executor.recipe_executor import execute_recipe, recipe_to_plan
from lab_executor.experiment_ir import PauseStep
from lab_executor.utils.seq_expression import (
    SeqExpressionError, interpolate_string, string_expr_parts,
)
from visa_mcp.session_manager import InstrumentSession

RESOURCE = "TEST::INSTR"

SAMPLE_YAML = """
metadata:
  manufacturer: "Test"
  model: "Sp4Rig"
  category: "smu"
response_formats:
  num:
    fallback: "numeric_extract"
commands:
  meas:
    scpi: "MEAS?"
    type: "query"
    returns: { type: "float", format: "num" }
  set_current:
    scpi: "CURR {current}"
    type: "write"
    parameters:
      - { name: current, type: float, range: [0.0, 0.02] }
  set_output:
    scpi: "OUTP {state}"
    type: "write"
    parameters:
      - { name: state, type: enum, choices: ["ON", "OFF"] }
safe_shutdown:
  - { command: "set_current", args: { current: 0.0 } }
  - { command: "set_output", args: { state: "OFF" } }
recipes:
  with_pause:
    description: "capture -> pause -> 続行後に出力 ON"
    steps:
      - { command: "meas", result_as: "x" }
      - pause:
          message: "測定値 ${steps.x} です。続行しますか?"
          timeout_s: 30
          on_timeout: "abort"
          expose: ["steps.x"]
      - { command: "set_output", args: { state: "ON" } }
  pause_timeout_abort:
    steps:
      - { command: "meas", result_as: "x" }
      - pause: { message: "t", timeout_s: 0.5, on_timeout: "abort" }
      - { command: "set_output", args: { state: "ON" } }
  pause_timeout_shutdown:
    steps:
      - { command: "meas", result_as: "x" }
      - pause: { message: "t", timeout_s: 0.5, on_timeout: "safe_shutdown" }
      - { command: "set_output", args: { state: "ON" } }
  no_pause:
    steps:
      - { command: "meas", result_as: "x" }
"""


def _defn() -> InstrumentDefinition:
    return InstrumentDefinition(**yaml.safe_load(textwrap.dedent(SAMPLE_YAML)))


def _session():
    return InstrumentSession(
        resource_name=RESOURCE,
        idn_response="<test>",
        idn_parsed={"manufacturer": "Test", "model": "Sp4Rig"},
        definition=_defn(),
    )


def _visa(query_return="3.0"):
    v = MagicMock()
    v.write = AsyncMock(return_value=None)
    v.query = AsyncMock(return_value=query_return)
    return v


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    visa = _visa("3.0")
    session = _session()

    class _SM:
        def get_session(self, name):
            return session if name == RESOURCE else None

    store = JobStore(db_path=tmp_path / "sp4.sqlite")
    mgr = JobManager(backend=visa, session_mgr=_SM(), store=store)
    yield mgr, store, visa, session
    store.close()


async def _wait_active_pause(store, job_id, timeout=5.0):
    for _ in range(int(timeout / 0.05)):
        p = store.get_active_pause(job_id)
        if p is not None:
            return p
        await asyncio.sleep(0.05)
    return None


async def _wait_terminal(mgr, job_id, timeout=8.0):
    for _ in range(int(timeout / 0.05)):
        cur = mgr.get(job_id)
        if cur.status in (JobStatus.COMPLETED, JobStatus.FAILED,
                          JobStatus.CANCELLED, JobStatus.TIMEOUT):
            return cur
        await asyncio.sleep(0.05)
    return mgr.get(job_id)


# ============================================================
# 1. 文字列補間ヘルパ
# ============================================================

def test_string_interpolation_helpers():
    ctx = {"params": {}, "steps": {"x": 3.5}, "vars": {}, "env": {}}
    assert string_expr_parts("v=${steps.x} / ${steps.x * 2}") == [
        "steps.x", "steps.x * 2",
    ]
    assert interpolate_string("値は ${steps.x} です", ctx) == "値は 3.5 です"
    # 評価エラー部分は fail-soft (そのまま残す)
    assert interpolate_string("${vars.missing} 継続", ctx) == "${vars.missing} 継続"
    with pytest.raises(SeqExpressionError):
        string_expr_parts("閉じない ${steps.x")


def test_pause_compile_validates_message_and_expose():
    # message / expose 内の未定義参照はコンパイルエラー
    yaml_doc = yaml.safe_load(textwrap.dedent(SAMPLE_YAML))
    yaml_doc["recipes"]["bad_msg"] = {
        "steps": [
            {"pause": {"message": "x=${steps.undefined_name}", "timeout_s": 10}},
        ],
    }
    defn = InstrumentDefinition(**yaml_doc)
    with pytest.raises(SeqExpressionError):
        recipe_to_plan(defn.recipes["bad_msg"], {}, definition=defn)
    # 正常系: PauseStep へ変換される
    plan = recipe_to_plan(_defn().recipes["with_pause"], {}, definition=_defn())
    ps = plan.steps[1]
    assert isinstance(ps, PauseStep)
    assert ps.on_timeout == "abort"
    assert ps.expose == ["steps.x"]


# ============================================================
# 2. 同期経路は Job 化を促す
# ============================================================

@pytest.mark.asyncio
async def test_sync_path_rejects_pause(monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    res = await execute_recipe(_visa(), _session(), "with_pause", {})
    assert res["success"] is False
    assert res["error"] == "AsyncStepRequiresJob"
    assert res["async_step_type"] == "pause"
    assert res["recommended_action"]["tool"] == "start_recipe_job"


# ============================================================
# 3. Job 経路: pause -> continue / abort / timeout
# ============================================================

@pytest.mark.asyncio
async def test_pause_phase_expose_and_continue(setup):
    mgr, store, visa, _ = setup
    rec = await mgr.start_recipe_job(RESOURCE, "with_pause", {})
    pause = await _wait_active_pause(store, rec.job_id)
    assert pause is not None
    # message の ${} 補間 + expose 値
    assert "3.0" in pause["message"]
    assert pause["expose"] == {"steps.x": 3.0}
    assert pause["timeout_at"] is not None
    # JobStatus は 8 状態のまま (waiting)、phase が paused
    cur = mgr.get(rec.job_id)
    assert cur.status == JobStatus.WAITING
    assert "paused" in PHASE_ENUM
    assert compute_current_phase(
        cur.status.value, None, pause_active=True,
    ) == "paused"
    # timeline に pause_requested
    events = {e["event_type"] for e in store.list_events(rec.job_id)}
    assert "pause_requested" in events

    # continue 応答 → 再開して後続 step 実行 → completed
    r = mgr.respond_pause(rec.job_id, "continue", responder="tester")
    assert r["ok"] is True
    final = await _wait_terminal(mgr, rec.job_id)
    assert final.status == JobStatus.COMPLETED, final.result
    # 後続 set_output が実行された
    steps = final.result["steps_executed"]
    assert any(s.get("command") == "set_output" for s in steps)
    pstep = steps[1]
    assert pstep["step_type"] == "pause"
    assert pstep["resolution"] == "continue"
    assert pstep["responder"] == "tester"
    events = {e["event_type"] for e in store.list_events(rec.job_id)}
    assert "pause_resolved" in events
    # pause は解決済み
    assert store.get_active_pause(rec.job_id) is None


@pytest.mark.asyncio
async def test_pause_abort_response_fails_job(setup):
    mgr, store, _, _ = setup
    rec = await mgr.start_recipe_job(RESOURCE, "with_pause", {})
    assert await _wait_active_pause(store, rec.job_id) is not None
    r = mgr.respond_pause(rec.job_id, "abort", responder="tester")
    assert r["ok"] is True
    final = await _wait_terminal(mgr, rec.job_id)
    assert final.status == JobStatus.FAILED
    failed = final.result["steps_executed"][-1]
    assert failed["error"] == "pause_aborted"


@pytest.mark.asyncio
async def test_pause_timeout_abort(setup):
    mgr, store, _, _ = setup
    rec = await mgr.start_recipe_job(RESOURCE, "pause_timeout_abort", {})
    final = await _wait_terminal(mgr, rec.job_id)
    assert final.status == JobStatus.FAILED
    failed = final.result["steps_executed"][-1]
    assert failed["error"] == "pause_timeout"
    assert failed.get("safe_shutdown") is None    # abort は shutdown しない
    events = {e["event_type"] for e in store.list_events(rec.job_id)}
    assert "pause_timeout" in events


@pytest.mark.asyncio
async def test_pause_timeout_safe_shutdown(setup):
    mgr, store, visa, _ = setup
    rec = await mgr.start_recipe_job(RESOURCE, "pause_timeout_shutdown", {})
    final = await _wait_terminal(mgr, rec.job_id)
    assert final.status == JobStatus.FAILED
    failed = final.result["steps_executed"][-1]
    assert failed["error"] == "pause_timeout"
    assert failed["safe_shutdown"] is not None
    assert failed["safe_shutdown"]["attempted"] is True
    # YAML safe_shutdown (CURR 0 / OUTP OFF) が送信された
    sent = [c.args[1] for c in visa.write.call_args_list]
    assert any("CURR 0" in s for s in sent)


@pytest.mark.asyncio
async def test_respond_pause_errors(setup):
    mgr, store, _, _ = setup
    # job 不在
    r = mgr.respond_pause("job_nonexistent", "continue")
    assert r["ok"] is False and r["error"] == "not_found"
    # 不正 action
    rec = await mgr.start_recipe_job(RESOURCE, "with_pause", {})
    assert await _wait_active_pause(store, rec.job_id) is not None
    r2 = mgr.respond_pause(rec.job_id, "retry")
    assert r2["ok"] is False and r2["error"] == "invalid_action"
    # 正常応答後はもう未解決 pause が無い
    assert mgr.respond_pause(rec.job_id, "continue")["ok"] is True
    r3 = mgr.respond_pause(rec.job_id, "continue")
    assert r3["ok"] is False and r3["error"] == "no_active_pause"
    await _wait_terminal(mgr, rec.job_id)


# ============================================================
# 4. control plane
# ============================================================

TOKEN = "a" * 64


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_control_plane_pause_response(setup):
    from starlette.testclient import TestClient
    from lab_executor.control_plane import create_control_app

    mgr, store, _, _ = setup
    app = create_control_app(mgr, token=TOKEN, backend_id="mock")
    client = TestClient(app, raise_server_exceptions=False)

    async def run():
        rec = await mgr.start_recipe_job(RESOURCE, "with_pause", {})
        assert await _wait_active_pause(store, rec.job_id) is not None
        return rec

    # Job task を保持する専用ループで起動し、TestClient (別ループ) から
    # 応答を送った後、同じ専用ループで完了まで進める。
    loop = asyncio.new_event_loop()
    try:
        rec = loop.run_until_complete(run())

        # token 無し → 401
        r = client.post(f"/control/jobs/{rec.job_id}/pause-response",
                        json={"action": "continue"})
        assert r.status_code == 401
        # 不正 action → 422
        r = client.post(
            f"/control/jobs/{rec.job_id}/pause-response",
            json={"action": "retry"},
            headers={"X-Control-Token": TOKEN},
        )
        assert r.status_code == 422
        # 正常応答 → 200 + audit
        r = client.post(
            f"/control/jobs/{rec.job_id}/pause-response",
            json={"action": "continue", "responder": "ai-agent"},
            headers={"X-Control-Token": TOKEN},
        )
        assert r.status_code == 200, r.json()
        body = r.json()
        assert body["ok"] is True and body["action"] == "continue"
        # 未解決 pause が無い job → 404
        r2 = client.post(
            f"/control/jobs/{rec.job_id}/pause-response",
            json={"action": "continue"},
            headers={"X-Control-Token": TOKEN},
        )
        assert r2.status_code == 404

        loop.run_until_complete(_wait_terminal(mgr, rec.job_id))
    finally:
        loop.close()

    # audit 記録 (cancel と同じ流儀)
    from lab_executor.audit import AuditStore
    audit = AuditStore(mgr.store)
    rows, _cursor = audit.query(limit=50)
    tool_names = {r.get("tool_name") for r in rows}
    assert "control.pause_response" in tool_names
    client_ids = {r.get("client_id") for r in rows}
    assert "control-plane" in client_ids


# ============================================================
# 5. CLI respond-pause
# ============================================================

@pytest.mark.asyncio
async def test_cli_respond_pause(setup, tmp_path, capsys):
    from lab_executor.cli import main as cli_main

    mgr, store, _, _ = setup
    rec = await mgr.start_recipe_job(RESOURCE, "with_pause", {})
    assert await _wait_active_pause(store, rec.job_id) is not None

    rc = cli_main([
        "job", "respond-pause", rec.job_id,
        "--action", "continue", "--responder", "ai-cli",
        "--db", str(store.db_path), "--json",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"ok": true' in out

    final = await _wait_terminal(mgr, rec.job_id)
    assert final.status == JobStatus.COMPLETED
    # 未解決 pause が無い場合は exit 1
    rc2 = cli_main([
        "job", "respond-pause", rec.job_id,
        "--action", "continue", "--db", str(store.db_path),
    ])
    assert rc2 == 1


# ============================================================
# 6. UI (プロキシ + pause パネル)
# ============================================================

@pytest.mark.asyncio
async def test_ui_pause_panel_and_proxy(setup, tmp_path):
    from fastapi.testclient import TestClient as FTClient
    from lab_executor.ui.app import create_app

    mgr, store, _, _ = setup
    rec = await mgr.start_recipe_job(RESOURCE, "with_pause", {})
    assert await _wait_active_pause(store, rec.job_id) is not None

    class _FakeControl:
        """control plane の代わりに respond_pause を直接呼ぶ fake client。"""
        def available(self):
            return {"backend_id": "mock", "pid": 1, "started_at": "now"}

        def pause_response(self, job_id, action, *, owner="web-ui"):
            r = mgr.respond_pause(job_id, action, responder=owner)
            return (200 if r.get("ok") else 404), r

    app = create_app(db_path=store.db_path, control_client=_FakeControl())
    client = FTClient(app)

    # 詳細 API に pause が載り phase=paused
    detail = client.get(f"/api/jobs/{rec.job_id}").json()
    assert detail["phase"] == "paused"
    assert detail["pause"] is not None
    assert "3.0" in detail["pause"]["message"]

    # HTML に pause パネル (続行/中止ボタン)
    html = client.get(f"/jobs/{rec.job_id}").text
    assert "pause-panel" in html
    assert "続行" in html and "中止" in html

    # プロキシ経由で continue
    r = client.post(
        f"/api/control/jobs/{rec.job_id}/pause-response",
        json={"action": "continue"},
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 200, r.json()

    final = await _wait_terminal(mgr, rec.job_id)
    assert final.status == JobStatus.COMPLETED

    # 解決後は pause パネルが出ない
    detail2 = client.get(f"/api/jobs/{rec.job_id}").json()
    assert detail2["pause"] is None
    assert detail2["phase"] == "completed"

    # 不正 action は 422 (プロキシ側で弾く)
    r2 = client.post(
        f"/api/control/jobs/{rec.job_id}/pause-response",
        json={"action": "retry"},
        headers={"Content-Type": "application/json"},
    )
    assert r2.status_code == 422


# ============================================================
# 7. 後方互換
# ============================================================

@pytest.mark.asyncio
async def test_backward_compat_no_pause(setup):
    mgr, store, _, _ = setup
    rec = await mgr.start_recipe_job(RESOURCE, "no_pause", {})
    final = await _wait_terminal(mgr, rec.job_id)
    assert final.status == JobStatus.COMPLETED
    assert store.get_active_pause(rec.job_id) is None
    # phase は従来どおり (pause_active=False 既定)
    assert compute_current_phase("completed", None) == "completed"
    assert compute_current_phase("waiting", None) == "waiting"
