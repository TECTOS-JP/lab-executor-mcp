"""Entry-point discovery for instrument backends (BEF-2)."""
from __future__ import annotations

from dataclasses import dataclass
from importlib import metadata
import warnings
from typing import Any

from lab_executor.backends.base import InstrumentBackend
from lab_executor.backends.mock_backend import MockBackend


ENTRY_POINT_GROUP = "lab_executor.backends"


@dataclass(frozen=True)
class BackendRegistration:
    """A backend instance and the resource-name prefixes it owns."""

    backend: InstrumentBackend
    prefixes: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.backend, InstrumentBackend):
            raise TypeError("backend must implement InstrumentBackend")
        if not isinstance(self.prefixes, tuple):
            raise TypeError("prefixes must be tuple[str, ...]")
        if any(not isinstance(prefix, str) or not prefix for prefix in self.prefixes):
            raise ValueError("prefixes must contain only non-empty strings")
        if len(set(self.prefixes)) != len(self.prefixes):
            raise ValueError("a registration must not contain duplicate prefixes")


def make_mock_backend(config: dict[str, Any] | None = None) -> BackendRegistration:
    """Create the bundled mock registration.

    Mock owns no routed prefix. It remains useful as a direct, single backend,
    but can never become an implicit fallback child in a composite.
    """
    del config
    return BackendRegistration(backend=MockBackend(), prefixes=())


class _BuiltinMockEntryPoint:
    """Source-tree fallback until newly declared package metadata is installed."""

    name = "mock"

    @staticmethod
    def load():
        return make_mock_backend


def _entry_points() -> list[Any]:
    discovered = list(metadata.entry_points(group=ENTRY_POINT_GROUP))
    if not any(entry_point.name == "mock" for entry_point in discovered):
        discovered.append(_BuiltinMockEntryPoint())
    return discovered


def discover_backends(
    names: list[str] | None,
) -> list[BackendRegistration]:
    """Discover and instantiate selected backend entry points."""
    return _discover_backends(names, configs=None)


def _discover_backends(
    names: list[str] | None,
    *,
    configs: dict[str, dict[str, Any] | None] | None = None,
) -> list[BackendRegistration]:
    """Internal configured discovery used by declarative composition.

    A broken or missing child is excluded with a warning. This is deliberate
    partial degradation: routing remains fail-closed for every excluded
    child's resources once the surviving registrations are composed.
    """
    requested = None if names is None else list(dict.fromkeys(names))
    configs = configs or {}
    by_name: dict[str, list[Any]] = {}
    for entry_point in _entry_points():
        by_name.setdefault(entry_point.name, []).append(entry_point)

    selected_names = list(by_name) if requested is None else requested
    registrations: list[BackendRegistration] = []
    for name in selected_names:
        candidates = by_name.get(name, [])
        if not candidates:
            warnings.warn(
                f"backend {name!r} is not installed; excluding it",
                RuntimeWarning,
                stacklevel=2,
            )
            continue
        if len(candidates) != 1:
            warnings.warn(
                f"backend {name!r} has duplicate entry points; excluding it",
                RuntimeWarning,
                stacklevel=2,
            )
            continue
        try:
            factory = candidates[0].load()
            registration = factory(configs.get(name))
            if not isinstance(registration, BackendRegistration):
                raise TypeError("factory must return BackendRegistration")
        except Exception as exc:
            warnings.warn(
                f"backend {name!r} failed to initialize and was excluded: {exc}",
                RuntimeWarning,
                stacklevel=2,
            )
            continue
        registrations.append(registration)
    return registrations


def select_backend(registrations: list[BackendRegistration]) -> InstrumentBackend:
    """Return one backend directly, or compose two or more registrations."""
    if not registrations:
        raise ValueError("no usable backends were selected")
    if len(registrations) == 1:
        return registrations[0].backend
    from lab_executor.backends.composite import CompositeBackend

    return CompositeBackend(registrations)


__all__ = [
    "BackendRegistration",
    "ENTRY_POINT_GROUP",
    "discover_backends",
    "make_mock_backend",
    "select_backend",
]
