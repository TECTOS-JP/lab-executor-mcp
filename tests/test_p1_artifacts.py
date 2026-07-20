from __future__ import annotations

import hashlib
import json
from unittest.mock import MagicMock

import pytest

from lab_executor.artifact import ArtifactReferenceError, parse_artifact_reference
from lab_executor.asset.levels import judge_l0
from lab_executor.system_config import ArtifactsConfig, SystemConfig
from lab_executor.tools.export import build_bundle_files


def _reference(name: str, blob: bytes = b"waveform") -> dict:
    return {
        "artifact": "v1",
        "name": name,
        "sha256": hashlib.sha256(blob).hexdigest(),
        "bytes": len(blob),
        "shape": [10000, 1],
        "rate_hz": 250000.0,
        "unit": "V",
    }


def _result_string(reference: dict) -> str:
    return json.dumps(reference, separators=(",", ":"))


def _manager(job_store, job_id: str, config: SystemConfig) -> MagicMock:
    manager = MagicMock()
    manager.store = job_store
    manager.system_config = config
    manager.audit = None
    manager.get.return_value = job_store.get(job_id)
    return manager


def _seed_artifact_job(job_store, seed_job, job_id: str, value: str) -> None:
    seed_job(job_store, job_id)
    row_id = job_store.record_step_started(job_id, 0, "command")
    job_store.record_step_completed(
        row_id,
        status="ok",
        result={"command": "acquire", "raw_response": value},
    )


def test_parse_valid_artifact_reference():
    raw = _reference("acq-20260720-101530-ai0.npz")
    parsed = parse_artifact_reference(_result_string(raw))
    assert parsed is not None
    assert parsed.to_dict() == {key: raw[key] for key in raw if key != "artifact"}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("artifact", "v2"),
        ("sha256", "g" * 64),
        ("sha256", "a" * 63),
        ("bytes", -1),
        ("rate_hz", float("inf")),
        ("rate_hz", float("nan")),
        ("unit", ""),
    ],
)
def test_parse_rejects_invalid_reference_fields(field, value):
    raw = _reference("acq.npz")
    raw[field] = value
    with pytest.raises(ArtifactReferenceError):
        parse_artifact_reference(_result_string(raw))


@pytest.mark.parametrize(
    "name",
    [
        "../secret",
        "../../etc/passwd",
        "/etc/passwd",
        r"\etc\passwd",
        r"C:\Windows\x",
        "C:/Windows/x",
        r"\\server\share\f.npz",
        "sub/dir/f.npz",
        r"sub\dir\f.npz",
        "bad\x00name",
        # ``.`` denotes the artifact root directory rather than a file; it must
        # fail at the boundary rather than downstream when the read fails.
        ".",
        "..",
    ],
)
def test_parse_rejects_path_traversal(name):
    with pytest.raises(ArtifactReferenceError):
        parse_artifact_reference(_result_string(_reference(name)))


@pytest.mark.parametrize("value", ["1.25", "not json", '{"value": 1.25}'])
def test_plain_scalar_is_not_an_artifact(value):
    assert parse_artifact_reference(value) is None


def test_plain_scalar_bundle_is_unchanged(job_store, seed_job):
    job_id = "plain_scalar"
    _seed_artifact_job(job_store, seed_job, job_id, "1.25")
    files = build_bundle_files(_manager(job_store, job_id, SystemConfig()), job_id)
    assert "artifacts/index.json" not in files
    assert b'"value": "1.25"' in files["results.jsonl"]


@pytest.mark.parametrize(
    ("limit", "expected_status", "expected_embedded"),
    [(8, "embedded", True), (7, "referenced", False)],
)
def test_artifact_embedding_threshold(
    job_store, seed_job, tmp_path, limit, expected_status, expected_embedded,
):
    blob = b"waveform"
    name = "acq.npz"
    (tmp_path / name).write_bytes(blob)
    reference = _reference(name, blob)
    job_id = f"threshold_{limit}"
    raw = _result_string(reference)
    _seed_artifact_job(job_store, seed_job, job_id, raw)
    config = SystemConfig(
        artifacts=ArtifactsConfig(root=tmp_path, embed_max_bytes=limit),
    )

    files = build_bundle_files(_manager(job_store, job_id, config), job_id)
    entry = json.loads(files["artifacts/index.json"])[0]
    assert entry == {
        **{key: value for key, value in reference.items() if key != "artifact"},
        "embedded": expected_embedded,
        "status": expected_status,
    }
    assert ("artifacts/acq.npz" in files) is expected_embedded
    result_row = json.loads(files["results.jsonl"].decode("utf-8"))
    assert result_row["value"] == raw

    manifest = json.loads(files["manifest.json"])
    artifact_contents = [
        item for item in manifest["contents"] if isinstance(item, dict)
    ]
    if expected_embedded:
        assert artifact_contents == [{
            "path": "artifacts/acq.npz",
            "sha256": reference["sha256"],
            "kind": "results",
        }]
        assert manifest["checksums"]["artifacts/acq.npz"] == reference["sha256"]
    else:
        assert artifact_contents == []


@pytest.mark.parametrize("failure", ["missing", "mismatch", "unset_root"])
def test_unresolved_artifact_still_builds_bundle(
    job_store, seed_job, tmp_path, failure,
):
    blob = b"waveform"
    name = "acq.npz"
    if failure == "mismatch":
        (tmp_path / name).write_bytes(b"different")
    reference = _reference(name, blob)
    job_id = f"unresolved_{failure}"
    _seed_artifact_job(job_store, seed_job, job_id, _result_string(reference))
    config = SystemConfig(
        artifacts=ArtifactsConfig(root=None if failure == "unset_root" else tmp_path),
    )

    files = build_bundle_files(_manager(job_store, job_id, config), job_id)
    entry = json.loads(files["artifacts/index.json"])[0]
    assert entry["status"] == "unresolved"
    assert entry["embedded"] is False
    assert entry["reason"]
    assert "artifacts/acq.npz" not in files
    assert "manifest.json" in files


def test_artifact_only_result_still_passes_l0(job_store, seed_job):
    job_id = "artifact_l0"
    _seed_artifact_job(
        job_store, seed_job, job_id, _result_string(_reference("missing.npz")),
    )
    files = build_bundle_files(_manager(job_store, job_id, SystemConfig()), job_id)
    rows = [line for line in files["results.jsonl"].splitlines() if line]
    assert len(rows) == 1
    assert judge_l0(results_row_count=len(rows))["ok"] is True


def test_system_config_loads_artifacts_section(tmp_path):
    config_path = tmp_path / "_system.yaml"
    config_path.write_text(
        "artifacts:\n  root: artifacts-cache\n  embed_max_bytes: 123\n",
        encoding="utf-8",
    )
    config = SystemConfig.from_yaml(config_path)
    assert config.artifacts.root is not None
    assert str(config.artifacts.root) == "artifacts-cache"
    assert config.artifacts.embed_max_bytes == 123
    assert SystemConfig().artifacts.embed_max_bytes == 33554432
