"""
InstrumentBackend Protocol — lab-executor-mcp の backend 公開境界 (v2.0)

`lab-executor-mcp` runtime が機器と通信する際の **backend-independent
contract**。runtime module は本 Protocol にのみ依存し、PyVISA 等の
特定 backend に直接依存しない。

実装:
- `lab_executor.backends.mock_backend.MockBackend`
  (lab-executor-mcp 同梱、PyVISA 非依存、benchmark / dry-run 用)
- `visa_mcp.backends.pyvisa_backend.PyVisaBackend`
  (visa-mcp 同梱、PyVISA 透過 adapter、実機通信用)

Protocol は **意図的に最小**: `backend_id` / `list_resources` /
`query` / `write` / `close` のみ。async API / streaming / event
subscription / remote backend / plugin loading は v2.x 以降で慎重に
検討する。

`timeout_ms` / `read_termination` / `write_termination` の単位は v1.1
spike 時点から維持。v2.0 公開境界としてこの形式を採用する。

詳細: `docs/backend_abstraction.md` / `docs/separation/notes.md`
"""
from __future__ import annotations
from typing import Protocol, runtime_checkable


@runtime_checkable
class InstrumentBackend(Protocol):
    """機器との通信を抽象化する backend (v2.0 公開境界)

    既存実装 (v1.11 時点):
      - ``visa_mcp.backends.pyvisa_backend.PyVisaBackend``
        (visa-mcp owner、`VisaManager` を包む)
      - ``lab_executor.backends.mock_backend.MockBackend``
        (lab-executor-mcp owner、`MockVisaManager` を包む)

    v2.x 以降の候補 (実装は別途判断):
      - replay: bundle の過去応答を deterministic に返す
      - rest:   REST device adapter
      - simulator: 数学モデルベース backend
    """

    backend_id: str

    async def list_resources(self) -> list[str]: ...

    async def query(
        self,
        resource_name: str,
        command: str,
        timeout_ms: int = 5000,
        read_termination: str = "\n",
        write_termination: str = "\n",
    ) -> str: ...

    async def write(
        self,
        resource_name: str,
        command: str,
        timeout_ms: int = 5000,
        read_termination: str = "\n",
        write_termination: str = "\n",
    ) -> None: ...
