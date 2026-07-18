# Backend integration contract

This document defines the semver-stable runtime surface that an external
instrument backend package may depend on. Changes to this surface require a
major-version compatibility decision.

## `InstrumentBackend`

Import from `lab_executor.backends.base`. The runtime-checkable protocol has
exactly four members; it must not be extended for routing metadata.

```python
backend_id: str

async def list_resources() -> list[str]: ...

async def query(
    resource_name: str,
    command: str,
    timeout_ms: int = 5000,
    read_termination: str = "\n",
    write_termination: str = "\n",
) -> str: ...

async def write(
    resource_name: str,
    command: str,
    timeout_ms: int = 5000,
    read_termination: str = "\n",
    write_termination: str = "\n",
) -> None: ...
```

`timeout_ms` is milliseconds. Termination values are transport strings and are
passed through unchanged. Resource names are opaque, case-sensitive strings.

An implementation may additionally expose synchronous `close()`. It is not a
protocol member. If present it must be idempotent, must not raise, and should
release resources on a best-effort basis. The conformance kit calls it twice;
runtime composition isolates failures between children.

The reusable conformance assertion is:

```python
from lab_executor.testing.backend_conformance import assert_backend_contract

await assert_backend_contract(backend, sample_resource="MODBUS::1")
```

## Server composition

- `lab_executor.server.compose_server(backend, *, name="lab-executor",
  enable_experimental=True, store_path=None) -> (mcp, job_manager)`
- `lab_executor.server.create_server(backend, *, name="lab-executor",
  enable_experimental=True) -> mcp`
- `lab_executor.control_plane.run_mcp_with_control(mcp, job_mgr, control_port,
  *, backend_id, control_path=None)`

Backend packages inject an `InstrumentBackend`; they do not register MCP tools.

## Resource-name prefixes

Each backend registration declares the resource prefixes it owns, for example
`("MODBUS::",)`, `("BLE::",)`, or VISA prefixes such as `("USB", "GPIB")`.
Matching is case-sensitive `str.startswith`; the longest matching prefix wins.
Duplicate prefixes across registrations are rejected at construction time. An
empty prefix tuple owns no routed resources. A resource with no match raises
`ResourceRoutingError`; there is no default-backend fallback.

Backends return already-prefixed resource names from `list_resources()`.
Composition never adds a prefix.

## Entry-point registration

The entry-point group is `lab_executor.backends`:

```toml
[project.entry-points."lab_executor.backends"]
modbus = "modbus_mcp.backend:make_backend"
```

The loaded object is a synchronous factory:

```python
def make_backend(config: dict | None = None) -> BackendRegistration: ...
```

`BackendRegistration` contains `backend: InstrumentBackend` and
`prefixes: tuple[str, ...]`. Factory/import failure disables only that child and
emits a warning; it never grants its resources to another backend.

## Declarative selection

`lab-executor serve --backends visa,modbus` has highest priority. If absent, a
`backends:` section in `_system.yaml` selects names and per-backend config. If
neither is present, the legacy `--backend` value is used. Exactly one selected
backend is returned directly and is not wrapped in `CompositeBackend`; this
preserves the existing `--backend mock` path.
