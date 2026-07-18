"""BEF-2 backend discovery, routing, and server integration tests."""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import fields
import inspect
import os
import subprocess
import sys

import pytest

from lab_executor.backends import (
    BackendRegistration,
    CompositeBackend,
    MockBackend,
    ResourceRoutingError,
    discover_backends,
    select_backend,
)
from lab_executor.backends import discovery
from lab_executor.server import compose_server, list_registered_tools
from lab_executor.testing.backend_conformance import assert_backend_contract


class _SpyBackend:
    def __init__(self, backend_id: str, resources: list[str] | None = None):
        self.backend_id = backend_id
        self.resources = resources or []
        self.calls: list[tuple] = []
        self.close_calls = 0

    async def list_resources(self) -> list[str]:
        self.calls.append(("list_resources",))
        return list(self.resources)

    async def query(
        self,
        resource_name: str,
        command: str,
        timeout_ms: int = 5000,
        read_termination: str = "\n",
        write_termination: str = "\n",
    ) -> str:
        self.calls.append(
            (
                "query", resource_name, command, timeout_ms,
                read_termination, write_termination,
            )
        )
        return f"{self.backend_id}:{command}"

    async def write(
        self,
        resource_name: str,
        command: str,
        timeout_ms: int = 5000,
        read_termination: str = "\n",
        write_termination: str = "\n",
    ) -> None:
        self.calls.append(
            (
                "write", resource_name, command, timeout_ms,
                read_termination, write_termination,
            )
        )

    def close(self) -> None:
        self.close_calls += 1


def _composite() -> tuple[CompositeBackend, _SpyBackend, _SpyBackend]:
    broad = _SpyBackend("broad", ["BUS::1", "SHARED"])
    narrow = _SpyBackend("narrow", ["BUS::SPECIAL::1", "SHARED"])
    composite = CompositeBackend(
        [
            BackendRegistration(broad, ("BUS::",)),
            BackendRegistration(narrow, ("BUS::SPECIAL::",)),
        ]
    )
    return composite, broad, narrow


def test_registration_has_exact_public_fields():
    assert [field.name for field in fields(BackendRegistration)] == [
        "backend", "prefixes",
    ]


def test_discovery_public_signature_is_frozen():
    assert tuple(inspect.signature(discover_backends).parameters) == ("names",)


def test_longest_prefix_routes_query_and_preserves_arguments():
    composite, broad, narrow = _composite()
    result = asyncio.run(
        composite.query("BUS::SPECIAL::7", "READ?", 123, "R", "W")
    )
    assert result == "narrow:READ?"
    assert broad.calls == []
    assert narrow.calls == [
        ("query", "BUS::SPECIAL::7", "READ?", 123, "R", "W")
    ]


def test_write_routes_to_broad_prefix():
    composite, broad, narrow = _composite()
    asyncio.run(composite.write("BUS::7", "SET", 456, "r", "w"))
    assert broad.calls == [("write", "BUS::7", "SET", 456, "r", "w")]
    assert narrow.calls == []


def test_unknown_resource_fails_without_any_child_io():
    composite, broad, narrow = _composite()
    with pytest.raises(ResourceRoutingError) as exc_info:
        asyncio.run(composite.query("UNKNOWN::7", "DANGEROUS"))
    assert exc_info.value.error_class == "ResourceRoutingError"
    assert broad.calls == []
    assert narrow.calls == []


def test_empty_prefix_is_not_a_fallback():
    child = _SpyBackend("empty")
    composite = CompositeBackend([BackendRegistration(child, ())])
    with pytest.raises(ResourceRoutingError):
        asyncio.run(composite.write("ANY::1", "SET"))
    assert child.calls == []


def test_duplicate_prefix_rejected_at_construction():
    with pytest.raises(ValueError, match="duplicate backend resource prefix"):
        CompositeBackend(
            [
                BackendRegistration(_SpyBackend("one"), ("BUS::",)),
                BackendRegistration(_SpyBackend("two"), ("BUS::",)),
            ]
        )


def test_list_resources_aggregates_without_prefixing_and_deduplicates():
    composite, broad, narrow = _composite()
    assert asyncio.run(composite.list_resources()) == [
        "BUS::1", "SHARED", "BUS::SPECIAL::1",
    ]
    assert broad.calls == [("list_resources",)]
    assert narrow.calls == [("list_resources",)]


def test_close_is_idempotent_and_one_failure_does_not_block_other(caplog):
    first = _SpyBackend("first")
    second = _SpyBackend("second")

    def broken_close():
        first.close_calls += 1
        raise RuntimeError("boom")

    first.close = broken_close
    composite = CompositeBackend(
        [
            BackendRegistration(first, ("A::",)),
            BackendRegistration(second, ("B::",)),
        ]
    )
    composite.close()
    composite.close()
    assert first.close_calls == 1
    assert second.close_calls == 1
    assert "close failed" in caplog.text


def test_single_registration_is_returned_without_wrapper():
    backend = _SpyBackend("single")
    assert select_backend([BackendRegistration(backend, ("ONE::",))]) is backend


def test_bundled_mock_is_discoverable_and_owns_no_prefix():
    registrations = discover_backends(["mock"])
    assert len(registrations) == 1
    assert isinstance(registrations[0].backend, MockBackend)
    assert registrations[0].prefixes == ()


class _EntryPoint:
    def __init__(self, name, factory):
        self.name = name
        self._factory = factory

    def load(self):
        return self._factory


def test_discovery_passes_config_and_partially_degrades(monkeypatch):
    good = _SpyBackend("good")
    received = []

    def good_factory(config):
        received.append(config)
        return BackendRegistration(good, ("GOOD::",))

    def broken_factory(config):
        raise ImportError("driver unavailable")

    monkeypatch.setattr(
        discovery,
        "_entry_points",
        lambda: [
            _EntryPoint("good", good_factory),
            _EntryPoint("broken", broken_factory),
        ],
    )
    with pytest.warns(RuntimeWarning, match="broken.*excluded"):
        registrations = discovery._discover_backends(
            ["good", "broken"], configs={"good": {"port": "COM3"}},
        )
    assert received == [{"port": "COM3"}]
    composite = CompositeBackend(registrations)
    with pytest.raises(ResourceRoutingError):
        asyncio.run(composite.query("BROKEN::1", "READ?"))
    assert good.calls == []


def test_unknown_discovery_name_warns_and_is_excluded(monkeypatch):
    monkeypatch.setattr(discovery, "_entry_points", lambda: [])
    with pytest.warns(RuntimeWarning, match="not installed"):
        assert discover_backends(["missing"]) == []


def test_composite_satisfies_backend_conformance():
    composite, _broad, _narrow = _composite()
    returned = asyncio.run(
        assert_backend_contract(composite, sample_resource="BUS::SPECIAL::1")
    )
    assert returned is composite


def test_compose_server_accepts_composite_and_keeps_tool_surface():
    composite, _broad, _narrow = _composite()
    server, job_mgr = compose_server(composite)
    direct_server, _ = compose_server(MockBackend())
    assert set(list_registered_tools(server)) == set(
        list_registered_tools(direct_server)
    )
    assert job_mgr._visa is composite


def test_cli_backends_mock_dry_run():
    result = subprocess.run(
        [
            sys.executable, "-m", "lab_executor.cli", "serve",
            "--backends", "mock", "--dry-run",
        ],
        text=True,
        capture_output=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    assert result.returncode == 0, result.stderr
    assert "backends=mock" in result.stdout
    assert "backend_id: mock" in result.stdout
    assert "registered tools:" in result.stdout


def test_system_yaml_backend_selection(monkeypatch, tmp_path):
    instruments = tmp_path / "instruments"
    instruments.mkdir()
    (instruments / "_system.yaml").write_text(
        "backends:\n  alpha:\n    port: COM3\n  beta: {}\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    from lab_executor.cli import _resolve_declared_backends

    assert _resolve_declared_backends(
        argparse.Namespace(backends=None),
    ) == (["alpha", "beta"], {"alpha": {"port": "COM3"}, "beta": {}})


def test_cli_selection_has_priority_over_system_yaml(monkeypatch, tmp_path):
    instruments = tmp_path / "instruments"
    instruments.mkdir()
    (instruments / "_system.yaml").write_text(
        "backends:\n  yaml_backend: {}\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    from lab_executor.cli import _resolve_declared_backends

    assert _resolve_declared_backends(
        argparse.Namespace(backends="cli_one,cli_two"),
    ) == (["cli_one", "cli_two"], {})
