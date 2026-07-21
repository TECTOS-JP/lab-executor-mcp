"""v1.0: Stability policy + bundle + schema status tests"""
from __future__ import annotations
import hashlib
import json
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from lab_executor.job import JobManager, JobStore
from lab_executor.job.state_machine import JobStatus
from lab_executor.models.instrument_def import InstrumentDefinition
from lab_visa_mcp.session_manager import InstrumentSession
from lab_executor.system_config import SystemConfig, InstrumentBinding


ROOT = Path(__file__).parent.parent


# =========================================================
# 1. Version + __init__
# =========================================================


def test_docs_v1_stability_policy_exists():
    p = ROOT / "docs" / "v1_stability_policy.md"
    assert p.exists()
    text = p.read_text(encoding="utf-8")
    for kw in ("v1.x", "Stable MCP tools", "Experimental MCP tools",
               "Deprecation policy", "What is NOT guaranteed",
               "response envelope", "error_class", "blocked"):
        assert kw in text, f"v1_stability_policy.md に {kw!r} 無し"


def test_compatibility_md_points_to_v1_policy():
    text = (ROOT / "docs" / "compatibility.md").read_text(encoding="utf-8")
    assert "v1.0" in text
    assert "v1_stability_policy" in text


# =========================================================
# 3. Schema status: stable
# =========================================================


SCHEMA_FILES = [
    "instrument.schema.json",
    "system_config.schema.json",
    "dsl.schema.json",
    "benchmark_task.schema.json",
]


@pytest.mark.parametrize("name", SCHEMA_FILES)
def test_schema_status_is_stable(name):
    p = ROOT / "schemas" / name
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data.get("x-visa-mcp-status") == "stable"
    assert data.get("x-compatibility") == "v1.x-compatible"


# =========================================================
# 4. error_taxonomy: lock_conflict / lock_stale が deprecated 明記
# =========================================================


def test_lock_conflict_deprecated_to_blocked():
    text = (ROOT / "docs" / "error_taxonomy.md").read_text(encoding="utf-8")
    # lock_conflict が deprecated として明記され blocked への統一が書かれている
    assert "lock_conflict" in text
    assert "deprecated" in text.lower()
    assert "blocked" in text


# =========================================================
# 5. export_experiment_bundle
# =========================================================


YAML_PSU = """
metadata: { manufacturer: T, model: PSU, category: power_supply }
commands:
  measure_voltage:
    scpi: "MEAS:VOLT?"
    type: query
"""


def _setup_with_job(tmp_path):
    d = InstrumentDefinition(**yaml.safe_load(YAML_PSU))
    sessions = {
        "psu001": InstrumentSession(
            resource_name="psu001", idn_response="<x>",
            idn_parsed={}, definition=d,
        ),
    }

    class _SM:
        def get_session(self, name):
            return sessions.get(name)

    sys_cfg = SystemConfig(
        instruments={"psu001": InstrumentBinding(resource="psu001")},
    )
    visa = MagicMock()
    visa.write = AsyncMock(return_value=None)
    visa.query = AsyncMock(return_value="5.0")
    store = JobStore(db_path=tmp_path / "j.sqlite")
    mgr = JobManager(visa, _SM(), store=store, system_config=sys_cfg)
    # Job + 1 step + 1 event
    store.create_job("job_b", "alice", "psu001", "<r>", {})
    store.transition_status("job_b", JobStatus.RUNNING)
    sid = store.record_step_started("job_b", 0, "query")
    store.record_step_completed(
        sid, status="ok",
        result={"command": "measure_voltage", "value": 5.0, "unit": "V"},
    )
    store.record_event("job_b", "step_completed", payload={"foo": "bar"})
    store.transition_status("job_b", JobStatus.COMPLETED,
                            last_step_summary="done",
                            result={"success": True})
    return _SM(), mgr, store


@pytest.mark.asyncio
async def test_export_bundle_creates_zip_with_manifest(tmp_path, monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    monkeypatch.setattr(
        "lab_executor.tools.export.DEFAULT_EXPORT_DIR",
        tmp_path / "exports",
    )
    sm, mgr, store = _setup_with_job(tmp_path)
    try:
        from fastmcp import FastMCP
        from lab_executor.tools.export import register_tools
        mcp = FastMCP("t")
        register_tools(mcp, mgr)
        tool = await mcp.get_tool("export_experiment_bundle")
        res = await tool.run({"job_id": "job_b"})
        data = (res.structured_content or {}).get("data") or {}
        path = Path(data["path"])
        assert path.exists() and path.suffix == ".zip"
        assert data["bundle_version"] == "1.0"
        # zip 中身
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            assert "manifest.json" in names
            assert "plan.json" in names or len(names) >= 5
            assert "job_record.json" in names
            assert "timeline.jsonl" in names
            assert "results.jsonl" in names
            assert "results.csv" in names
            manifest = json.loads(zf.read("manifest.json"))
        assert manifest["bundle_version"] == "1.0"
        assert manifest["job_id"] == "job_b"
        assert "checksums" in manifest
        # sha256 値の長さは 64
        for k, v in manifest["checksums"].items():
            assert len(v) == 64
    finally:
        store.close()


@pytest.mark.asyncio
async def test_export_bundle_writes_consistent_sha256(tmp_path, monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    monkeypatch.setattr(
        "lab_executor.tools.export.DEFAULT_EXPORT_DIR",
        tmp_path / "exports",
    )
    sm, mgr, store = _setup_with_job(tmp_path)
    try:
        from fastmcp import FastMCP
        from lab_executor.tools.export import register_tools
        mcp = FastMCP("t")
        register_tools(mcp, mgr)
        tool = await mcp.get_tool("export_experiment_bundle")
        res = await tool.run({"job_id": "job_b"})
        data = (res.structured_content or {}).get("data") or {}
        path = Path(data["path"])
        # manifest.json 内の各 file の sha256 が zip 中身と一致
        with zipfile.ZipFile(path) as zf:
            manifest = json.loads(zf.read("manifest.json"))
            for name, expected in manifest["checksums"].items():
                actual = hashlib.sha256(zf.read(name)).hexdigest()
                assert actual == expected, f"{name} sha256 mismatch"
        # 外側 zip 全体の sha256 も response の sha256 と一致
        outer = hashlib.sha256(path.read_bytes()).hexdigest()
        assert outer == data["sha256"]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_export_bundle_rejects_path_traversal(tmp_path, monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    monkeypatch.setattr(
        "lab_executor.tools.export.DEFAULT_EXPORT_DIR",
        tmp_path / "exports",
    )
    sm, mgr, store = _setup_with_job(tmp_path)
    try:
        from fastmcp import FastMCP
        from lab_executor.tools.export import register_tools
        mcp = FastMCP("t")
        register_tools(mcp, mgr)
        tool = await mcp.get_tool("export_experiment_bundle")
        res = await tool.run({
            "job_id": "job_b", "output_path": "../escape.zip",
        })
        out = res.structured_content or {}
        assert out.get("status") == "error"
        assert any(e["error_class"] == "invalid_export_path"
                   for e in (out.get("errors") or []))
    finally:
        store.close()


@pytest.mark.asyncio
async def test_export_bundle_rejects_existing_without_overwrite(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    monkeypatch.setattr(
        "lab_executor.tools.export.DEFAULT_EXPORT_DIR",
        tmp_path / "exports",
    )
    sm, mgr, store = _setup_with_job(tmp_path)
    try:
        from fastmcp import FastMCP
        from lab_executor.tools.export import register_tools
        mcp = FastMCP("t")
        register_tools(mcp, mgr)
        tool = await mcp.get_tool("export_experiment_bundle")
        # 1 回目 OK
        res1 = await tool.run({"job_id": "job_b"})
        assert (res1.structured_content or {}).get("status") == "ok"
        # 2 回目は overwrite=False で拒否
        res2 = await tool.run({"job_id": "job_b"})
        out = res2.structured_content or {}
        assert out.get("status") == "error"
        # overwrite=True で再試行成功
        res3 = await tool.run({"job_id": "job_b", "overwrite": True})
        assert (res3.structured_content or {}).get("status") == "ok"
    finally:
        store.close()


# =========================================================
# 6. README links to core docs
# =========================================================


def test_readme_links_to_v1_stability_policy():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "v1_stability_policy" in text


def test_readme_lists_export_experiment_bundle():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "export_experiment_bundle" in text
