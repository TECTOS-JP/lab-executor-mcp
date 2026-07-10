"""
実行時変数コンテキスト (SP-1)

シーケンス処理拡張 (sequence_processing_spec §3) の変数モデルを実装する。
1 Job (= 1 レシピ実行) のスコープで、capture (steps.*) と compute (vars.*) の
値を保持し、代入毎に ``var_assigned`` イベントを蓄積する。

- ``params.*``: レシピパラメータ (コンパイル時解決済み、読み取り専用)
- ``steps.*``:  capture (result_as) で登録した測定値
- ``vars.*``:   compute で演算した派生値
- ``env.*``:    実行コンテキスト (job_id / started_at、読み取り専用)

型は float / int / bool / str の 4 型のみ (array 等は SP-5)。
"""
from __future__ import annotations
import re
from typing import Any

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_RESERVED = {"params", "steps", "vars", "env", "value"}


class VariableStoreError(ValueError):
    """変数登録エラー (命名規則違反・予約語・型違反)。"""


def _coerce(value: Any) -> Any:
    """許可する型 (bool/int/float/str) のみ通す。それ以外は TypeError。"""
    # bool は int のサブクラスだが明示的に許可
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, str)):
        return value
    raise TypeError(
        f"サポートされない変数型: {type(value).__name__} "
        "(float/int/bool/str のみ許可、array は SP-5)"
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
        if not isinstance(name, str) or not _NAME_RE.match(name):
            raise VariableStoreError(
                f"不正な変数名: {name!r} ([a-z][a-z0-9_]* が必要)"
            )
        if name in _RESERVED:
            raise VariableStoreError(f"予約語は変数名に使えません: {name!r}")

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
            "value": value,
            "source_step_path": source_step_path,
            "expr": expr,
            "unit": unit,
        })

    def as_ctx(self) -> dict[str, dict]:
        """``seq_expression.evaluate`` に渡す評価コンテキストを返す。"""
        return {
            "params": dict(self._params),
            "steps": dict(self._steps),
            "vars": dict(self._vars),
            "env": dict(self._env),
        }

    def snapshot(self) -> dict[str, dict]:
        """result 格納用のスナップショット (steps / vars の最終値)。"""
        return {"steps": dict(self._steps), "vars": dict(self._vars)}
