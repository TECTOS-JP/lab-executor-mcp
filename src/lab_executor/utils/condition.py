"""
安全な condition 式評価 (v0.5.1)

Polling wait (wait_for_condition) で使用する真偽値式評価。

許可されるノード:
  - 変数 `value` (最新測定値)
  - 数値リテラル
  - 比較演算子: < <= > >= == !=
  - 論理演算: and / or / not
  - 単項算術演算子: + -
  - 二項算術演算子: + - * / // % **
  - 関数呼び出し: abs() のみ
  - 括弧

禁止:
  - 属性アクセス
  - import / 任意関数呼び出し
  - インデックス / 添字
  - 文字列リテラル
  - 代入 / 内包表記 / lambda
"""
from __future__ import annotations
import ast
from typing import Any


class ConditionError(Exception):
    """condition 式評価エラー"""


_ALLOWED_NODES = (
    ast.Expression,
    ast.BoolOp, ast.UnaryOp, ast.BinOp, ast.Compare,
    ast.Constant, ast.Name, ast.Load, ast.Call,
    # 論理
    ast.And, ast.Or, ast.Not,
    # 比較
    ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq,
    # 算術
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.USub, ast.UAdd,
)

_ALLOWED_FUNCS = {"abs"}


def safe_eval_condition(expr: str, variables: dict[str, Any]) -> bool:
    """
    expr を評価し、真偽値を返す。

    variables には少なくとも `value` キーが含まれる必要がある (呼び出し側が保証)。
    """
    expr = expr.strip()
    if not expr:
        raise ConditionError("空の condition 式")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise ConditionError(f"condition 式の構文エラー: {expr!r} ({e})")

    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ConditionError(
                f"安全でないノードを検出: {type(node).__name__} (式: {expr!r})"
            )
        # 関数呼び出しは allow list
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCS:
                raise ConditionError(
                    f"許可されていない関数呼び出し: {ast.dump(node.func)} (式: {expr!r})"
                )

    result = _eval_node(tree.body, variables, expr)
    return bool(result)


def _eval_node(node: ast.AST, vars: dict[str, Any], expr: str) -> Any:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float, bool)):
            return node.value
        raise ConditionError(f"数値/真偽値以外のリテラル禁止: {node.value!r}")

    if isinstance(node, ast.Name):
        if node.id not in vars:
            raise ConditionError(f"未定義の変数: {node.id} (式: {expr!r})")
        return vars[node.id]

    if isinstance(node, ast.BoolOp):
        vals = [_eval_node(v, vars, expr) for v in node.values]
        if isinstance(node.op, ast.And):
            r = True
            for v in vals:
                r = r and v
            return r
        if isinstance(node.op, ast.Or):
            r = False
            for v in vals:
                r = r or v
            return r

    if isinstance(node, ast.UnaryOp):
        operand = _eval_node(node.operand, vars, expr)
        if isinstance(node.op, ast.USub): return -operand
        if isinstance(node.op, ast.UAdd): return +operand
        if isinstance(node.op, ast.Not): return not operand

    if isinstance(node, ast.BinOp):
        left = _eval_node(node.left, vars, expr)
        right = _eval_node(node.right, vars, expr)
        op = node.op
        if isinstance(op, ast.Add): return left + right
        if isinstance(op, ast.Sub): return left - right
        if isinstance(op, ast.Mult): return left * right
        if isinstance(op, ast.Div): return left / right
        if isinstance(op, ast.FloorDiv): return left // right
        if isinstance(op, ast.Mod): return left % right
        if isinstance(op, ast.Pow): return left ** right
        raise ConditionError(f"未対応の演算子: {type(op).__name__}")

    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, vars, expr)
        result = True
        for op, comp_node in zip(node.ops, node.comparators):
            right = _eval_node(comp_node, vars, expr)
            if isinstance(op, ast.Lt):   ok = left < right
            elif isinstance(op, ast.LtE): ok = left <= right
            elif isinstance(op, ast.Gt):  ok = left > right
            elif isinstance(op, ast.GtE): ok = left >= right
            elif isinstance(op, ast.Eq):  ok = left == right
            elif isinstance(op, ast.NotEq): ok = left != right
            else:
                raise ConditionError(f"未対応の比較演算子: {type(op).__name__}")
            result = result and ok
            left = right
        return result

    if isinstance(node, ast.Call):
        # allow list は既に walk でチェック済み
        func_name = node.func.id  # type: ignore[attr-defined]
        if func_name == "abs":
            if len(node.args) != 1:
                raise ConditionError("abs() は引数 1 つ")
            return abs(_eval_node(node.args[0], vars, expr))

    raise ConditionError(f"未対応のノード: {type(node).__name__}")
