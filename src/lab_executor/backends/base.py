"""
v1.11: InstrumentBackend Protocol — backend / runtime separation boundary

v1.1 で spike 公開した Protocol を、v1.11 で **分離境界として実体化**
した。runtime module (lab-executor-mcp 候補) は `InstrumentBackend`
にのみ依存し、`VisaManager` 等を直接 import しない構造になっている。

- `PyVisaBackend` (visa-mcp 側) は `VisaManager` を内部に包む実装
- `MockBackend` (lab-executor-mcp 側) は PyVISA 非依存の adapter
- 既存 `VisaManager` / `MockVisaManager` も duck-typed に compatible で
  あり、v2.0 までは並走可能。

Protocol は **意図的に最小**: `list_resources` / `query` / `write` /
`close` / `backend_id`。async API / streaming / event subscription /
remote / plugin loading は v2.x 以降で慎重に検討する (v2.0 の目的は
分離であり、backend API の完成ではない)。

`timeout_ms` / `read_termination` / `write_termination` の単位は v1.1
spike 時点から維持。v2.0 公開境界としてこの形式を採用する。

詳細: `docs/backend_abstraction.md` / `docs/separation/notes.md` /
`docs/raw_visa.md`
"""
from __future__ import annotations
from typing import Protocol, runtime_checkable


@runtime_checkable
class InstrumentBackend(Protocol):
    """機器との通信を抽象化する backend (v1.11: 分離境界として確定)

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
