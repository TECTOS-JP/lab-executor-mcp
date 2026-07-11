"""SP-3: branch / guard / repeat のテスト (v2.29.0)

対象:
- branch: 採択 / else / ネスト / 深さ上限 / 全経路定義検証
- repeat: count / while + max_iterations / 上限 / env.loop_index
- guard: 3 動作 (abort / safe_shutdown / warn) + 評価エラー
- deferred の非数値解決値の明示エラー化 (SP-2 改善)
- Job 経路 (branch_taken / repeat_ended / guard_failed timeline)
- dry-run (branch 全 case 展開 / repeat / guard)
- 後方互換
"""
import asyncio
import textwrap
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from lab_executor.models.instrument_def import InstrumentDefinition
from lab_executor.recipe_executor import (
    BRANCH_MAX_DEPTH, REPEAT_MAX_COUNT,
    execute_recipe, recipe_to_plan,
)
from lab_executor.experiment_ir import (
    BranchStep, CommandStep, GuardStep, RepeatStep,
)
from lab_executor.utils.seq_expression import SeqExpressionError
from lab_executor.ui.views import dryrun_view
from visa_mcp.session_manager import InstrumentSession


SAMPLE_YAML = """
metadata:
  manufacturer: "Test"
  model: "Sp3Rig"
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
  branch_basic:
    description: "branch の採択 (compute で電流を決める)"
    steps:
      - { command: "meas", result_as: "r" }
      - branch:
          - when: "steps.r < 1"
            steps:
              - compute: { set: "c", expr: "0.010" }
          - when: "steps.r < 10"
            steps:
              - compute: { set: "c", expr: "0.001" }
          - else:
            steps:
              - compute: { set: "c", expr: "0.0001" }
      - { command: "set_current", args: { current: "${vars.c}" } }
  branch_nested:
    description: "branch のネスト (深さ 2)"
    steps:
      - { command: "meas", result_as: "r" }
      - branch:
          - when: "steps.r > 1"
            steps:
              - branch:
                  - when: "steps.r > 100"
                    steps:
                      - compute: { set: "zone", expr: "2" }
                  - else:
                    steps:
                      - compute: { set: "zone", expr: "1" }
          - else:
            steps:
              - compute: { set: "zone", expr: "0" }
      - compute: { set: "zone2", expr: "vars.zone * 10" }
  branch_no_else_use_after:
    description: "else 無しの分岐内定義変数を分岐後に参照 (コンパイルエラー)"
    steps:
      - { command: "meas", result_as: "r" }
      - branch:
          - when: "steps.r > 1"
            steps:
              - compute: { set: "c", expr: "1.0" }
      - compute: { set: "d", expr: "vars.c + 1" }
  repeat_count:
    description: "count 反復 + env.loop_index"
    parameters:
      - { name: "n", type: "integer", default: 3 }
    steps:
      - repeat:
          count: "$n"
          steps:
            - compute: { set: "last_i", expr: "env.loop_index" }
            - { command: "meas", result_as: "v" }
      - compute: { set: "final", expr: "vars.last_i + steps.v" }
  repeat_while:
    description: "while 反復 (max_iterations 到達は failed にしない)"
    steps:
      - { command: "meas", result_as: "drift" }
      - compute: { set: "n_done", expr: "0" }
      - repeat:
          while: "steps.drift > 0.01"
          max_iterations: 4
          steps:
            - compute: { set: "n_done", expr: "vars.n_done + 1" }
      - guard: { expr: "vars.n_done <= 4", on_fail: "warn" }
  guard_flow:
    description: "guard 3 動作"
    parameters:
      - { name: "limit", type: "float", default: 10.0 }
    steps:
      - { command: "meas", result_as: "x" }
      - guard: { expr: "steps.x < params.limit", on_fail: "warn",
                 message: "x が大きい (warn)" }
      - guard: { expr: "steps.x < 100", on_fail: "abort",
                 message: "x が大きすぎる" }
  guard_shutdown:
    description: "guard safe_shutdown"
    steps:
      - { command: "meas", result_as: "x" }
      - guard: { expr: "steps.x < 1", on_fail: "safe_shutdown",
                 message: "物理範囲外" }
  deferred_str:
    description: "deferred の解決値が str (明示エラーになるべき)"
    steps:
      - { command: "meas", result_as: "x" }
      - compute: { set: "s", expr: "'ON' if steps.x > 0 else 'OFF'" }
      - { command: "set_current", args: { current: "${vars.s}" } }
"""


def _defn() -> InstrumentDefinition:
    return InstrumentDefinition(**yaml.safe_load(textwrap.dedent(SAMPLE_YAML)))


def _session():
    return InstrumentSession(
        resource_name="TEST::INSTR",
        idn_response="<test>",
        idn_parsed={"manufacturer": "Test", "model": "Sp3Rig"},
        definition=_defn(),
    )


def _visa(query_return="3.0"):
    v = MagicMock()
    v.write = AsyncMock(return_value=None)
    v.query = AsyncMock(return_value=query_return)
    return v


# ============================================================
# 1. branch
# ============================================================

@pytest.mark.asyncio
async def test_branch_takes_first_true_case(monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    res = await execute_recipe(_visa("3.0"), _session(), "branch_basic", {})
    assert res["success"] is True, res
    br = res["steps_executed"][1]
    assert br["step_type"] == "branch"
    assert br["case_index"] == 1               # 3.0: case0 (r<1) 偽, case1 (r<10) 真
    assert res["variables"]["vars"]["c"] == 0.001
    # 採択 case のみ実行される (1 compute のみ)
    assert len(br["steps_executed"]) == 1
    assert br["steps_executed"][0]["step_path"] == "steps[1]/branch/case[1]/steps[0]"
    # deferred が採択値で解決される
    sc = res["steps_executed"][2]
    assert abs(float(sc["args"]["current"]) - 0.001) < 1e-9


@pytest.mark.asyncio
async def test_branch_else_taken(monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    res = await execute_recipe(_visa("50.0"), _session(), "branch_basic", {})
    assert res["success"] is True, res
    assert res["steps_executed"][1]["case_index"] == 2
    assert res["variables"]["vars"]["c"] == 0.0001


@pytest.mark.asyncio
async def test_branch_nested(monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    res = await execute_recipe(_visa("3.0"), _session(), "branch_nested", {})
    assert res["success"] is True, res
    assert res["variables"]["vars"]["zone"] == 1
    assert res["variables"]["vars"]["zone2"] == 10
    # 階層 step_path が保持される
    outer = res["steps_executed"][1]
    inner = outer["steps_executed"][0]
    assert inner["step_path"] == "steps[1]/branch/case[0]/steps[0]"
    assert inner["steps_executed"][0]["step_path"] == (
        "steps[1]/branch/case[0]/steps[0]/branch/case[1]/steps[0]"
    )


def test_branch_all_path_definition_check():
    # else 無しで分岐内定義変数を分岐後に使う → コンパイルエラー
    with pytest.raises(SeqExpressionError):
        recipe_to_plan(
            _defn().recipes["branch_no_else_use_after"], {}, definition=_defn(),
        )
    # else 付き (branch_basic) は分岐後の ${vars.c} が通る
    plan = recipe_to_plan(_defn().recipes["branch_basic"], {}, definition=_defn())
    assert isinstance(plan.steps[1], BranchStep)


def test_branch_depth_limit():
    # 深さ 4 の branch ネストを動的に構築 → コンパイルエラー
    inner: dict = {"compute": {"set": "z", "expr": "1"}}
    for _ in range(BRANCH_MAX_DEPTH + 1):   # 4 重
        inner = {"branch": [{"when": "params.p > 0", "steps": [inner]}]}
    yaml_doc = yaml.safe_load(textwrap.dedent(SAMPLE_YAML))
    yaml_doc["recipes"]["deep"] = {
        "parameters": [{"name": "p", "type": "float", "default": 1.0}],
        "steps": [inner],
    }
    defn = InstrumentDefinition(**yaml_doc)
    with pytest.raises(SeqExpressionError, match="ネスト深さ"):
        recipe_to_plan(defn.recipes["deep"], {}, definition=defn)


# ============================================================
# 2. repeat
# ============================================================

@pytest.mark.asyncio
async def test_repeat_count_and_loop_index(monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    res = await execute_recipe(_visa("2.0"), _session(), "repeat_count", {"n": 3})
    assert res["success"] is True, res
    rp = res["steps_executed"][0]
    assert rp["step_type"] == "repeat"
    assert rp["iterations"] == 3
    assert rp["ended"] == "count_completed"
    assert res["variables"]["vars"]["last_i"] == 2   # 0,1,2 の最後
    assert res["variables"]["vars"]["final"] == 4.0  # 2 + 2.0
    # 反復の step_path
    assert rp["steps_executed"][0]["step_path"] == "steps[0]/repeat/iter[0]/steps[0]"
    assert rp["steps_executed"][-1]["step_path"] == "steps[0]/repeat/iter[2]/steps[1]"


@pytest.mark.asyncio
async def test_repeat_while_max_iterations_not_failed(monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    # drift = 3.0 > 0.01 は永遠に真 → max_iterations=4 で終了、failed にはしない
    res = await execute_recipe(_visa("3.0"), _session(), "repeat_while", {})
    assert res["success"] is True, res
    rp = res["steps_executed"][2]
    assert rp["iterations"] == 4
    assert rp["ended"] == "max_iterations"
    assert res["variables"]["vars"]["n_done"] == 4


@pytest.mark.asyncio
async def test_repeat_while_condition_false_immediately(monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    # drift = 0.001 <= 0.01 → 0 回で condition_false 終了
    res = await execute_recipe(_visa("0.001"), _session(), "repeat_while", {})
    assert res["success"] is True, res
    rp = res["steps_executed"][2]
    assert rp["iterations"] == 0
    assert rp["ended"] == "condition_false"


def test_repeat_while_requires_max_iterations():
    yaml_doc = yaml.safe_load(textwrap.dedent(SAMPLE_YAML))
    yaml_doc["recipes"]["bad_while"] = {
        "steps": [
            {"command": "meas", "result_as": "x"},
            {"repeat": {"while": "steps.x > 1",
                        "steps": [{"compute": {"set": "y", "expr": "1"}}]}},
        ],
    }
    with pytest.raises(Exception, match="max_iterations"):
        InstrumentDefinition(**yaml_doc)


def test_repeat_count_over_limit():
    yaml_doc = yaml.safe_load(textwrap.dedent(SAMPLE_YAML))
    yaml_doc["recipes"]["big"] = {
        "steps": [
            {"repeat": {"count": REPEAT_MAX_COUNT + 1,
                        "steps": [{"compute": {"set": "y", "expr": "1"}}]}},
        ],
    }
    defn = InstrumentDefinition(**yaml_doc)
    with pytest.raises(SeqExpressionError, match="上限"):
        recipe_to_plan(defn.recipes["big"], {}, definition=defn)


# ============================================================
# 3. guard
# ============================================================

@pytest.mark.asyncio
async def test_guard_pass_and_warn_continue(monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    # x = 50: guard1 (x < 10, warn) 偽 → 続行、guard2 (x < 100, abort) 真 → 成功
    res = await execute_recipe(_visa("50.0"), _session(), "guard_flow", {})
    assert res["success"] is True, res
    g1 = res["steps_executed"][1]
    assert g1["passed"] is False and g1["warned"] is True and g1["success"] is True
    g2 = res["steps_executed"][2]
    assert g2["passed"] is True


@pytest.mark.asyncio
async def test_guard_abort(monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    # x = 500: guard2 (x < 100, abort) 偽 → failed
    res = await execute_recipe(_visa("500.0"), _session(), "guard_flow", {})
    assert res["success"] is False
    failed = res["steps_executed"][-1]
    assert failed["error"] == "guard_failed"
    assert "大きすぎる" in failed["message"]


@pytest.mark.asyncio
async def test_guard_safe_shutdown(monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    res = await execute_recipe(_visa("3.0"), _session(), "guard_shutdown", {})
    assert res["success"] is False
    failed = res["steps_executed"][-1]
    assert failed["error"] == "guard_failed"
    assert failed["safe_shutdown"] is not None
    assert failed["safe_shutdown"]["attempted"] is True


def test_guard_on_fail_pause_rejected():
    yaml_doc = yaml.safe_load(textwrap.dedent(SAMPLE_YAML))
    yaml_doc["recipes"]["pause_guard"] = {
        "steps": [
            {"command": "meas", "result_as": "x"},
            {"guard": {"expr": "steps.x < 1", "on_fail": "pause"}},
        ],
    }
    # v2.30.0 (SP-4): スタンドアロン pause ステップは実装されたが、
    # guard.on_fail=pause は引き続き未対応 (代替手順を案内するメッセージに変更)。
    with pytest.raises(Exception, match="on_fail=pause"):
        InstrumentDefinition(**yaml_doc)


# ============================================================
# 4. deferred の非数値解決値 (SP-2 改善)
# ============================================================

@pytest.mark.asyncio
async def test_deferred_str_value_explicit_error(monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    res = await execute_recipe(_visa("3.0"), _session(), "deferred_str", {})
    assert res["success"] is False
    failed = res["steps_executed"][-1]
    assert failed["error"] == "deferred_resolve_failed"
    assert "数値である必要" in failed["message"]


# ============================================================
# 5. Job 経路
# ============================================================

@pytest.mark.asyncio
async def test_job_path_branch_repeat_guard_timeline(tmp_path, monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    from lab_executor.job import JobManager, JobStore
    from lab_executor.job.state_machine import JobStatus

    visa = _visa("3.0")
    session = _session()

    class _SM:
        def get_session(self, name):
            return session if name == "TEST::INSTR" else None

    store = JobStore(db_path=tmp_path / "sp3.sqlite")
    mgr = JobManager(visa, _SM(), store=store)
    try:
        # branch (case1 採択) + deferred
        rec1 = await mgr.start_recipe_job("TEST::INSTR", "branch_basic", {})
        # repeat while (max_iterations 到達)
        rec2 = await mgr.start_recipe_job("TEST::INSTR", "repeat_while", {})
        for rec in (rec1, rec2):
            for _ in range(100):
                cur = mgr.get(rec.job_id)
                if cur.status in (JobStatus.COMPLETED, JobStatus.FAILED):
                    break
                await asyncio.sleep(0.05)
        f1 = mgr.get(rec1.job_id)
        assert f1.status == JobStatus.COMPLETED, f1.result
        assert f1.result["variables"]["vars"]["c"] == 0.001
        e1 = {e["event_type"] for e in store.list_events(rec1.job_id)}
        assert "branch_taken" in e1
        assert "var_assigned" in e1
        assert "deferred_arg_resolved" in e1

        f2 = mgr.get(rec2.job_id)
        assert f2.status == JobStatus.COMPLETED, f2.result
        events2 = store.list_events(rec2.job_id)
        e2 = {e["event_type"] for e in events2}
        assert "repeat_ended" in e2
        ended = [e for e in events2 if e["event_type"] == "repeat_ended"][0]
        assert ended["payload"]["reason"] == "max_iterations"
        # guard (warn 相当ではなく pass) — guard_flow の warn ケースも確認
        rec3 = await mgr.start_recipe_job(
            "TEST::INSTR", "guard_flow", {"limit": 1.0},
        )
        for _ in range(100):
            cur = mgr.get(rec3.job_id)
            if cur.status in (JobStatus.COMPLETED, JobStatus.FAILED):
                break
            await asyncio.sleep(0.05)
        f3 = mgr.get(rec3.job_id)
        assert f3.status == JobStatus.COMPLETED, f3.result
        e3 = {e["event_type"] for e in store.list_events(rec3.job_id)}
        assert "guard_failed" in e3   # warn の guard_failed が timeline に載る
    finally:
        store.close()


# ============================================================
# 6. dry-run
# ============================================================

def test_dryrun_branch_all_cases_and_taken():
    plan = recipe_to_plan(_defn().recipes["branch_basic"], {}, definition=_defn())
    # test_values 無し: 全 case が展開され taken は無い
    view = dryrun_view(plan)
    br = view["steps"][1]
    assert br["type"] == "branch"
    assert len(br["cases"]) == 3
    assert br["cases"][2]["is_else"] is True
    assert "taken" not in br["cases"][0]
    # test_values あり: 採択 case が併記され、採択値が後続 deferred に流れる
    view2 = dryrun_view(plan, test_values={"steps.r": 3.0})
    br2 = view2["steps"][1]
    assert br2["taken_case"] == 1
    assert br2["cases"][0]["taken"] is False
    assert br2["cases"][1]["taken"] is True
    sc = view2["steps"][2]
    assert abs(sc["deferred_args"][0]["resolved"] - 0.001) < 1e-9
    assert sc["deferred_args"][0]["in_range"] is True


def test_dryrun_repeat_and_guard():
    plan = recipe_to_plan(
        _defn().recipes["repeat_count"], {"n": 3}, definition=_defn(),
    )
    view = dryrun_view(plan)
    rp = view["steps"][0]
    assert rp["type"] == "repeat"
    assert rp["count"] == 3
    assert len(rp["iterations"]) == 3          # 小さい count は展開表示
    assert rp["iterations"][0]["loop_index"] == 0

    # 大きい count は省略表示
    yaml_doc = yaml.safe_load(textwrap.dedent(SAMPLE_YAML))
    yaml_doc["recipes"]["big_ok"] = {
        "steps": [
            {"repeat": {"count": 100,
                        "steps": [{"compute": {"set": "y", "expr": "1"}}]}},
        ],
    }
    defn = InstrumentDefinition(**yaml_doc)
    plan_big = recipe_to_plan(defn.recipes["big_ok"], {}, definition=defn)
    view_big = dryrun_view(plan_big)
    rp_big = view_big["steps"][0]
    assert rp_big["iterations_omitted"] is True
    assert len(rp_big["body"]) == 1

    # guard の表示と test_values 判定 (default 適用済み変数で plan を作る —
    # _build_dryrun と同じ前提)
    plan_g = recipe_to_plan(
        _defn().recipes["guard_flow"], {"limit": 10.0}, definition=_defn(),
    )
    view_g = dryrun_view(plan_g, test_values={"steps.x": 50.0})
    g1 = view_g["steps"][1]
    assert g1["type"] == "guard"
    assert g1["on_fail"] == "warn"
    assert g1["passed"] is False
    g2 = view_g["steps"][2]
    assert g2["passed"] is True


# ============================================================
# 7. 後方互換
# ============================================================

@pytest.mark.asyncio
async def test_backward_compat_flat_recipe(monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    # SP-1/2 スタイルのフラットな recipe が従来通り動く
    yaml_doc = yaml.safe_load(textwrap.dedent(SAMPLE_YAML))
    yaml_doc["recipes"]["flat"] = {
        "parameters": [{"name": "c", "type": "float"}],
        "steps": [
            {"command": "set_current", "args": {"current": "$c"}},
            {"command": "meas", "result_as": "x"},
            {"compute": {"set": "y", "expr": "steps.x * 2"}},
        ],
    }
    defn = InstrumentDefinition(**yaml_doc)
    plan = recipe_to_plan(defn.recipes["flat"], {"c": 0.01}, definition=defn)
    assert isinstance(plan.steps[0], CommandStep)
    assert plan.steps[0].args["current"] == 0.01
    session = InstrumentSession(
        resource_name="TEST::INSTR", idn_response="<t>",
        idn_parsed={}, definition=defn,
    )
    res = await execute_recipe(_visa("2.5"), session, "flat", {"c": 0.01})
    assert res["success"] is True, res
    assert res["variables"]["vars"]["y"] == 5.0
