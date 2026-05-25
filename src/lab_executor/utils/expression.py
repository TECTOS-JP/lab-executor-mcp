"""
安全な式評価 (v0.3.0)

recipe のステップで使用する式評価。Python の ast を用いて、
許可されたノード型のみを評価することで、任意コード実行を防ぐ。

許可されるもの:
- 数値リテラル (int, float)
- 変数参照 (識別子)
- 四則演算 (+ - * /)
- 単項演算子 (+x, -x)
- 括弧でのグループ化

許可されないもの:
- 関数呼び出し (例: __import__, exec)
- 属性アクセス
- インデックスアクセス
- 文字列リテラル
- 比較・論理演算
"""
from __future__ import annotations
import ast
from typing import Any


class ExpressionError(Exception):
    """式評価エラー"""


# 許可する AST ノード型
_ALLOWED_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp,
    ast.Constant, ast.Name, ast.Load,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.USub, ast.UAdd,
)


def safe_eval(expr: str, variables: dict[str, Any]) -> Any:
    """
    式 expr を変数辞書 variables のもとで評価する。
    expr 内で参照される変数名は variables に含まれている必要がある。
    """
    expr = expr.strip()
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise ExpressionError(f"式の構文エラー: {expr!r} ({e})")

    # AST を歩いて許可ノードのみか確認
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ExpressionError(
                f"安全でないノードを検出: {type(node).__name__} (式: {expr!r})"
            )

    return _eval_node(tree.body, variables, expr)


def _eval_node(node: ast.AST, vars: dict[str, Any], expr: str) -> Any:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ExpressionError(f"数値以外のリテラルは禁止: {node.value!r}")

    if isinstance(node, ast.Name):
        if node.id not in vars:
            raise ExpressionError(f"未定義の変数: {node.id} (式: {expr!r})")
        return vars[node.id]

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
        raise ExpressionError(f"未対応の演算子: {type(op).__name__}")

    if isinstance(node, ast.UnaryOp):
        operand = _eval_node(node.operand, vars, expr)
        if isinstance(node.op, ast.USub): return -operand
        if isinstance(node.op, ast.UAdd): return +operand
        raise ExpressionError(f"未対応の単項演算子: {type(node.op).__name__}")

    raise ExpressionError(f"未対応のノード: {type(node).__name__}")


def resolve_arg(value: Any, variables: dict[str, Any]) -> Any:
    """
    recipe ステップの args 値を解決する。

    - 文字列で "$" から始まる場合は式評価 (例: "$target_v * 1.1")
    - その他はそのまま返す
    """
    if isinstance(value, str) and value.startswith("$"):
        return safe_eval(value[1:], variables)
    return value
