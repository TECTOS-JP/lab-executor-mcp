"""
統合式評価器 (SP-1)

シーケンス処理拡張 (sequence_processing_spec §4) の式言語を 1 つの評価器に統合する。
既存の ``utils/expression.py`` (算術) と ``utils/condition.py`` (比較・論理) は
**変更せず**、既存経路 (wait_for_condition 等) の挙動を保護する。本モジュールは
compute / ${...} 実行時解決など **新機能だけ** が使う。

サポートする構文 (AST ホワイトリスト方式):
- リテラル: 数値 (int/float, 指数表記可)・文字列・True/False
- 参照: ``params.x`` / ``steps.x`` / ``vars.x`` / ``env.x`` (ドット 1 段のみ)
- 裸の名前 (後方互換): params -> vars -> steps の順で平坦探索
- 算術: ``+ - * / // % **``、単項 ``+ -``、括弧
- 比較 (連鎖可): ``== != < <= > >=``
- 論理 (短絡): ``and or not``
- 三項: ``A if cond else B``
- 関数: abs, min, max, round, floor, ceil, sqrt, log10, log, exp, clamp, len

拒否:
- 2 段以上の属性チェーン / dunder (``__``) アクセス
- ホワイトリスト外の関数呼び出し
- 添字・内包・ラムダ・代入・import 等、未許可の AST ノード全般

数値健全性:
- ゼロ除算・NaN/inf 生成・型不整合 (str と数値の演算) は ``SeqExpressionError``
"""
from __future__ import annotations
import ast
import math
from typing import Any


class SeqExpressionError(ValueError):
    """統合式評価エラー。

    ``ValueError`` を継承しているため、``recipe_to_plan`` を ``except
    ValueError`` で受けている既存呼び出し側 (UI dry-run 等) でも捕捉される。
    """


_NAMESPACES = ("params", "steps", "vars", "env")

# 裸名フォールバックの探索順 (spec: params -> vars -> steps)
_BARE_LOOKUP_ORDER = ("params", "vars", "steps", "env")

_ALLOWED_NODES = (
    ast.Expression,
    ast.BoolOp, ast.UnaryOp, ast.BinOp, ast.Compare, ast.IfExp,
    ast.Constant, ast.Name, ast.Load, ast.Call, ast.Attribute,
    # 論理
    ast.And, ast.Or, ast.Not,
    # 比較
    ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq,
    # 算術
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.USub, ast.UAdd,
)


def _clamp(x: Any, lo: Any, hi: Any) -> Any:
    return max(lo, min(x, hi))


_FUNCS = {
    "abs": abs,
    "min": min,
    "max": max,
    "round": round,
    "len": len,
    "floor": math.floor,
    "ceil": math.ceil,
    "sqrt": math.sqrt,
    "log10": math.log10,
    "log": math.log,
    "exp": math.exp,
    "clamp": _clamp,
}


def check_expr(expr: str) -> ast.Expression:
    """式をパースし AST ノードをホワイトリスト検証する (値は評価しない)。

    コンパイル時検証 (compute / ${...}) から利用する。構文/ノード違反時は
    ``SeqExpressionError``。
    """
    expr = (expr or "").strip()
    if not expr:
        raise SeqExpressionError("空の式です")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise SeqExpressionError(f"式の構文エラー: {expr!r} ({e})")

    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise SeqExpressionError(
                f"安全でないノードを検出: {type(node).__name__} (式: {expr!r})"
            )
        if isinstance(node, ast.Attribute):
            if not isinstance(node.value, ast.Name) or node.value.id not in _NAMESPACES:
                raise SeqExpressionError(
                    "属性アクセスは params/steps/vars/env の 1 段のみ許可されます "
                    f"(式: {expr!r})"
                )
            if node.attr.startswith("__"):
                raise SeqExpressionError(f"dunder 属性アクセスは禁止です (式: {expr!r})")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCS:
                raise SeqExpressionError(
                    f"許可されていない関数呼び出しです (式: {expr!r})"
                )
    return tree


def evaluate(expr: str, ctx: dict[str, dict]) -> Any:
    """式を値モードで評価する。

    ``ctx`` = ``{"params": {...}, "steps": {...}, "vars": {...}, "env": {...}}``。
    """
    tree = check_expr(expr)
    result = _eval_node(tree.body, ctx, expr)
    _check_finite(result, expr)
    return result


def evaluate_condition(expr: str, ctx: dict[str, dict]) -> bool:
    """式を条件モードで評価する (結果を bool 強制)。"""
    return bool(evaluate(expr, ctx))


def referenced_names(expr: str) -> set[str]:
    """式が参照する名前を集合で返す (コンパイル時検証用)。

    - ``params.x`` 形式は ``"params.x"`` のように名前空間付きで返す
    - 裸の名前 (関数名・名前空間名を除く) は ``"x"`` のように返す
    """
    try:
        tree = ast.parse((expr or "").strip(), mode="eval")
    except SyntaxError as e:
        raise SeqExpressionError(f"式の構文エラー: {expr!r} ({e})")

    out: set[str] = set()

    def visit(node: ast.AST) -> None:
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in _NAMESPACES
        ):
            out.add(f"{node.value.id}.{node.attr}")
            return  # 名前空間 Name 本体には降りない
        if isinstance(node, ast.Call):
            for a in node.args:
                visit(a)
            for kw in node.keywords:
                visit(kw.value)
            return
        if isinstance(node, ast.Name):
            if node.id not in _NAMESPACES and node.id not in _FUNCS:
                out.add(node.id)
            return
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree.body)
    return out


# ============================================================
# 内部評価
# ============================================================


def _check_finite(value: Any, expr: str) -> None:
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        raise SeqExpressionError(f"NaN / inf が生成されました (式: {expr!r})")


def _eval_node(node: ast.AST, ctx: dict, expr: str) -> Any:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float, str, bool)):
            return node.value
        raise SeqExpressionError(f"サポートされないリテラル: {node.value!r}")

    if isinstance(node, ast.Attribute):
        ns = node.value.id  # type: ignore[attr-defined]
        key = node.attr
        table = ctx.get(ns)
        if not isinstance(table, dict) or key not in table:
            raise SeqExpressionError(f"未定義の参照: {ns}.{key} (式: {expr!r})")
        return table[key]

    if isinstance(node, ast.Name):
        for ns in _BARE_LOOKUP_ORDER:
            table = ctx.get(ns)
            if isinstance(table, dict) and node.id in table:
                return table[node.id]
        raise SeqExpressionError(f"未定義の変数: {node.id} (式: {expr!r})")

    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            r: Any = True
            for v in node.values:
                r = _eval_node(v, ctx, expr)
                if not r:
                    return r
            return r
        if isinstance(node.op, ast.Or):
            r = False
            for v in node.values:
                r = _eval_node(v, ctx, expr)
                if r:
                    return r
            return r

    if isinstance(node, ast.IfExp):
        cond = _eval_node(node.test, ctx, expr)
        return (
            _eval_node(node.body, ctx, expr)
            if cond else _eval_node(node.orelse, ctx, expr)
        )

    if isinstance(node, ast.UnaryOp):
        operand = _eval_node(node.operand, ctx, expr)
        if isinstance(node.op, ast.USub):
            return -operand
        if isinstance(node.op, ast.UAdd):
            return +operand
        if isinstance(node.op, ast.Not):
            return not operand

    if isinstance(node, ast.BinOp):
        left = _eval_node(node.left, ctx, expr)
        right = _eval_node(node.right, ctx, expr)
        op = node.op
        try:
            if isinstance(op, ast.Add):
                r = left + right
            elif isinstance(op, ast.Sub):
                r = left - right
            elif isinstance(op, ast.Mult):
                r = left * right
            elif isinstance(op, ast.Div):
                r = left / right
            elif isinstance(op, ast.FloorDiv):
                r = left // right
            elif isinstance(op, ast.Mod):
                r = left % right
            elif isinstance(op, ast.Pow):
                r = left ** right
            else:
                raise SeqExpressionError(f"未対応の演算子: {type(op).__name__}")
        except ZeroDivisionError:
            raise SeqExpressionError(f"ゼロ除算です (式: {expr!r})")
        except SeqExpressionError:
            raise
        except (TypeError, ValueError) as e:
            raise SeqExpressionError(f"型不整合の演算です: {e} (式: {expr!r})")
        _check_finite(r, expr)
        return r

    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, ctx, expr)
        result = True
        for op, comp_node in zip(node.ops, node.comparators):
            right = _eval_node(comp_node, ctx, expr)
            try:
                if isinstance(op, ast.Lt):
                    ok = left < right
                elif isinstance(op, ast.LtE):
                    ok = left <= right
                elif isinstance(op, ast.Gt):
                    ok = left > right
                elif isinstance(op, ast.GtE):
                    ok = left >= right
                elif isinstance(op, ast.Eq):
                    ok = left == right
                elif isinstance(op, ast.NotEq):
                    ok = left != right
                else:
                    raise SeqExpressionError(
                        f"未対応の比較演算子: {type(op).__name__}"
                    )
            except SeqExpressionError:
                raise
            except TypeError as e:
                raise SeqExpressionError(
                    f"型不整合の比較です: {e} (式: {expr!r})"
                )
            result = result and ok
            left = right
        return result

    if isinstance(node, ast.Call):
        func = _FUNCS[node.func.id]  # type: ignore[attr-defined]
        args = [_eval_node(a, ctx, expr) for a in node.args]
        try:
            r = func(*args)
        except SeqExpressionError:
            raise
        except ZeroDivisionError:
            raise SeqExpressionError(f"ゼロ除算です (式: {expr!r})")
        except (TypeError, ValueError) as e:
            raise SeqExpressionError(
                f"関数 {node.func.id} の呼び出しエラー: {e} (式: {expr!r})"  # type: ignore[attr-defined]
            )
        _check_finite(r, expr)
        return r

    raise SeqExpressionError(f"未対応のノード: {type(node).__name__} (式: {expr!r})")


# ============================================================
# ${...} 実行時引数の検出 (SP-2)
# ============================================================


def parse_deferred(value: Any) -> str | None:
    """arg 値が実行時解決 ``${expr}`` かどうかを判定し、中身の式を返す。

    - ``${expr}`` (arg 値全体を占める) の場合のみ対応。式全体を返す
    - ``${`` を含むが全体を占めない (文字列内埋め込み) 場合は ``SeqExpressionError``
      (SP-4 で対応予定)
    - ``${`` を含まない場合は ``None`` (従来の $ 式 / リテラルとして扱う)
    """
    if not isinstance(value, str):
        return None
    s = value.strip()
    if "${" not in s:
        return None
    if s.startswith("${") and s.endswith("}") and s.count("${") == 1:
        inner = s[2:-1].strip()
        if not inner:
            raise SeqExpressionError("空の ${} 式です")
        return inner
    raise SeqExpressionError(
        f"文字列内への ${{...}} 埋め込みは未対応です (SP-4 予定): {value!r}"
    )
