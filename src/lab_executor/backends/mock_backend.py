"""MockBackend — PyVISA 非依存 backend for lab-executor-mcp (v2.0)

`InstrumentBackend` Protocol を満たす **lab-executor-mcp 同梱** の
mock 実装。benchmark / dry-run / CI で実機 backend が不要な経路に使う。

実機との通信が必要な場合は外部 backend package (`visa-mcp` 等) を
別途 install して、`PyVisaBackend` を runtime に注入する。

内部の `MockVisaManager` は v1.x からの legacy internal name で、
public 名は `MockBackend`。利用者は `MockBackend` のみ意識すればよい。

詳細: `docs/v2_migration.md` / `docs/backend_abstraction.md`
"""
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lab_executor.testing.mock_instruments import MockVisaManager


class MockBackend:
    """`InstrumentBackend` Protocol を満たす mock adapter (v1.11)

    `MockVisaManager` を内部に持ち、benchmark / dry-run / CI で PyVISA
    を必要としない backend として動作する。
    """

    backend_id: str = "mock"

    def __init__(self, mock_visa: "MockVisaManager | None" = None):
        if mock_visa is None:
            # lazy import (lab-executor 側で PyVISA 非依存)
            from lab_executor.testing.mock_instruments import (
                MockVisaManager as _MVM,
            )
            mock_visa = _MVM()
        self._mock: "MockVisaManager" = mock_visa

    async def list_resources(
        self, query: str = "?*::INSTR"
    ) -> list[str]:
        return await self._mock.list_resources(query)

    async def query(
        self,
        resource_name: str,
        command: str,
        timeout_ms: int = 5000,
        read_termination: str = "\n",
        write_termination: str = "\n",
    ) -> str:
        return await self._mock.query(
            resource_name, command,
            timeout_ms=timeout_ms,
            read_termination=read_termination,
            write_termination=write_termination,
        )

    async def write(
        self,
        resource_name: str,
        command: str,
        timeout_ms: int = 5000,
        read_termination: str = "\n",
        write_termination: str = "\n",
    ) -> None:
        await self._mock.write(
            resource_name, command,
            timeout_ms=timeout_ms,
            read_termination=read_termination,
            write_termination=write_termination,
        )

    def close(self) -> None:
        close_fn = getattr(self._mock, "close", None)
        if callable(close_fn):
            close_fn()

    @property
    def mock_visa(self) -> "MockVisaManager":
        return self._mock
