"""Validation for backend-produced bulk-acquisition artifact references."""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import PurePath, PureWindowsPath
from typing import Any


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ArtifactReferenceError(ValueError):
    """An artifact-looking result failed closed validation."""


@dataclass(frozen=True)
class ArtifactReference:
    name: str
    sha256: str
    bytes: int
    shape: list[int]
    rate_hz: float
    unit: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "sha256": self.sha256,
            "bytes": self.bytes,
            "shape": list(self.shape),
            "rate_hz": self.rate_hz,
            "unit": self.unit,
        }


def parse_artifact_reference(result: str) -> ArtifactReference | None:
    """Return a validated reference, or ``None`` for a non-artifact result.

    Invalid JSON and JSON values without an ``artifact`` key remain ordinary
    scalar results. Once that key is present, every field is validated and an
    invalid reference raises :class:`ArtifactReferenceError`.
    """
    if not isinstance(result, str):
        return None
    try:
        raw = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(raw, dict) or "artifact" not in raw:
        return None
    if raw.get("artifact") != "v1":
        raise ArtifactReferenceError("artifact must be exactly 'v1'")

    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise ArtifactReferenceError("name must be a non-empty file name")
    windows_name = PureWindowsPath(name)
    if (
        "\x00" in name
        or "/" in name
        or "\\" in name
        or ".." in name
        or PurePath(name).is_absolute()
        or windows_name.is_absolute()
        or bool(windows_name.drive)
        # ``.`` names no file; it resolves to the artifact root directory
        # itself. Reading it fails downstream anyway, but a name that cannot
        # denote a file has no business passing the boundary check.
        or not PurePath(name).name
    ):
        raise ArtifactReferenceError("name must be a relative file name only")

    sha256 = raw.get("sha256")
    if not isinstance(sha256, str) or _SHA256_RE.fullmatch(sha256) is None:
        raise ArtifactReferenceError("sha256 must be 64 lowercase hex characters")

    byte_count = raw.get("bytes")
    if (
        isinstance(byte_count, bool)
        or not isinstance(byte_count, int)
        or byte_count < 0
    ):
        raise ArtifactReferenceError("bytes must be a non-negative integer")

    shape = raw.get("shape")
    if not isinstance(shape, list) or not shape or any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in shape
    ):
        raise ArtifactReferenceError(
            "shape must be a non-empty list of positive integers"
        )

    rate_hz = raw.get("rate_hz")
    if (
        isinstance(rate_hz, bool)
        or not isinstance(rate_hz, (int, float))
        or not math.isfinite(rate_hz)
        or rate_hz <= 0
    ):
        raise ArtifactReferenceError("rate_hz must be a finite positive number")

    unit = raw.get("unit")
    if not isinstance(unit, str) or not unit:
        raise ArtifactReferenceError("unit must be a non-empty string")

    return ArtifactReference(
        name=name,
        sha256=sha256,
        bytes=byte_count,
        shape=list(shape),
        rate_hz=float(rate_hz),
        unit=unit,
    )
