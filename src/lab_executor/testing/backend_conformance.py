"""Reusable assertions for the frozen InstrumentBackend contract (BEF-1)."""
from __future__ import annotations

import inspect
from typing import Any

from lab_executor.backends.base import InstrumentBackend


_IO_PARAMETERS = (
    ("resource_name", inspect.Parameter.empty),
    ("command", inspect.Parameter.empty),
    ("timeout_ms", 5000),
    ("read_termination", "\n"),
    ("write_termination", "\n"),
)


async def _resolve_backend(backend_or_factory: Any) -> Any:
    if (
        not inspect.isclass(backend_or_factory)
        and isinstance(backend_or_factory, InstrumentBackend)
    ):
        return backend_or_factory
    assert callable(backend_or_factory), (
        "backend must implement InstrumentBackend or be a zero-argument factory"
    )
    backend = backend_or_factory()
    if inspect.isawaitable(backend):
        backend = await backend
    return backend


def _assert_io_signature(method: Any, method_name: str) -> None:
    assert inspect.iscoroutinefunction(method), f"{method_name} must be async"
    parameters = tuple(inspect.signature(method).parameters.values())
    assert len(parameters) == len(_IO_PARAMETERS), (
        f"{method_name} signature has unexpected parameters: {parameters!r}"
    )
    for actual, (expected_name, expected_default) in zip(
        parameters, _IO_PARAMETERS, strict=True,
    ):
        assert actual.name == expected_name, (
            f"{method_name} parameter must be {expected_name!r}, got {actual.name!r}"
        )
        assert actual.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ), f"{method_name}.{actual.name} has unsupported parameter kind"
        assert actual.default == expected_default, (
            f"{method_name}.{actual.name} default must be {expected_default!r}, "
            f"got {actual.default!r}"
        )


async def assert_backend_contract(
    backend_or_factory: Any,
    *,
    sample_resource: str,
) -> Any:
    """Assert protocol shape and behavior, returning the resolved backend.

    The supplied sample resource must be safe for mock/conformance I/O. The
    assertion performs one query and one write using harmless placeholder
    commands, then invokes optional synchronous ``close`` twice.
    """
    backend = await _resolve_backend(backend_or_factory)

    assert isinstance(backend, InstrumentBackend), (
        "backend does not satisfy the runtime InstrumentBackend protocol"
    )
    assert isinstance(backend.backend_id, str) and backend.backend_id.strip(), (
        "backend_id must be a non-empty str"
    )
    assert isinstance(sample_resource, str) and sample_resource, (
        "sample_resource must be a non-empty str"
    )

    list_resources = backend.list_resources
    assert inspect.iscoroutinefunction(list_resources), "list_resources must be async"
    resources = await list_resources()
    assert isinstance(resources, list), "list_resources must return list[str]"
    assert all(isinstance(item, str) for item in resources), (
        "list_resources must return list[str]"
    )

    _assert_io_signature(backend.query, "query")
    _assert_io_signature(backend.write, "write")
    query_result = await backend.query(sample_resource, "*IDN?")
    assert isinstance(query_result, str), "query must return str"
    write_result = await backend.write(sample_resource, "CONF")
    assert write_result is None, "write must return None"

    if hasattr(backend, "prefixes"):
        prefixes = backend.prefixes
        assert isinstance(prefixes, tuple), "prefixes must be tuple[str, ...]"
        assert all(isinstance(prefix, str) and prefix for prefix in prefixes), (
            "prefixes must contain non-empty strings"
        )

    close = getattr(backend, "close", None)
    if close is not None:
        assert callable(close), "close must be callable"
        for _ in range(2):
            result = close()
            assert not inspect.isawaitable(result), "close must be synchronous"
            assert result is None, "close must return None"

    return backend


__all__ = ["assert_backend_contract"]
