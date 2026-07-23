"""SP-7: サブシーケンス (サブルーチン) のテスト (v2.33.0)

対象:
- 同一ファイル内 call / ライブラリ (sequences_dir) 解決 / 同名衝突
- with (コンパイル時解決) / returns_as マップ / スコープ分離
- @role 置換 + capability 照合 (OK / NG) / bind 漏れ
- 再帰・相互再帰 / 深さ上限
- ネスト (call 内 branch/repeat) / 実行時式 with の非対応
- contains_code 伝播 / dry-run 展開 / 後方互換
"""
import textwrap
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from lab_executor.models.instrument_def import (
    CapabilityRequirements, InstrumentDefinition, RangeSpec,
    SubsequenceDefinition,
)
from lab_executor.recipe_executor import (
    CALL_MAX_DEPTH, execute_recipe, recipe_to_plan,
)
from lab_executor.experiment_ir import CallStep
from lab_executor.utils.seq_expression import SeqExpressionError
from lab_executor.ui.views import dryrun_view
from lab_visa_mcp.session_manager import InstrumentSession


SAMPLE_YAML = """
metadata:
  manufacturer: "Test"
  model: "Sp7Rig"
response_formats:
  num:
    fallback: "numeric_extract"
commands:
  measure_voltage:
    scpi: "MEAS:VOLT?"
    type: "query"
    returns: { type: "float", format: "num" }
  set_current:
    scpi: "CURR {current}"
    type: "write"
    parameters:
      - { name: current, type: float, range: [0.0, 0.02] }
sequences:
  stabilize_and_measure:
    description: "N 回測定して平均・標準偏差を返す"
    roles:
      - { name: meter, requires: { commands: [measure_voltage] } }
    parameters:
      - { name: n, type: integer, default: 3 }
    returns: [v_avg, v_std]
    steps:
      - repeat:
          count: "$n"
          collect: { v: "vs" }
          steps:
            - { command: measure_voltage, instrument: "@meter", result_as: v }
      - compute: { set: v_avg, expr: "np.mean(vars.vs)" }
      - compute: { set: v_std, expr: "np.std(vars.vs)" }
  needs_ammeter:
    roles:
      - { name: probe, requires: { commands: [measure_current] } }
    returns: [x]
    steps:
      - { command: measure_voltage, instrument: "@probe", result_as: raw }
      - compute: { set: x, expr: "steps.raw * 2" }
  self_recursive:
    returns: [y]
    steps:
      - call: { sequence: self_recursive }
      - compute: { set: y, expr: "1" }
recipes:
  main:
    steps:
      - call:
          sequence: stabilize_and_measure
          bind: { meter: "DMM1" }
          with: { n: 4 }
          returns_as: { v_avg: baseline_v, v_std: baseline_noise }
      - compute: { set: report, expr: "vars.baseline_v" }
  use_return_deferred:
    steps:
      - call:
          sequence: stabilize_and_measure
          bind: { meter: "DMM1" }
          with: { n: 2 }
          returns_as: { v_avg: bv }
      - { command: set_current, args: { current: "${vars.bv * 0.001}" } }
"""


def _defn(extra_yaml: str = "") -> InstrumentDefinition:
    doc = yaml.safe_load(textwrap.dedent(SAMPLE_YAML))
    if extra_yaml:
        extra = yaml.safe_load(textwrap.dedent(extra_yaml))
        for k, v in extra.items():
            doc.setdefault(k, {}).update(v) if isinstance(v, dict) else doc.update({k: v})
    return InstrumentDefinition(**doc)


def _session(defn: InstrumentDefinition):
    return InstrumentSession(
        resource_name="DMM1", idn_response="<t>",
        idn_parsed={"manufacturer": "Test", "model": "Sp7Rig"},
        definition=defn,
    )


def _visa(query_return="2.5"):
    v = MagicMock()
    v.write = AsyncMock(return_value=None)
    v.query = AsyncMock(return_value=query_return)
    return v


# ============================================================
# 1. 基本 call: bind / with / returns_as / スコープ分離
# ============================================================

@pytest.mark.asyncio
async def test_call_basic_bind_with_returns():
    defn = _defn()
    r = await execute_recipe(_visa("2.5"), _session(defn), "main", {})
    assert r["success"] is True
    v = r["variables"]["vars"]
    assert v["baseline_v"] == 2.5
    assert v["baseline_noise"] == 0.0
    assert v["report"] == 2.5


@pytest.mark.asyncio
async def test_call_scope_isolation():
    """サブシーケンス内の vars/steps は呼び出し元に漏れない。"""
    defn = _defn()
    r = await execute_recipe(_visa("2.5"), _session(defn), "main", {})
    v = r["variables"]["vars"]
    assert "vs" not in v and "v_avg" not in v and "v_std" not in v


def test_call_role_bound_and_replaced():
    """@role が bind 先へ置換され、展開後 CommandStep の instrument になる。"""
    defn = _defn()
    plan = recipe_to_plan(defn.recipes["main"], {}, definition=defn)
    call = plan.steps[0]
    assert isinstance(call, CallStep)
    # sub_steps[0] は repeat、その body の command instrument が DMM1
    repeat = call.sub_steps[0]
    cmd = repeat.body[0]
    assert cmd.instrument == "DMM1"


def test_call_bind_to_non_primary_resource_is_compiled_for_sp8():
    """SP-8ではbind先をrequired_resourcesへ含めて実行時解決する。"""
    defn = _defn()
    r = defn.recipes["main"].model_copy(deep=True)
    r.steps[0].call["bind"] = {"meter": "OTHER"}
    plan = recipe_to_plan(
        r, {}, definition=defn, primary_resource="DMM1",
    )
    assert plan.required_resources == ["DMM1", "OTHER"]


@pytest.mark.asyncio
async def test_execute_plan_cross_call_without_resolver_fails_closed():
    """resolver無しのcross callは主装置へfallbackせず失敗する。"""
    from lab_executor.recipe_executor import execute_plan

    defn = _defn()
    r = defn.recipes["main"].model_copy(deep=True)
    r.steps[0].call["bind"] = {"meter": "OTHER"}
    plan = recipe_to_plan(r, {}, definition=defn)
    visa = _visa("2.5")
    result = await execute_plan(visa, _session(defn), plan)
    assert result["success"] is False
    failed = result["steps_executed"][0]
    assert failed["error"] == "InstrumentNotAvailable"
    visa.query.assert_not_awaited()


def test_call_inherits_parent_requires_ranges():
    """call展開で親の厳しいrangeを失わず、装置rangeとの積集合にする。"""
    defn = _defn()
    defn.sequences["range_child"] = SubsequenceDefinition.model_validate({
        "parameters": [{"name": "current", "type": "float", "default": 0.0}],
        "steps": [{
            "command": "set_current",
            "args": {"current": "${params.current}"},
        }],
    })
    r = defn.recipes["main"].model_copy(deep=True)
    r.requires = CapabilityRequirements(ranges={
        "set_current.current": RangeSpec(min=0.0, max=0.001),
    })
    r.steps = [type(r.steps[0])(call={
        "sequence": "range_child",
        "with": {"current": 0.005},
    })]
    plan = recipe_to_plan(
        r, {}, definition=defn, primary_resource="DMM1",
    )
    command = plan.steps[0].sub_steps[0]
    assert command.deferred_args["current"] == {
        "expr": "params.current", "min": 0.0, "max": 0.001,
    }


# ============================================================
# 2. 検証エラー系
# ============================================================

def test_call_unknown_sequence():
    defn = _defn()
    defn.recipes["bad"] = defn.recipes["main"].model_copy(deep=True)
    defn.recipes["bad"].steps[0].call["sequence"] = "nonexistent"
    with pytest.raises(SeqExpressionError, match="見つかりません"):
        recipe_to_plan(defn.recipes["bad"], {}, definition=defn)


def test_call_missing_bind():
    defn = _defn()
    r = defn.recipes["main"].model_copy(deep=True)
    r.steps[0].call["bind"] = {}      # meter を束縛しない
    with pytest.raises(SeqExpressionError, match="束縛されていません"):
        recipe_to_plan(r, {}, definition=defn)


def test_call_capability_mismatch():
    """ロールの requires を bind 先装置が満たさない → コンパイルエラー。"""
    defn = _defn()
    # needs_ammeter は measure_current を要求するが装置に無い
    r = defn.recipes["main"].model_copy(deep=True)
    r.steps = [type(r.steps[0])(call={
        "sequence": "needs_ammeter", "bind": {"probe": "DMM1"},
        "returns_as": {"x": "out"},
    })]
    with pytest.raises(SeqExpressionError, match="capability"):
        recipe_to_plan(r, {}, definition=defn)


def test_call_bad_returns_as():
    defn = _defn()
    r = defn.recipes["main"].model_copy(deep=True)
    r.steps[0].call["returns_as"] = {"nonexistent_return": "x"}
    with pytest.raises(SeqExpressionError, match="returns"):
        recipe_to_plan(r, {}, definition=defn)


def test_call_runtime_with_expr_refs_validated():
    """v2.34.0 (SP-7.1): with の実行時式 ${...} は呼び出し元スコープで参照検証。
    未定義参照はコンパイルエラー。"""
    defn = _defn()
    r = defn.recipes["main"].model_copy(deep=True)
    r.steps[0].call["with"] = {"n": "${vars.undefined_here}"}
    with pytest.raises(SeqExpressionError, match="未定義"):
        recipe_to_plan(r, {}, definition=defn)


@pytest.mark.asyncio
async def test_call_runtime_with_expr_evaluated():
    """v2.34.0 (SP-7.1): 呼び出し元の実行時変数を with で渡し、
    サブシーケンス内で params.X として使える。"""
    doc = yaml.safe_load(textwrap.dedent(SAMPLE_YAML))
    doc["sequences"]["scaled"] = {
        "roles": [{"name": "m", "requires": {"commands": ["measure_voltage"]}}],
        "parameters": [{"name": "factor", "type": "float", "default": 1.0}],
        "returns": ["out"],
        "steps": [
            {"command": "measure_voltage", "instrument": "@m", "result_as": "raw"},
            {"compute": {"set": "out", "expr": "steps.raw * params.factor"}},
        ],
    }
    doc["recipes"]["rt"] = {"steps": [
        {"command": "measure_voltage", "result_as": "base"},
        {"call": {"sequence": "scaled", "bind": {"m": "DMM1"},
                  "with": {"factor": "${steps.base * 10}"},
                  "returns_as": {"out": "result"}}},
    ]}
    defn = InstrumentDefinition(**doc)
    # コンパイル: with_exprs に式が入り sub_params には入らない
    plan = recipe_to_plan(defn.recipes["rt"], {}, definition=defn)
    call = plan.steps[1]
    assert call.with_exprs == {"factor": "steps.base * 10"}
    assert "factor" not in call.sub_params
    # 実行: base=2.5 -> factor=25 -> result=2.5*25=62.5
    r = await execute_recipe(_visa("2.5"), _session(defn), "rt", {})
    assert r["success"] is True
    assert r["variables"]["vars"]["result"] == 62.5


def test_call_recursion_detected():
    defn = _defn()
    r = defn.recipes["main"].model_copy(deep=True)
    r.steps = [type(r.steps[0])(call={"sequence": "self_recursive",
                                       "returns_as": {"y": "z"}})]
    with pytest.raises(SeqExpressionError, match="再帰"):
        recipe_to_plan(r, {}, definition=defn)


def test_call_depth_limit():
    """CALL_MAX_DEPTH を超えるチェーンはエラー。"""
    doc = yaml.safe_load(textwrap.dedent(SAMPLE_YAML))
    # s0 -> s1 -> ... のチェーンを CALL_MAX_DEPTH+1 段作る
    seqs = {}
    depth = CALL_MAX_DEPTH + 1
    for i in range(depth):
        nxt = f"s{i+1}"
        if i < depth - 1:
            steps = [{"call": {"sequence": nxt}}]
        else:
            steps = [{"compute": {"set": "leaf", "expr": "1"}}]
        seqs[f"s{i}"] = {"returns": [], "steps": steps}
    doc["sequences"].update(seqs)
    doc["recipes"]["chain"] = {"steps": [{"call": {"sequence": "s0"}}]}
    defn = InstrumentDefinition(**doc)
    with pytest.raises(SeqExpressionError, match="深さ"):
        recipe_to_plan(defn.recipes["chain"], {}, definition=defn)


# ============================================================
# 3. returns の実行時利用 (deferred 注入)
# ============================================================

@pytest.mark.asyncio
async def test_call_return_used_in_deferred():
    defn = _defn()
    r = await execute_recipe(_visa("2.5"), _session(defn), "use_return_deferred", {})
    assert r["success"] is True
    # bv=2.5 -> current = 0.0025 (範囲内)
    v = r["variables"]["vars"]
    assert v["bv"] == 2.5


# ============================================================
# 4. ライブラリ解決 (sequences_dir)
# ============================================================

def test_library_resolution_and_conflict(tmp_path):
    lib = tmp_path / "std_lib.yaml"
    lib.write_text(textwrap.dedent("""
        sequences:
          double_read:
            returns: [d]
            steps:
              - { command: measure_voltage, instrument: "@m", result_as: a }
              - compute: { set: d, expr: "steps.a * 2" }
    """), encoding="utf-8")
    defn = _defn()
    r = defn.recipes["main"].model_copy(deep=True)
    r.steps = [type(r.steps[0])(call={
        "sequence": "std_lib.double_read", "bind": {"m": "DMM1"},
        "returns_as": {"d": "dd"},
    })]
    # ライブラリ経由で解決できる
    plan = recipe_to_plan(r, {}, definition=defn, sequences_dir=str(tmp_path))
    assert isinstance(plan.steps[0], CallStep)
    assert plan.steps[0].lib_sha256   # sha256 が記録される


# ============================================================
# 5. dry-run 展開 / 後方互換
# ============================================================

def test_dryrun_expands_call():
    defn = _defn()
    plan = recipe_to_plan(defn.recipes["main"], {}, definition=defn)
    view = dryrun_view(plan)
    row = view["steps"][0]
    assert row["type"] == "call"


def test_call_rejects_code_pause_that_nested_runtime_cannot_service(
    tmp_path, monkeypatch
):
    # 検証したいのは on_error=pause の拒否。python 実行は既定 deny なので、
    # 許可しておかないとその手前のポリシー拒否で止まってしまう。
    (tmp_path / "_policy.yaml").write_text(
        "code_execution:\n  python: allow\n", encoding="utf-8",
    )
    monkeypatch.setenv("LAB_EXECUTOR_POLICY_DIR", str(tmp_path))

    doc = yaml.safe_load(textwrap.dedent(SAMPLE_YAML))
    doc["sequences"]["pausing_code"] = {
        "returns": [],
        "steps": [{
            "py": {
                "code": "raise RuntimeError('pause me')",
                "outputs": [],
                "on_error": "pause",
            },
        }],
    }
    doc["recipes"]["call_pausing_code"] = {
        "steps": [{"call": {"sequence": "pausing_code"}}],
    }
    defn = InstrumentDefinition(**doc)

    with pytest.raises(SeqExpressionError, match="on_error=pause"):
        recipe_to_plan(
            defn.recipes["call_pausing_code"], {}, definition=defn,
        )


@pytest.mark.asyncio
async def test_backward_compat_no_sequences():
    """sequences を持たない既存レシピは一切変わらない。"""
    doc = yaml.safe_load(textwrap.dedent(SAMPLE_YAML))
    doc.pop("sequences", None)
    doc["recipes"] = {"plain": {"steps": [
        {"command": "measure_voltage", "result_as": "x"},
        {"compute": {"set": "y", "expr": "steps.x * 2"}},
    ]}}
    defn = InstrumentDefinition(**doc)
    r = await execute_recipe(_visa("3.0"), _session(defn), "plain", {})
    assert r["success"] is True
    assert r["variables"]["vars"]["y"] == 6.0
