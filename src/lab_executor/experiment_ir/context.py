"""
実行時変数コンテキスト (SP-1 / SP-5)

シーケンス処理拡張 (sequence_processing_spec §3) の変数モデルを実装する。
1 Job (= 1 レシピ実行) のスコープで、capture (steps.*) と compute (vars.*) の
値を保持し、代入毎に ``var_assigned`` イベントを蓄積する。

- ``params.*``: レシピパラメータ (コンパイル時解決済み、読み取り専用)
- ``steps.*``:  capture (result_as) で登録した測定値
- ``vars.*``:   compute で演算した派生値
- ``env.*``:    実行コンテキスト (job_id / started_at / loop_index、読み取り専用)

型は float / int / bool / str / **array (np.ndarray、v2.31.0 SP-5)** の 5 型。
array は要素数上限 ``ARRAY_MAX_ELEMENTS`` (既定 10^7) に従う。

array の記録規約 (SP-5):
- ``var_assigned`` イベントと ``snapshot()`` には **ndarray 本体を入れず**、
  要約 (dtype / shape / size / 先頭数点 / mean / min / max) を記録する
  (timeline / result JSON の肥大防止)。
- ndarray 本体の資産へのフル保存 (npy) は SP-6 以降で検討 (今回はスコープ外)。
- 式評価用の ``as_ctx()`` は本体をそのまま返す。
"""
from __future__ import annotations
import re
from typing import Any

import numpy as np

from lab_executor.utils.seq_expression import ARRAY_MAX_ELEMENTS

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_RESERVED = {"params", "steps", "vars", "env", "value", "np"}


class VariableStoreError(ValueError):
    """変数登録エラー (命名規則違反・予約語・型違反)。"""


def validate_var_name(name: Any) -> None:
    """変数名の検証 (命名規則 + 予約語)。違反は ``VariableStoreError``。"""
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise VariableStoreError(
            f"不正な変数名: {name!r} ([a-z][a-z0-9_]* が必要)"
        )
    if name in _RESERVED:
        raise VariableStoreError(f"予約語は変数名に使えません: {name!r}")


def summarize_array(arr: np.ndarray) -> dict[str, Any]:
    """ndarray の要約 (timeline / result スナップショット用、SP-5)。

    本体は入れない。JSON 直列化可能な Python プリミティブのみで構成する。
    """
    out: dict[str, Any] = {
        "__type__": "array",
        "dtype": str(arr.dtype),
        "shape": list(arr.shape),
        "size": int(arr.size),
        "head": [
            (v.item() if isinstance(v, np.generic) else v)
            for v in arr.ravel()[:5].tolist()
        ] if arr.size else [],
    }
    if arr.size and arr.dtype.kind in "iuf":
        out["mean"] = float(arr.mean())
        out["min"] = float(arr.min())
        out["max"] = float(arr.max())
    return out


def _display_value(value: Any) -> Any:
    """イベント / スナップショット用の表示値 (array は要約に落とす)。"""
    if isinstance(value, np.ndarray):
        return summarize_array(value)
    return value


def _coerce(value: Any) -> Any:
    """許可する型 (bool/int/float/str/ndarray) のみ通す。それ以外は TypeError。

    v2.31.0 (SP-5): ndarray を 5 番目の型として許可 (要素数上限付き)。
    NumPy スカラは Python スカラへ自動昇格 (spec §3)。
    """
    # bool は int のサブクラスだが明示的に許可
    if isinstance(value, bool):
        return value
    # NumPy スカラの判定は素の float 判定より先に行う
    # (np.float64 は Python float のサブクラスであり、後に置くと
    #  .item() 変換に到達せず np 型のまま素通りする)
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, (int, float, str)):
        return value
    if isinstance(value, np.ndarray):
        if value.ndim == 0:
            return value.item()
        if value.size > ARRAY_MAX_ELEMENTS:
            raise VariableStoreError(
                f"配列の要素数 ({value.size}) が上限 ({ARRAY_MAX_ELEMENTS}) を"
                "超えています (spec §3 のメモリ暴走防止)"
            )
        return value
    raise TypeError(
        f"サポートされない変数型: {type(value).__name__} "
        "(float/int/bool/str/array のみ許可)"
    )


class VariableStore:
    """実行時変数ストア。

    ``events`` は代入毎に追記される ``var_assigned`` payload のリスト。
    呼び出し側 (Job 経路) がこれを timeline へ流す。
    """

    def __init__(self, params: dict[str, Any], env: dict[str, Any]):
        self._params = dict(params or {})
        self._env = dict(env or {})
        self._steps: dict[str, Any] = {}
        self._vars: dict[str, Any] = {}
        self.events: list[dict[str, Any]] = []

    def _validate_name(self, name: Any) -> None:
        validate_var_name(name)

    def set_step(
        self, name: str, value: Any, *, source_step_path: str, unit: str = "",
    ) -> Any:
        """capture 値を steps.* に登録する。"""
        self._validate_name(name)
        v = _coerce(value)
        self._steps[name] = v
        self._record("steps", name, v, source_step_path, "", unit)
        return v

    def set_var(
        self, name: str, value: Any, *,
        source_step_path: str, expr: str = "", unit: str = "",
    ) -> Any:
        """compute 値を vars.* に登録する (再代入可)。"""
        self._validate_name(name)
        v = _coerce(value)
        self._vars[name] = v
        self._record("vars", name, v, source_step_path, expr, unit)
        return v

    def _record(
        self, namespace: str, name: str, value: Any,
        source_step_path: str, expr: str, unit: str,
    ) -> None:
        self.events.append({
            "name": name,
            "namespace": namespace,
            # array は要約形で記録 (本体は timeline に載せない、SP-5)
            "value": _display_value(value),
            "source_step_path": source_step_path,
            "expr": expr,
            "unit": unit,
        })

    def as_ctx(self) -> dict[str, dict]:
        """``seq_expression.evaluate`` に渡す評価コンテキストを返す。

        array は本体をそのまま返す (式から np.* 関数で扱えるように)。
        """
        return {
            "params": dict(self._params),
            "steps": dict(self._steps),
            "vars": dict(self._vars),
            "env": dict(self._env),
        }

    def snapshot(self) -> dict[str, dict]:
        """result 格納用のスナップショット (steps / vars の最終値)。

        array は要約形 (`summarize_array`) に落とす (result JSON の肥大防止。
        ndarray 本体の資産保存は SP-6 以降で npy を検討)。
        """
        return {
            "steps": {k: _display_value(v) for k, v in self._steps.items()},
            "vars": {k: _display_value(v) for k, v in self._vars.items()},
        }

    # --- env 操作 (ランタイム内部専用、レシピからは読み取り専用) ---
    # v2.29.0 (SP-3): repeat が env.loop_index を供給するために使う。
    # レシピ側から env へ書く手段は提供しない (仕様 §3: env は読み取り専用)。

    def set_env(self, name: str, value: Any) -> None:
        self._env[name] = value

    def get_env(self, name: str, default: Any = None) -> Any:
        return self._env.get(name, default)

    def del_env(self, name: str) -> None:
        self._env.pop(name, None)
