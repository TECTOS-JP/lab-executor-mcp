"""Strict resource-prefix routing across multiple instrument backends."""
from __future__ import annotations

import logging

from lab_executor.backends.discovery import BackendRegistration


logger = logging.getLogger(__name__)


class ResourceRoutingError(RuntimeError):
    """No configured backend owns the requested resource name."""

    error_class = "ResourceRoutingError"


class CompositeBackend:
    """InstrumentBackend implementation using longest-prefix routing."""

    backend_id = "composite"

    def __init__(self, registrations: list[BackendRegistration]) -> None:
        if not registrations:
            raise ValueError("CompositeBackend requires at least one registration")
        self._registrations = tuple(registrations)
        claimed: dict[str, object] = {}
        routes: list[tuple[str, object]] = []
        for registration in registrations:
            for prefix in registration.prefixes:
                if prefix in claimed:
                    raise ValueError(
                        f"duplicate backend resource prefix: {prefix!r}"
                    )
                claimed[prefix] = registration.backend
                routes.append((prefix, registration.backend))
        self._routes = tuple(sorted(routes, key=lambda route: len(route[0]), reverse=True))
        self._closed = False

    def _route(self, resource_name: str):
        for prefix, backend in self._routes:
            if resource_name.startswith(prefix):
                return backend
        raise ResourceRoutingError(
            f"no configured backend owns resource {resource_name!r}"
        )

    async def list_resources(self) -> list[str]:
        resources: list[str] = []
        seen: set[str] = set()
        for registration in self._registrations:
            for resource in await registration.backend.list_resources():
                if resource not in seen:
                    seen.add(resource)
                    resources.append(resource)
        return resources

    async def query(
        self,
        resource_name: str,
        command: str,
        timeout_ms: int = 5000,
        read_termination: str = "\n",
        write_termination: str = "\n",
    ) -> str:
        backend = self._route(resource_name)
        return await backend.query(
            resource_name,
            command,
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
        backend = self._route(resource_name)
        await backend.write(
            resource_name,
            command,
            timeout_ms=timeout_ms,
            read_termination=read_termination,
            write_termination=write_termination,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for registration in self._registrations:
            close = getattr(registration.backend, "close", None)
            if close is None:
                continue
            try:
                close()
            except Exception as exc:
                logger.warning(
                    "backend %r close failed: %s",
                    registration.backend.backend_id,
                    exc,
                )


__all__ = ["CompositeBackend", "ResourceRoutingError"]
