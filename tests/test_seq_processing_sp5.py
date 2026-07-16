"""SP-5: array 型 + repeat collect + NumPy 名前空間のテスト (v2.31.0)

対象:
- VariableStore の array 代入 / 要素数上限 / var_assigned・snapshot の要約形
- repeat collect (count / while / 空 / 複数 / 型混入エラー)
- 式言語の np.* (mean / polyfit / fft) / 明示allowlist / 0 次元スカラ化
- ndarray の deferred 拒否 (明示エラー)
- 条件式での ndarray 曖昧真偽値エラー
- dry-run (array test_values は list で与えて ndarray 化)
- 後方互換
"""
import asyncio
import textwrap
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest
import yaml

from lab_executor.experiment_ir import VariableStore
from lab_executor.experiment_ir.context import (
    VariableStoreError, summarize_array,
)
from lab_executor.models.instrument_def import InstrumentDefinition
from lab_executor.recipe_executor import execute_recipe, recipe_to_plan
from lab_executor.utils.seq_expression import (
    ARRAY_MAX_ELEMENTS, SeqExpressionError,
    evaluate, evaluate_condition, referenced_names,
)
from lab_executor.ui.views import dryrun_view
from visa_mcp.session_manager import InstrumentSession

RESOURCE = "TEST::INSTR"

SAMPLE_YAML = """
metadata:
  manufacturer: "Test"
  model: "Sp5Rig"
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
recipes:
  iv_collect:
    description: "repeat collect -> np 統計"
    parameters:
      - { name: "n", type: "integer", default: 4 }
    steps:
      - repeat:
          count: "$n"
          collect: { v: "vs" }
          steps:
            - { command: "meas", result_as: "v" }
      - compute: { set: "v_mean", expr: "np.mean(vars.vs)" }
      - compute: { set: "v_std", expr: "np.std(vars.vs)" }
      - guard: { expr: "vars.v_std < 10.0", on_fail: "warn" }
  collect_while_zero:
    description: "while 0 回 -> 空配列"
    steps:
      - { command: "meas", result_as: "drift" }
      - repeat:
          while: "steps.drift > 1000.0"
          max_iterations: 3
          collect: { d: "ds" }
          steps:
            - { command: "meas", result_as: "d" }
      - compute: { set: "n_pts", expr: "len(vars.ds)" }
  collect_multi:
    description: "複数 collect"
    steps:
      - repeat:
          count: 3
          collect: { a: "as_arr", b: "bs_arr" }
          steps:
            - { command: "meas", result_as: "a" }
            - compute: { set: "b", expr: "steps.a * 2" }
      - compute: { set: "total", expr: "np.sum(vars.as_arr) + np.sum(vars.bs_arr)" }
  collect_str_mix:
    description: "str 値の collect (エラーになるべき)"
    steps:
      - repeat:
          count: 2
          collect: { s: "ss" }
          steps:
            - compute: { set: "s", expr: "'ON' if env.loop_index == 0 else 'OFF'" }
  deferred_array:
    description: "ndarray の deferred 注入 (明示エラーになるべき)"
    steps:
      - repeat:
          count: 2
          collect: { v: "vs" }
          steps:
            - { command: "meas", result_as: "v" }
      - { command: "set_current", args: { current: "${vars.vs}" } }
  ambiguous_guard:
    description: "guard 条件の ndarray 曖昧真偽値"
    steps:
      - repeat:
          count: 2
          collect: { v: "vs" }
          steps:
            - { command: "meas", result_as: "v" }
      - guard: { expr: "vars.vs > 0", on_fail: "abort" }
"""


def _defn(extra: dict | None = None) -> InstrumentDefinition:
    doc = yaml.safe_load(textwrap.dedent(SAMPLE_YAML))
    if extra:
        doc["recipes"].update(extra)
    return InstrumentDefinition(**doc)


def _session(defn=None):
    return InstrumentSession(
        resource_name=RESOURCE,
        idn_response="<test>",
        idn_parsed={"manufacturer": "Test", "model": "Sp5Rig"},
        definition=defn or _defn(),
    )


def _visa(returns):
    v = MagicMock()
    v.write = AsyncMock(return_value=None)
    if isinstance(returns, list):
        v.query = AsyncMock(side_effect=returns)
    else:
        v.query = AsyncMock(return_value=returns)
    return v


_CTX = lambda **kw: {  # noqa: E731 - テスト用の簡易 ctx
    "params": {}, "steps": {}, "vars": dict(kw), "env": {},
}


# ============================================================
# 1. 式言語: np 名前空間
# ============================================================

def test_np_functions():
    ctx = _CTX(xs=np.array([1.0, 2.0, 3.0]), ys=np.array([2.0, 4.0, 6.0]))
    assert evaluate("np.mean(vars.xs)", ctx) == 2.0
    coef = evaluate("np.polyfit(vars.xs, vars.ys, 1)", ctx)
    assert isinstance(coef, np.ndarray)
    assert abs(coef[0] - 2.0) < 1e-9
    spec = evaluate("np.fft.rfft(vars.xs)", ctx)
    assert isinstance(spec, np.ndarray)
    # 要素演算も ndarray を返す
    out = evaluate("vars.xs * 2 + 1", ctx)
    assert isinstance(out, np.ndarray)
    assert out.tolist() == [3.0, 5.0, 7.0]
    # np.pi 等の定数参照
    assert abs(evaluate("np.pi", ctx) - 3.14159265) < 1e-6


def test_np_scalar_and_0dim_to_python_scalar():
    ctx = _CTX(xs=np.array([1.0, 2.0, 3.0]))
    v = evaluate("np.sum(vars.xs)", ctx)
    assert type(v) is float and v == 6.0
    v2 = evaluate("np.mean(vars.xs) + 1", ctx)
    assert type(v2) is float


def test_np_allowlist_rejects_side_effects_and_allocators():
    ctx = _CTX()
    for expr in (
        "np.load('x.npy')", "np.save('x.npy', 1)", "np.loadtxt('x.txt')",
        "np.fromfile('x')", "np.memmap('x')", "np.DataSource()",
        "np.lib.npyio", "np.vectorize(abs)", "np.frompyfunc(abs, 1, 1)",
        "np.apply_along_axis(abs, 0, 1)",
        # 任意native code load。結果型を拒否してもload時副作用は既に起きる。
        "np.ctypeslib.load_library('evil.dll', '.')",
        # 結果size検査より前に巨大allocateできるためconstructorは許可しない。
        "np.zeros(10)", "np.ones(10)", "np.arange(10)",
        # global state mutation / 非決定的名前空間も許可しない。
        "np.seterr('ignore')", "np.random.seed(1)",
    ):
        with pytest.raises(SeqExpressionError):
            evaluate(expr, ctx)
    # 3 段属性は拒否
    with pytest.raises(SeqExpressionError):
        evaluate("np.fft.helper.fftfreq(4)", ctx)
    # dunder 禁止
    with pytest.raises(SeqExpressionError):
        evaluate("np.__loader__", ctx)
    # list リテラルは引き続き禁止 (値は collect / np 関数から来る)
    with pytest.raises(SeqExpressionError):
        evaluate("np.array([1, 2])", ctx)


def test_np_allocator_is_rejected_before_numpy_invocation(monkeypatch):
    """Result-size checks are too late if an allocator has already run."""
    invoked = False

    def forbidden_allocator(*args, **kwargs):
        nonlocal invoked
        invoked = True
        raise AssertionError("NumPy allocator must not be invoked")

    monkeypatch.setattr(np, "zeros", forbidden_allocator)
    with pytest.raises(SeqExpressionError, match="allowlist"):
        evaluate("np.zeros(1000000000000)", _CTX())
    assert invoked is False


def test_np_allowlist_documented_calculations_still_work():
    ctx = _CTX(xs=np.array([1.0, 2.0, 3.0]))
    assert evaluate("np.mean(vars.xs)", ctx) == 2.0
    assert evaluate("np.std(vars.xs)", ctx) == pytest.approx(np.std(ctx["vars"]["xs"]))
    assert evaluate("np.sum(vars.xs)", ctx) == 6.0
    assert evaluate("np.all(vars.xs > 0)", ctx) is True
    fft = evaluate("np.fft.rfft(vars.xs)", ctx)
    assert isinstance(fft, np.ndarray)


def test_np_not_a_variable_reference():
    # np.* は参照名として数えない (コンパイル検証を通る)
    assert referenced_names("np.mean(vars.xs)") == {"vars.xs"}


def test_ambiguous_truth_value_is_clear_error():
    ctx = _CTX(xs=np.array([1.0, 2.0]))
    with pytest.raises(SeqExpressionError, match="曖昧"):
        evaluate_condition("vars.xs > 1", ctx)
    with pytest.raises(SeqExpressionError, match="曖昧"):
        evaluate("vars.xs and 1", ctx)
    # np.all で集約すれば OK
    assert evaluate_condition("np.all(vars.xs > 0)", ctx) is True


# ============================================================
# 2. VariableStore の array
# ============================================================

def test_variable_store_array_and_summary():
    s = VariableStore(params={}, env={})
    arr = np.array([1.0, 2.0, 3.0])
    s.set_var("xs", arr, source_step_path="steps[0]", expr="collect(v)")
    # 本体は ctx に残る (式評価用)
    assert isinstance(s.as_ctx()["vars"]["xs"], np.ndarray)
    # イベント / スナップショットは要約形 (本体を入れない)
    ev = s.events[-1]["value"]
    assert ev["__type__"] == "array"
    assert ev["shape"] == [3] and ev["size"] == 3
    assert ev["head"] == [1.0, 2.0, 3.0]
    assert ev["mean"] == 2.0 and ev["min"] == 1.0 and ev["max"] == 3.0
    snap = s.snapshot()["vars"]["xs"]
    assert snap["__type__"] == "array"
    # np スカラは Python スカラへ昇格
    s.set_var("m", np.float64(1.5), source_step_path="x")
    assert type(s.as_ctx()["vars"]["m"]) is float


def test_variable_store_array_size_limit():
    s = VariableStore(params={}, env={})
    big = np.zeros(ARRAY_MAX_ELEMENTS + 1, dtype=np.int8)
    with pytest.raises(VariableStoreError, match="上限"):
        s.set_var("big", big, source_step_path="x")


# ============================================================
# 3. repeat collect
# ============================================================

@pytest.mark.asyncio
async def test_collect_count_and_np_stats(monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    visa = _visa(["1.0", "2.0", "3.0", "4.0"])
    res = await execute_recipe(visa, _session(), "iv_collect", {"n": 4})
    assert res["success"] is True, res
    rp = res["steps_executed"][0]
    assert rp["collected"]["vs"]["size"] == 4
    assert rp["collected"]["vs"]["head"] == [1.0, 2.0, 3.0, 4.0]
    # スナップショットも要約形、np 統計は Python float
    assert res["variables"]["vars"]["vs"]["__type__"] == "array"
    assert res["variables"]["vars"]["v_mean"] == 2.5
    assert abs(res["variables"]["vars"]["v_std"] - np.std([1, 2, 3, 4])) < 1e-9


@pytest.mark.asyncio
async def test_collect_while_zero_iterations_empty_array(monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    visa = _visa("3.0")   # drift=3.0 <= 1000 → 0 回
    res = await execute_recipe(visa, _session(), "collect_while_zero", {})
    assert res["success"] is True, res
    rp = res["steps_executed"][1]
    assert rp["iterations"] == 0 and rp["ended"] == "condition_false"
    assert rp["collected"]["ds"]["size"] == 0
    # 空配列でも定義済み → len() が使える
    assert res["variables"]["vars"]["n_pts"] == 0


@pytest.mark.asyncio
async def test_collect_multiple_targets(monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    visa = _visa(["1.0", "2.0", "3.0"])
    res = await execute_recipe(visa, _session(), "collect_multi", {})
    assert res["success"] is True, res
    # as = [1,2,3], bs = [2,4,6] → total = 6 + 12 = 18
    assert res["variables"]["vars"]["total"] == 18.0


@pytest.mark.asyncio
async def test_collect_str_value_is_error(monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    res = await execute_recipe(_visa("1.0"), _session(), "collect_str_mix", {})
    assert res["success"] is False
    failed = res["steps_executed"][-1]
    assert failed["error"] == "collect_failed"
    assert "数値" in failed["message"]


def test_collect_source_must_be_defined_in_body():
    defn = _defn({
        "bad_collect": {
            "steps": [
                {"command": "meas", "result_as": "outside"},
                {"repeat": {
                    "count": 2,
                    "collect": {"outside": "arr"},   # body 内で定義されない
                    "steps": [{"compute": {"set": "x", "expr": "1"}}],
                }},
            ],
        },
    })
    with pytest.raises(SeqExpressionError, match="body 内で"):
        recipe_to_plan(defn.recipes["bad_collect"], {}, definition=defn)


# ============================================================
# 4. deferred / 条件式との関係
# ============================================================

@pytest.mark.asyncio
async def test_deferred_array_rejected(monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    visa = _visa(["1.0", "2.0"])
    res = await execute_recipe(visa, _session(), "deferred_array", {})
    assert res["success"] is False
    failed = res["steps_executed"][-1]
    assert failed["error"] == "deferred_resolve_failed"
    assert "数値である必要" in failed["message"]


@pytest.mark.asyncio
async def test_guard_ambiguous_array_condition_fails_clearly(monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    visa = _visa(["1.0", "2.0"])
    res = await execute_recipe(visa, _session(), "ambiguous_guard", {})
    assert res["success"] is False
    failed = res["steps_executed"][-1]
    assert failed["error"] == "guard_error"
    assert "曖昧" in failed["message"]


# ============================================================
# 5. dry-run
# ============================================================

def test_dryrun_array_test_values_as_list():
    defn = _defn({
        "analysis": {
            "steps": [
                {"repeat": {
                    "count": 3,
                    "collect": {"v": "vs"},
                    "steps": [{"command": "meas", "result_as": "v"}],
                }},
                {"compute": {"set": "m", "expr": "np.mean(vars.vs)"}},
                {"guard": {"expr": "np.all(vars.vs > 0)", "on_fail": "warn"}},
            ],
        },
    })
    plan = recipe_to_plan(defn.recipes["analysis"], {}, definition=defn)
    # collect 宣言が表示される
    view0 = dryrun_view(plan)
    assert view0["steps"][0]["collect"] == {"v": "vs"}
    # array の test_values は list で与える → ndarray 化されて np 式が評価できる
    view = dryrun_view(plan, test_values={"vars.vs": [1.0, 2.0, 3.0]})
    comp = view["steps"][1]
    assert comp["value"] == 2.0
    g = view["steps"][2]
    assert g["passed"] is True
    import json
    json.dumps(view)   # JSON 直列化可能 (ndarray が漏れていない)


# ============================================================
# 6. 後方互換
# ============================================================

@pytest.mark.asyncio
async def test_backward_compat_scalar_paths(monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    defn = _defn({
        "legacy": {
            "parameters": [{"name": "c", "type": "float"}],
            "steps": [
                {"command": "set_current", "args": {"current": "$c"}},
                {"command": "meas", "result_as": "x"},
                {"compute": {"set": "y", "expr": "steps.x * 2"}},
            ],
        },
    })
    res = await execute_recipe(
        _visa("2.5"), _session(defn), "legacy", {"c": 0.01},
    )
    assert res["success"] is True, res
    assert res["variables"]["vars"]["y"] == 5.0
    # スカラは要約形にならずそのまま
    assert isinstance(res["variables"]["vars"]["y"], float)
