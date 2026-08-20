"""掃引 (sweep) — 値を振りながら同じ手順を繰り返す。

反復 (repeat) との違いは値が決まる時点にある。掃引はコンパイル時に展開されるので、
振った値は body の中で通常のパラメータとして参照でき、展開後は普通のリテラル引数に
なる。実行時に決まる ``${...}`` と違って範囲宣言を要求しないのは、値が実行前に
確定していて dry-run でそのまま読めるからである。

反復では書けなかった **任意の値の列** (1, 2, 5, 10 のような飛び飛びの値) が
書けるようになる、というのが掃引を足した理由。
"""

from __future__ import annotations

import textwrap

import pytest
import yaml

from lab_executor.models.instrument_def import InstrumentDefinition
from lab_executor.recipe_executor import recipe_to_plan
from lab_executor.utils.expression import ExpressionError
from lab_executor.utils.seq_expression import SeqExpressionError

_BASE = """
metadata:
  manufacturer: Kikusui
  model: PMX35-3A
  category: power_supply
  support_level: experimental
  definition_version: "0.1.0"
commands:
  set_voltage:
    scpi: "VOLT {voltage}"
    type: write
    description: 電圧設定
    parameters:
      - {name: voltage, type: float, range: [0, 36.75]}
  measure_current:
    scpi: "MEAS:CURR?"
    type: query
    description: 電流測定
    returns: {type: float, unit: A}
recipes:
  r:
%(params)s
    steps:
%(steps)s
"""


def plan_for(steps_yaml: str, *, parameters: str = "", **variables):
    """steps だけを与えてレシピをコンパイルする。"""
    document = _BASE % {
        "params": textwrap.indent(parameters, "    ") if parameters else "",
        "steps": textwrap.indent(textwrap.dedent(steps_yaml).strip("\n"), "      "),
    }
    definition = InstrumentDefinition(**yaml.safe_load(document))
    return recipe_to_plan(
        definition.recipes["r"], dict(variables),
        primary_resource="USB0::1::INSTR", definition=definition,
    )


def voltages(plan):
    return [
        s.args.get("voltage")
        for s in plan.steps
        if getattr(s, "command", "") == "set_voltage"
    ]


LISTED = """
- sweep:
    parameter: v
    values: {values: [1, 2, 5, 10]}
    steps:
      - {command: set_voltage, args: {voltage: "$v"}}
      - {command: measure_current, result_as: i}
"""


def test_an_explicit_list_of_values_is_swept():
    """反復では書けなかった、飛び飛びの値。"""
    plan = plan_for(LISTED)
    assert voltages(plan) == [1, 2, 5, 10]
    # body は値の数だけ複製される (1 回あたり 2 手順)。
    assert len(plan.steps) == 8


def test_a_start_stop_step_range_is_swept():
    plan = plan_for("""
        - sweep:
            parameter: v
            values: {start: 0, stop: 1, step: 0.5}
            steps:
              - {command: set_voltage, args: {voltage: "$v"}}
    """)
    assert voltages(plan) == [0, 0.5, 1.0]


def test_a_swept_value_is_an_ordinary_argument_afterwards():
    """実行時解決ではないので、範囲宣言も実行時解決も要らない。"""
    for step in plan_for(LISTED).steps:
        assert not getattr(step, "deferred_args", None)


def test_the_sweep_variable_does_not_leak_after_the_sweep():
    """掃引変数は body の中だけのもの。後から使えば未定義になる。"""
    with pytest.raises((ExpressionError, SeqExpressionError), match="v"):
        plan_for("""
            - sweep:
                parameter: v
                values: {values: [1]}
                steps:
                  - {command: set_voltage, args: {voltage: "$v"}}
            - {command: set_voltage, args: {voltage: "$v"}}
        """)


def test_a_parameter_with_the_same_name_is_restored_after_the_sweep():
    """同名のパラメータを掃引が一時的に覆っても、後で元に戻る。"""
    plan = plan_for(
        """
        - sweep:
            parameter: v
            values: {values: [1, 2]}
            steps:
              - {command: set_voltage, args: {voltage: "$v"}}
        - {command: set_voltage, args: {voltage: "$v"}}
        """,
        parameters="parameters:\n  - {name: v, type: float, default: 30}\n",
        v=30,
    )
    assert voltages(plan) == [1, 2, 30]


def test_a_sweep_inside_a_repeat_is_expanded_in_its_body():
    """入れ子でも展開できる (compile 時展開なので実行時構造に触らない)。"""
    plan = plan_for("""
        - repeat:
            count: 2
            steps:
              - sweep:
                  parameter: v
                  values: {values: [1, 2]}
                  steps:
                    - {command: set_voltage, args: {voltage: "$v"}}
    """)
    # repeat は実行時構造なので IR には 1 段として残り、body の中が展開される。
    assert len(plan.steps) == 1
    assert plan.steps[0].type == "repeat"
    assert [s.args.get("voltage") for s in plan.steps[0].body] == [1, 2]


@pytest.mark.parametrize(
    "sweep_body, reason",
    [
        ("parameter: v\n    values: {}", "values"),
        ("parameter: ''\n    values: {values: [1]}", "parameter"),
        ("parameter: params\n    values: {values: [1]}", "予約語"),
        ("parameter: v\n    values: {values: [1]}\n    steps: []", "steps"),
    ],
)
def test_a_malformed_sweep_is_refused_with_a_reason(sweep_body, reason):
    body = sweep_body if "steps:" in sweep_body else (
        sweep_body + '\n    steps:\n      - {command: set_voltage, args: {voltage: "$v"}}'
    )
    with pytest.raises(SeqExpressionError, match=reason):
        plan_for(f"- sweep:\n    {body.replace(chr(10), chr(10) + '')}\n")


def test_the_expansion_limit_is_enforced():
    """点数の上限は DSL の掃引と同じものを使う。"""
    with pytest.raises(SeqExpressionError, match="上限|超過"):
        plan_for("""
            - sweep:
                parameter: v
                values: {start: 0, stop: 100000, step: 1}
                steps:
                  - {command: set_voltage, args: {voltage: "$v"}}
        """)
