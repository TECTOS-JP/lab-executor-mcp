"""v1.11: MockBackend adapter (lab-executor-mcp 側 / PyVISA 不要)

`InstrumentBackend` Protocol を満たす mock 実装。`MockVisaManager` を
包み、benchmark / dry-run / CI で PyVISA 非依存の動作を可能にする。

v2.0 では lab-executor-mcp 側に移送される (PyVISA 不要なため)。

詳細: `docs/separation/notes.md`
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
