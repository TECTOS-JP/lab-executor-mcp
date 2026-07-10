"""SP-1 / SP-2: 統合式評価器・capture/compute・${...} 実行時解決のテスト (v2.28.0)

対象:
- utils/seq_expression: 算術/比較連鎖/論理短絡/三項/関数/名前空間/裸名/拒否/数値健全性
- experiment_ir/context: VariableStore
- capture 拡張 (value_path / 寛容抽出 / 抽出失敗 failed / unit)
- compute (正常 / 前方参照コンパイルエラー / 実行時 on_error)
- ${...} deferred (検出 / 範囲宣言必須 / 実行時範囲執行)
- Job 経路 (変数スナップショット + var_assigned timeline)
- dry-run 拡張 (deferred 表示 / test_values 解決)
- 後方互換 (既存レシピの recipe_to_plan 結果不変)
"""
import asyncio
import textwrap
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from lab_executor.models.instrument_def import InstrumentDefinition
from lab_executor.recipe_executor import execute_recipe, recipe_to_plan
from lab_executor.experiment_ir import CommandStep, ComputeStep, VariableStore
from lab_executor.experiment_ir.context import VariableStoreError
from lab_executor.utils.seq_expression import (
    SeqExpressionError, evaluate, evaluate_condition, referenced_names, parse_deferred,
)
from lab_executor.ui.views import dryrun_view
from visa_mcp.session_manager import InstrumentSession


# ============================================================
# 共通 YAML / セッション
# ============================================================

SAMPLE_YAML = """
metadata:
  manufacturer: "Test"
  model: "SeqRig"
  category: "smu"
response_formats:
  num:
    fallback: "numeric_extract"
commands:
  reset:
    scpi: "*RST"
    type: "write"
  measure_thickness:
    scpi: "MEAS:THICK?"
    type: "query"
    returns: { type: "float", format: "num" }
  measure_res:
    scpi: "MEAS:RES?"
    type: "query"
    returns: { type: "float", format: "num" }
  set_current:
    scpi: "CURR {current}"
    type: "write"
    parameters:
      - { name: current, type: float, range: [0.0, 0.02] }
  set_bias:
    scpi: "BIAS {bias}"
    type: "write"
    parameters:
      - { name: bias, type: float }
  set_output:
    scpi: "OUTP {state}"
    type: "write"
    parameters:
      - { name: state, type: enum, choices: ["ON", "OFF"] }
safe_shutdown:
  - { command: "set_current", args: { current: 0.0 } }
  - { command: "set_output", args: { state: "OFF" } }
recipes:
  legacy:
    description: "既存互換レシピ ($ 式のみ)"
    parameters:
      - { name: target_c, type: float }
    steps:
      - { command: "reset" }
      - { command: "set_current", args: { current: "$target_c" } }
  cap_compute:
    description: "capture -> compute"
    steps:
      - { command: "measure_thickness", result_as: "thickness", unit: "nm" }
      - { command: "measure_res", result_as: "sheet_res", unit: "ohm/sq" }
      - compute: { set: "resistivity", expr: "steps.sheet_res * steps.thickness * 1e-3", unit: "ohm.m" }
  cap_value_path:
    description: "value_path capture"
    steps:
      - { command: "measure_thickness", result_as: "thickness", value_path: "parsed.value_numeric" }
  deferred_ok:
    description: "compute -> deferred set_current (ParameterDefinition.range)"
    steps:
      - { command: "measure_res", result_as: "sheet_res" }
      - compute: { set: "meas_c", expr: "0.001 if steps.sheet_res > 5 else 0.01" }
      - { command: "set_current", args: { current: "${vars.meas_c}" } }
  deferred_requires:
    description: "requires.ranges だけで宣言"
    requires:
      commands: [set_bias]
      ranges: { "set_bias.bias": { min: 0.0, max: 5.0 } }
    steps:
      - { command: "measure_res", result_as: "sheet_res" }
      - { command: "set_bias", args: { bias: "${steps.sheet_res}" } }
  deferred_no_range:
    description: "範囲宣言の無い deferred (コンパイルエラーになるべき)"
    steps:
      - { command: "measure_res", result_as: "sheet_res" }
      - { command: "set_bias", args: { bias: "${steps.sheet_res}" } }
  compute_forward_ref:
    description: "前方参照 compute (コンパイルエラー)"
    steps:
      - compute: { set: "x", expr: "vars.y + 1" }
      - compute: { set: "y", expr: "2" }
  compute_runtime_err:
    description: "実行時エラー compute (safe_shutdown)"
    steps:
      - { command: "measure_res", result_as: "sheet_res" }
      - compute: { set: "bad", expr: "steps.sheet_res / 0", on_error: "safe_shutdown" }
  deferred_out_of_range:
    description: "実行時範囲外 -> range_violation"
    steps:
      - { command: "measure_res", result_as: "sheet_res" }
      - compute: { set: "big", expr: "steps.sheet_res * 100.0" }
      - { command: "set_current", args: { current: "${vars.big}" } }
  capture_fail:
    description: "capture 抽出失敗"
    steps:
      - { command: "reset", result_as: "nothing" }
"""


def _defn() -> InstrumentDefinition:
    return InstrumentDefinition(**yaml.safe_load(textwrap.dedent(SAMPLE_YAML)))


def _session():
    return InstrumentSession(
        resource_name="TEST::INSTR",
        idn_response="<test>",
        idn_parsed={"manufacturer": "Test", "model": "SeqRig"},
        definition=_defn(),
    )


def _visa(query_return="3.0"):
    v = MagicMock()
    v.write = AsyncMock(return_value=None)
    v.query = AsyncMock(return_value=query_return)
    return v


# ============================================================
# 1. 統合式評価器
# ============================================================

def test_arithmetic_and_namespaces():
    ctx = {"params": {"b": 1}, "steps": {"a": 3}, "vars": {}, "env": {}}
    assert evaluate("steps.a * 2 + params.b", ctx) == 7
    assert evaluate("(steps.a + params.b) ** 2", ctx) == 16


def test_chained_compare_and_bool_shortcircuit():
    ctx = {"params": {}, "steps": {"a": 5}, "vars": {}, "env": {}}
    assert evaluate_condition("1 < steps.a < 10", ctx) is True
    assert evaluate_condition("steps.a > 100 or steps.a == 5", ctx) is True
    assert evaluate_condition("not (steps.a == 5)", ctx) is False


def test_ternary_and_functions():
    ctx = {"params": {}, "steps": {"x": 15}, "vars": {"r": 3}, "env": {}}
    assert evaluate("1.0 if vars.r < 5 else 2.0", ctx) == 1.0
    assert evaluate("clamp(steps.x, 0, 10)", ctx) == 10
    assert evaluate("min(steps.x, 4, 9)", ctx) == 4
    assert abs(evaluate("sqrt(steps.x + 1)", ctx) - 4.0) < 1e-9


def test_bare_name_fallback():
    # 裸名は params -> vars -> steps の順で解決 (後方互換)
    ctx = {"params": {"p": 10}, "steps": {"s": 1}, "vars": {"v": 2}, "env": {}}
    assert evaluate("p + v + s", ctx) == 13


def test_reject_dangerous_nodes():
    ctx = {"params": {}, "steps": {"a": 1.0}, "vars": {}, "env": {}}
    for expr in ("steps.a.real", "foo.__class__", "unknownf(1)", "[1,2]",
                 "steps.a[0]", "lambda: 1"):
        with pytest.raises(SeqExpressionError):
            evaluate(expr, ctx)


def test_numeric_health():
    ctx = {"params": {}, "steps": {"a": 1.0}, "vars": {}, "env": {}}
    with pytest.raises(SeqExpressionError):
        evaluate("steps.a / 0", ctx)
    with pytest.raises(SeqExpressionError):
        evaluate("1e308 * 1e308", ctx)          # inf
    with pytest.raises(SeqExpressionError):
        evaluate("'x' + steps.a", ctx)          # str と数値混在


def test_referenced_names():
    refs = referenced_names("steps.a + vars.b * foo + params.c")
    assert refs == {"steps.a", "vars.b", "params.c", "foo"}
    # 関数名・名前空間名は含まれない
    assert referenced_names("abs(steps.a)") == {"steps.a"}


# ============================================================
# 2. VariableStore
# ============================================================

def test_variable_store_basics():
    s = VariableStore(params={"p": 1}, env={"job_id": "j1"})
    s.set_step("thickness", 100.0, source_step_path="steps[0]", unit="nm")
    s.set_var("r", 2.0, source_step_path="steps[1]", expr="1+1", unit="ohm.m")
    ctx = s.as_ctx()
    assert ctx["steps"]["thickness"] == 100.0
    assert ctx["vars"]["r"] == 2.0
    assert ctx["params"]["p"] == 1
    assert ctx["env"]["job_id"] == "j1"
    assert s.snapshot() == {"steps": {"thickness": 100.0}, "vars": {"r": 2.0}}
    assert s.events[-1]["unit"] == "ohm.m"
    assert s.events[0]["namespace"] == "steps"


def test_variable_store_rejects_bad_names_and_types():
    s = VariableStore(params={}, env={})
    with pytest.raises(VariableStoreError):
        s.set_var("Bad", 1, source_step_path="x")     # 大文字
    with pytest.raises(VariableStoreError):
        s.set_var("params", 1, source_step_path="x")  # 予約語
    with pytest.raises(TypeError):
        s.set_var("ok", [1, 2], source_step_path="x")  # array は SP-5


# ============================================================
# 3. capture
# ============================================================

@pytest.mark.asyncio
async def test_capture_lenient_and_compute(monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    visa = _visa("3.0")   # measure_* は 3.0 を返す
    res = await execute_recipe(visa, _session(), "cap_compute", {})
    assert res["success"] is True, res
    # steps.sheet_res = 3.0, steps.thickness = 3.0 -> 3*3*1e-3 = 0.009
    assert abs(res["variables"]["vars"]["resistivity"] - 0.009) < 1e-9
    assert res["variables"]["steps"]["thickness"] == 3.0


@pytest.mark.asyncio
async def test_capture_value_path(monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    visa = _visa("42.5")
    res = await execute_recipe(visa, _session(), "cap_value_path", {})
    assert res["success"] is True, res
    assert res["variables"]["steps"]["thickness"] == 42.5


@pytest.mark.asyncio
async def test_capture_failure_is_step_failed(monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    # reset は write でparsed 無し -> 寛容抽出も失敗 -> capture_failed
    visa = _visa()
    res = await execute_recipe(visa, _session(), "capture_fail", {})
    assert res["success"] is False
    assert res["steps_executed"][-1]["error"] == "capture_failed"


# ============================================================
# 4. compute
# ============================================================

def test_compute_forward_reference_is_compile_error():
    recipe = _defn().recipes["compute_forward_ref"]
    with pytest.raises(SeqExpressionError):
        recipe_to_plan(recipe, {}, definition=_defn())


@pytest.mark.asyncio
async def test_compute_runtime_error_triggers_safe_shutdown(monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    visa = _visa("3.0")
    res = await execute_recipe(visa, _session(), "compute_runtime_err", {})
    assert res["success"] is False
    failed = res["steps_executed"][-1]
    assert failed["error"] == "compute_error"
    # safe_shutdown が実行された (yaml 定義: set_current 0 + set_output OFF)
    assert failed["safe_shutdown"] is not None
    assert failed["safe_shutdown"]["attempted"] is True


# ============================================================
# 5. deferred (${...})
# ============================================================

def test_deferred_detected_and_kept():
    plan = recipe_to_plan(_defn().recipes["deferred_ok"], {}, definition=_defn())
    cmd = [s for s in plan.steps if isinstance(s, CommandStep) and s.command == "set_current"][0]
    assert "current" in cmd.deferred_args
    assert cmd.deferred_args["current"]["expr"] == "vars.meas_c"
    assert cmd.deferred_args["current"]["min"] == 0.0
    assert cmd.deferred_args["current"]["max"] == 0.02
    # deferred は args に含めない
    assert "current" not in cmd.args


def test_deferred_missing_range_is_compile_error():
    with pytest.raises(SeqExpressionError):
        recipe_to_plan(_defn().recipes["deferred_no_range"], {}, definition=_defn())


def test_deferred_requires_ranges_only_ok():
    plan = recipe_to_plan(_defn().recipes["deferred_requires"], {}, definition=_defn())
    cmd = [s for s in plan.steps if isinstance(s, CommandStep) and s.command == "set_bias"][0]
    assert cmd.deferred_args["bias"]["min"] == 0.0
    assert cmd.deferred_args["bias"]["max"] == 5.0


@pytest.mark.asyncio
async def test_deferred_runtime_resolution_ok(monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    visa = _visa("3.0")   # sheet_res=3 -> meas_c = 0.01 (<=5 branch false -> 0.01)
    res = await execute_recipe(visa, _session(), "deferred_ok", {})
    assert res["success"] is True, res
    # set_current が resolved 0.01 で送信されたか
    sc = [s for s in res["steps_executed"] if s.get("command") == "set_current"][0]
    assert abs(float(sc["args"]["current"]) - 0.01) < 1e-9


@pytest.mark.asyncio
async def test_deferred_out_of_range_violation(monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    visa = _visa("3.0")   # big = 300 -> 範囲 [0, 0.02] 外
    res = await execute_recipe(visa, _session(), "deferred_out_of_range", {})
    assert res["success"] is False
    failed = res["steps_executed"][-1]
    assert failed["error"] == "range_violation"
    assert failed["safe_shutdown"] is not None


# ============================================================
# 6. Job 経路
# ============================================================

@pytest.mark.asyncio
async def test_job_path_variables_and_timeline(tmp_path, monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    from lab_executor.job import JobManager, JobStore
    from lab_executor.job.state_machine import JobStatus

    visa = _visa("3.0")
    session = _session()

    class _SM:
        def get_session(self, name):
            return session if name == "TEST::INSTR" else None

    store = JobStore(db_path=tmp_path / "seq.sqlite")
    mgr = JobManager(visa, _SM(), store=store)
    try:
        rec = await mgr.start_recipe_job("TEST::INSTR", "deferred_ok", {})
        for _ in range(60):
            cur = mgr.get(rec.job_id)
            if cur.status in (JobStatus.COMPLETED, JobStatus.FAILED):
                break
            await asyncio.sleep(0.05)
        final = mgr.get(rec.job_id)
        assert final.status == JobStatus.COMPLETED, final.result
        assert final.result["variables"]["vars"]["meas_c"] == 0.01
        assert final.result["variables"]["steps"]["sheet_res"] == 3.0
        # var_assigned / deferred_arg_resolved が timeline に載る
        events = store.list_events(rec.job_id)
        etypes = {e["event_type"] for e in events}
        assert "var_assigned" in etypes
        assert "deferred_arg_resolved" in etypes
    finally:
        store.close()


# ============================================================
# 7. dry-run
# ============================================================

def test_dryrun_shows_deferred_without_test_values():
    plan = recipe_to_plan(_defn().recipes["deferred_ok"], {}, definition=_defn())
    view = dryrun_view(plan)
    sc = [s for s in view["steps"] if s["command"] == "set_current"][0]
    assert sc["deferred_args"][0]["arg"] == "current"
    assert sc["deferred_args"][0]["resolved"] == "deferred"
    assert sc["deferred_args"][0]["range_declared"] is True


def test_dryrun_resolves_test_values():
    plan = recipe_to_plan(_defn().recipes["deferred_ok"], {}, definition=_defn())
    view = dryrun_view(plan, test_values={"steps.sheet_res": 3.0})
    # compute meas_c = 0.01 (branch)
    comp = [s for s in view["steps"] if s["type"] == "compute"][0]
    assert abs(comp["value"] - 0.01) < 1e-9
    sc = [s for s in view["steps"] if s["command"] == "set_current"][0]
    assert abs(sc["deferred_args"][0]["resolved"] - 0.01) < 1e-9
    assert sc["deferred_args"][0]["in_range"] is True


# ============================================================
# 8. validation lint + 後方互換
# ============================================================

def test_validate_flags_missing_range(tmp_path):
    from lab_executor.registry import validate_instrument_file
    # deferred_no_range を含む定義を書き出して検証
    p = tmp_path / "rig.yaml"
    p.write_text(textwrap.dedent(SAMPLE_YAML), encoding="utf-8")
    rep = validate_instrument_file(p)
    classes = {e["error_class"] for e in rep.errors}
    assert "recipe_deferred_arg_missing_range" in classes
    assert rep.status == "error"


@pytest.mark.asyncio
async def test_backward_compat_legacy_recipe(monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    visa = _visa()
    # 既存 $ 式レシピは従来通り解決される
    plan = recipe_to_plan(_defn().recipes["legacy"], {"target_c": 0.005}, definition=_defn())
    cmd = [s for s in plan.steps if isinstance(s, CommandStep) and s.command == "set_current"][0]
    assert cmd.args["current"] == 0.005
    assert cmd.deferred_args == {}
    res = await execute_recipe(visa, _session(), "legacy", {"target_c": 0.005})
    assert res["success"] is True
