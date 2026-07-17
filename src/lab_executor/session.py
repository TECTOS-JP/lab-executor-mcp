"""v2.3.0: SessionFacade Protocol — session lookup contract.

lab-executor runtime / tools が要求する session lookup の最小
surface を Protocol として明文化する。`server._SessionFacade` の
class-level shape と、`visa-mcp` 側 `SessionManager` の両方が
互換であることを揃える土台。

v2.3 では新規追加のみ (既存 _SessionFacade / SessionManager との
動作互換は維持)。
"""
from __future__ import annotations
from typing import Any, Callable, Protocol, TypeAlias, runtime_checkable


# polling_executor とシーケンス実行経路で共有する、最小の session 解決契約。
SessionResolver: TypeAlias = Callable[[str], Any | None]


@runtime_checkable
class SessionFacade(Protocol):
    """Backend-independent session lookup contract (v2.3.0)

    実装候補:
      - `lab_executor.server._SessionFacade` (内部 Mock 用)
      - `visa_mcp.session_manager.SessionManager` (実機 backend)

    runtime / tools は `get_session(name)` 経由で session を取得し、
    必要に応じて backend (`InstrumentBackend`) と組み合わせて
    実機 / mock query/write を実行する。
    """

    def get_session(self, resource: str) -> Any: ...
