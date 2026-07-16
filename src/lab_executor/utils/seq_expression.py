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
- NumPy 名前空間 (v2.31.0 SP-5): ``np.*`` として許可済みNumPy関数を利用可
  (例 ``np.mean(vars.xs)`` / ``np.polyfit(vars.x, vars.y, 1)`` /
  ``np.fft.rfft(...)``)。属性は ``np.`` 配下 1〜2 段のみ。
  **明示allowlist** (spec §4): 統計・補間・fitting・FFT等の純粋計算だけを
  許可する。I/O / ctypes / callback受付 / global state変更 / 配列constructorは
  allowlist外として拒否する。dunder (``__`` 始まり) アクセスも全面禁止。

拒否:
- np 以外の 2 段以上の属性チェーン / dunder (``__``) アクセス
- ホワイトリスト外の関数呼び出し
- 添字・内包・ラムダ・代入・import 等、未許可の AST ノード全般

数値健全性:
- ゼロ除算・NaN/inf 生成 (配列要素含む)・型不整合 (str と数値の演算) は
  ``SeqExpressionError``
- ndarray の真偽値 (曖昧) を条件に使うと ``SeqExpressionError``
- 0 次元 ndarray / NumPy スカラは Python スカラへ自動変換 (spec §3)
- 式の返す ndarray は要素数上限 ``ARRAY_MAX_ELEMENTS`` (既定 10^7) に従う
"""
from __future__ import annotations
import ast
import math
from typing import Any

import numpy as np


class SeqExpressionError(ValueError):
    """統合式評価エラー。

    ``ValueError`` を継承しているため、``recipe_to_plan`` を ``except
    ValueError`` で受けている既存呼び出し側 (UI dry-run 等) でも捕捉される。
    """


_NAMESPACES = ("params", "steps", "vars", "env")

# 裸名フォールバックの探索順 (spec: params -> vars -> steps)
_BARE_LOOKUP_ORDER = ("params", "vars", "steps", "env")

# v2.31.0 (SP-5): array の要素数上限 (spec §3、メモリ暴走防止)。
# VariableStore の代入時と式評価の返り値の両方で執行する。
ARRAY_MAX_ELEMENTS = 10_000_000

# v2.34.x security hardening: NumPy は denylist ではなく純粋計算APIの
# **明示allowlist** に限定する。NumPyの公開名前空間は広く、ctypeslib等の
# 副作用APIが追加・見落としで到達可能になるため「危険名だけ拒否」は安全境界に
# できない。配列生成関数も結果サイズ検査より前にallocateするため許可しない。
NP_ALLOWED_TOP_LEVEL = frozenset({
    # reductions / statistics
    "all", "any", "mean", "std", "var", "sum", "min", "max",
    "median", "percentile", "quantile", "ptp", "argmin", "argmax",
    # element-wise / shape-preserving transforms
    "abs", "absolute", "sqrt", "log", "log10", "exp", "clip",
    "diff", "gradient", "cumsum", "cumprod", "sort", "argsort",
    # fitting / interpolation / linear operations with bounded inputs
    "interp", "polyfit", "polyval", "corrcoef", "cov", "dot",
    # constants
    "pi", "e",
})

NP_ALLOWED_SUBMODULES: dict[str, frozenset[str]] = {
    "fft": frozenset({
        "fft", "ifft", "rfft", "irfft", "fftfreq", "rfftfreq",
    }),
}


def _np_allowed(parts: list[str]) -> bool:
    if len(parts) == 1:
        return parts[0] in NP_ALLOWED_TOP_LEVEL
    if len(parts) == 2:
        return parts[1] in NP_ALLOWED_SUBMODULES.get(parts[0], frozenset())
    return False


def _np_chain(node: ast.AST) -> list[str] | None:
    """Attribute ノードが ``np.`` 起点のチェーンなら属性名リストを返す。

    例: ``np.mean`` -> ["mean"], ``np.fft.rfft`` -> ["fft", "rfft"]。
    np 起点でなければ None。
    """
    parts: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name) and cur.id == "np":
        parts.reverse()
        return parts
    return None

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
            if node.attr.startswith("__"):
                raise SeqExpressionError(f"dunder 属性アクセスは禁止です (式: {expr!r})")
            np_parts = _np_chain(node)
            if np_parts is not None:
                if not _np_allowed(np_parts):
                    raise SeqExpressionError(
                        f"np.{'.'.join(np_parts)} は式のNumPy allowlistに"
                        f"含まれていません (純粋計算APIのみ許可。spec §4) "
                        f"(式: {expr!r})"
                    )
            elif isinstance(node.value, ast.Name) and node.value.id in _NAMESPACES:
                pass  # params/steps/vars/env の 1 段 (従来どおり)
            else:
                raise SeqExpressionError(
                    "属性アクセスは params/steps/vars/env の 1 段、"
                    f"または np.* (1〜2 段) のみ許可されます (式: {expr!r})"
                )
        if isinstance(node, ast.Call):
            is_builtin = (
                isinstance(node.func, ast.Name) and node.func.id in _FUNCS
            )
            is_np = (
                isinstance(node.func, ast.Attribute)
                and _np_chain(node.func) is not None
            )
            if not (is_builtin or is_np):
                raise SeqExpressionError(
                    f"許可されていない関数呼び出しです (式: {expr!r})"
                )
    return tree


def evaluate(expr: str, ctx: dict[str, dict]) -> Any:
    """式を値モードで評価する。

    ``ctx`` = ``{"params": {...}, "steps": {...}, "vars": {...}, "env": {...}}``。

    v2.31.0 (SP-5): 返り値が NumPy スカラ / 0 次元 ndarray の場合は Python
    スカラへ自動変換する。ndarray は要素数上限と NaN/inf を検査して返す。
    """
    tree = check_expr(expr)
    result = _eval_node(tree.body, ctx, expr)
    result = _normalize_result(result, expr)
    _check_finite(result, expr)
    return result


def _normalize_result(value: Any, expr: str) -> Any:
    """NumPy 値の正規化 (SP-5)。

    - np.generic (np.float64 等) / 0 次元 ndarray -> Python スカラ
    - ndarray: 要素数上限を執行。モジュール・関数等の非値はエラー
    """
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        if value.ndim == 0:
            return value.item()
        if value.size > ARRAY_MAX_ELEMENTS:
            raise SeqExpressionError(
                f"配列の要素数 ({value.size}) が上限 ({ARRAY_MAX_ELEMENTS}) を"
                f"超えています (式: {expr!r})"
            )
        return value
    if isinstance(value, (int, float, bool, str)) or value is None:
        return value
    # tuple 返し (np.polyfit(full=True) 等) やモジュール参照は不可
    raise SeqExpressionError(
        f"式の結果がサポートされない型です: {type(value).__name__} (式: {expr!r})"
    )


def evaluate_condition(expr: str, ctx: dict[str, dict]) -> bool:
    """式を条件モードで評価する (結果を bool 強制)。

    v2.31.0 (SP-5): ndarray の真偽値 (曖昧) は ``SeqExpressionError`` に変換する
    (guard / branch / repeat while が明確なエラーで failed になるように)。
    """
    v = evaluate(expr, ctx)
    try:
        return bool(v)
    except ValueError:
        raise SeqExpressionError(
            f"条件式の結果が配列で真偽値が曖昧です。np.all(...) / np.any(...) "
            f"等で集約してください (式: {expr!r})"
        )


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
        if isinstance(node, ast.Attribute):
            if _np_chain(node) is not None:
                return  # np.* チェーンは変数参照ではない (SP-5)
            if (
                isinstance(node.value, ast.Name)
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
            if (
                node.id not in _NAMESPACES
                and node.id not in _FUNCS
                and node.id != "np"
            ):
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
    # v2.31.0 (SP-5): NumPy スカラ / float 系 ndarray も NaN/inf を検査
    if isinstance(value, np.generic):
        v = value.item()
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            raise SeqExpressionError(f"NaN / inf が生成されました (式: {expr!r})")
    if isinstance(value, np.ndarray) and value.dtype.kind in "fc" and value.size:
        if not np.isfinite(value).all():
            raise SeqExpressionError(
                f"配列に NaN / inf が含まれています (式: {expr!r})"
            )


def _truthy(value: Any, expr: str) -> bool:
    """真偽値化 (ndarray の曖昧真偽値を SeqExpressionError に変換)。"""
    try:
        return bool(value)
    except ValueError:
        raise SeqExpressionError(
            f"配列の真偽値は曖昧です。np.all(...) / np.any(...) 等で"
            f"集約してください (式: {expr!r})"
        )


def _resolve_np(parts: list[str], expr: str) -> Any:
    """np.* チェーンを実体に解決する (check_expr 通過後に呼ばれる)。"""
    obj: Any = np
    for p in parts:
        try:
            obj = getattr(obj, p)
        except AttributeError:
            raise SeqExpressionError(
                f"np.{'.'.join(parts)} は存在しません (式: {expr!r})"
            )
    return obj


def _eval_node(node: ast.AST, ctx: dict, expr: str) -> Any:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float, str, bool)):
            return node.value
        raise SeqExpressionError(f"サポートされないリテラル: {node.value!r}")

    if isinstance(node, ast.Attribute):
        np_parts = _np_chain(node)
        if np_parts is not None:
            # v2.31.0 (SP-5): np.mean 等の関数・np.pi 等の定数参照
            return _resolve_np(np_parts, expr)
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
                if not _truthy(r, expr):
                    return r
            return r
        if isinstance(node.op, ast.Or):
            r = False
            for v in node.values:
                r = _eval_node(v, ctx, expr)
                if _truthy(r, expr):
                    return r
            return r

    if isinstance(node, ast.IfExp):
        cond = _eval_node(node.test, ctx, expr)
        return (
            _eval_node(node.body, ctx, expr)
            if _truthy(cond, expr) else _eval_node(node.orelse, ctx, expr)
        )

    if isinstance(node, ast.UnaryOp):
        operand = _eval_node(node.operand, ctx, expr)
        if isinstance(node.op, ast.USub):
            return -operand
        if isinstance(node.op, ast.UAdd):
            return +operand
        if isinstance(node.op, ast.Not):
            return not _truthy(operand, expr)

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
            result = _truthy(result, expr) and ok
            left = right
        return result

    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Attribute):
            # v2.31.0 (SP-5): np.* 関数呼び出し (check_expr 検証済み)
            parts = _np_chain(node.func) or []
            func = _resolve_np(parts, expr)
            func_label = f"np.{'.'.join(parts)}"
            if not callable(func):
                raise SeqExpressionError(
                    f"{func_label} は呼び出し可能ではありません (式: {expr!r})"
                )
        else:
            func = _FUNCS[node.func.id]  # type: ignore[attr-defined]
            func_label = node.func.id  # type: ignore[attr-defined]
        # キーワード引数は不許可 (ast.keyword はホワイトリスト外)。位置引数のみ。
        args = [_eval_node(a, ctx, expr) for a in node.args]
        try:
            r = func(*args)
        except SeqExpressionError:
            raise
        except ZeroDivisionError:
            raise SeqExpressionError(f"ゼロ除算です (式: {expr!r})")
        except (TypeError, ValueError) as e:
            raise SeqExpressionError(
                f"関数 {func_label} の呼び出しエラー: {e} (式: {expr!r})"
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
        f"文字列内への ${{...}} 埋め込みは未対応です (表示文字列 [pause の message 等] "
        f"のみ v2.30.0 で対応。args への部分埋め込みは引き続き禁止): {value!r}"
    )


# ============================================================
# 表示文字列内の ${...} 補間 (SP-4)
# ============================================================
# 装置に届く args ではなく、pause の message 等「人間/AI への表示文字列」
# 専用。args への部分埋め込みは引き続き parse_deferred が拒否する。


def string_expr_parts(text: str) -> list[str]:
    """表示文字列内の ``${expr}`` の式部分を出現順に列挙する (検証用)。

    対応する ``}`` が無い・空の式は ``SeqExpressionError``。
    """
    out: list[str] = []
    i = 0
    while True:
        start = text.find("${", i)
        if start < 0:
            break
        end = text.find("}", start + 2)
        if end < 0:
            raise SeqExpressionError(
                f"閉じられていない ${{...}} があります: {text!r}"
            )
        inner = text[start + 2:end].strip()
        if not inner:
            raise SeqExpressionError(f"空の ${{}} 式です: {text!r}")
        out.append(inner)
        i = end + 1
    return out


def interpolate_string(text: str, ctx: dict[str, dict]) -> str:
    """表示文字列内の ``${expr}`` を評価値で置換する (SP-4、message 用)。

    評価エラーの部分は **そのまま残す** (表示専用のため安全に fail-soft。
    装置に届く値ではないので guard/deferred のような failed 化はしない)。
    """
    result: list[str] = []
    i = 0
    while True:
        start = text.find("${", i)
        if start < 0:
            result.append(text[i:])
            break
        end = text.find("}", start + 2)
        if end < 0:
            result.append(text[i:])
            break
        result.append(text[i:start])
        raw = text[start:end + 1]
        inner = text[start + 2:end].strip()
        try:
            result.append(str(evaluate(inner, ctx)))
        except SeqExpressionError:
            result.append(raw)  # fail-soft: 未解決のまま表示
        i = end + 1
    return "".join(result)
