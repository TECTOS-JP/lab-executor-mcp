"""v1.1: Direction-setting release tests

- naming/repository strategy docs 存在
- backend_abstraction docs + InstrumentBackend Protocol 存在
- validate_experiment_bundle / inspect_experiment_bundle (experimental)
- stability.py 整合 (43 + 7 = 50)
- 新規 tools が experimental に登録されている
"""
from __future__ import annotations
import hashlib
import json
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from lab_executor import stability
from lab_executor.job import JobManager, JobStore
from lab_executor.job.state_machine import JobStatus
from lab_executor.models.instrument_def import InstrumentDefinition
from visa_mcp.session_manager import InstrumentSession
from lab_executor.system_config import SystemConfig, InstrumentBinding


ROOT = Path(__file__).parent.parent


# =========================================================
# version
# =========================================================


def test_naming_strategy_doc_exists():
    p = ROOT / "docs" / "naming_and_repository_strategy.md"
    assert p.exists()
    text = p.read_text(encoding="utf-8")
    for kw in ("v1.x", "visa-mcp", "lab-executor-mcp", "decision",
               "v1.1 decision", "NOT split"):
        assert kw in text, f"naming_and_repository_strategy.md に {kw!r} 無し"


def test_backend_abstraction_doc_exists():
    p = ROOT / "docs" / "backend_abstraction.md"
    assert p.exists()
    text = p.read_text(encoding="utf-8")
    for kw in ("InstrumentBackend", "Protocol", "spike", "pyvisa",
               "v1.1", "naming_and_repository_strategy"):
        assert kw in text, f"backend_abstraction.md に {kw!r} 無し"


# =========================================================
# backend Protocol spike
# =========================================================


def test_instrument_backend_protocol_importable():
    from visa_mcp.backends import InstrumentBackend
    assert hasattr(InstrumentBackend, "__call__") or True
    # Protocol の代表 method が定義されている
    assert "list_resources" in InstrumentBackend.__dict__ or \
           any("list_resources" in c.__dict__
                for c in InstrumentBackend.__mro__)


def test_existing_visa_managers_are_duck_compatible_with_backend():
    """既存 VisaManager / MockVisaManager は明示継承していないが
    duck-typed compatible である (将来 adapter 化のための存在証明)"""
    from lab_executor.testing.mock_instruments import MockVisaManager
    m = MockVisaManager()
    # 必要な async method が存在する
    assert hasattr(m, "list_resources")
    assert hasattr(m, "query")
    assert hasattr(m, "write")


# =========================================================
# stability.py 整合 (43 stable + 7 experimental = 50)
# =========================================================


def test_stability_counts_after_v11():
    assert stability.stable_count() == 43
    assert stability.experimental_count() == 7
    assert stability.total_documented_count() == 50


def test_new_bundle_tools_registered_as_experimental():
    """v1.1 で追加した bundle 検証 tools が experimental に登録"""
    exp_names = set(stability.experimental_tool_names())
    assert "validate_experiment_bundle" in exp_names
    assert "inspect_experiment_bundle" in exp_names


def test_no_new_stable_tools_in_v11():
    """v1.0 から stable は増えていない (v1.1 = direction-setting release)"""
    assert stability.stable_count() == 43


def test_v1_stability_policy_lists_v11_tools():
    text = (ROOT / "docs" / "v1_stability_policy.md").read_text(encoding="utf-8")
    assert "validate_experiment_bundle" in text
    assert "inspect_experiment_bundle" in text


# =========================================================
# validate_experiment_bundle / inspect_experiment_bundle
# =========================================================


YAML_PSU = """
metadata: { manufacturer: T, model: PSU, category: power_supply }
commands:
  measure_voltage:
    scpi: "MEAS:VOLT?"
    type: query
"""


def _setup_with_bundle(tmp_path):
    """Job を作って bundle を export し、その path を返す"""
    d = InstrumentDefinition(**yaml.safe_load(YAML_PSU))
    sessions = {"psu001": InstrumentSession(
        resource_name="psu001", idn_response="<x>",
        idn_parsed={}, definition=d,
    )}

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
    store.create_job("job_b", "alice", "psu001", "<r>", {})
    store.transition_status("job_b", JobStatus.RUNNING)
    sid = store.record_step_started("job_b", 0, "query")
    store.record_step_completed(
        sid, status="ok",
        result={"command": "measure_voltage", "value": 5.0, "unit": "V"},
    )
    store.transition_status("job_b", JobStatus.COMPLETED,
                            last_step_summary="done",
                            result={"success": True})
    return mgr, store


@pytest.mark.asyncio
async def test_validate_bundle_success(tmp_path, monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    monkeypatch.setattr(
        "lab_executor.tools.export.DEFAULT_EXPORT_DIR",
        tmp_path / "exports",
    )
    mgr, store = _setup_with_bundle(tmp_path)
    try:
        from fastmcp import FastMCP
        from lab_executor.tools.export import register_tools
        mcp = FastMCP("t")
        register_tools(mcp, mgr)
        export_tool = await mcp.get_tool("export_experiment_bundle")
        out = await export_tool.run({"job_id": "job_b"})
        bundle_path = (out.structured_content or {})["data"]["path"]

        validate_tool = await mcp.get_tool("validate_experiment_bundle")
        res = await validate_tool.run({"path": bundle_path})
        data = (res.structured_content or {}).get("data") or {}
        assert (res.structured_content or {}).get("status") == "ok"
        assert data["bundle_valid"] is True
        assert data["bundle_version"] == "1.0"
        assert data["checksum_errors"] == []
        assert data["missing_files"] == []
    finally:
        store.close()


@pytest.mark.asyncio
async def test_validate_bundle_missing_manifest(tmp_path, monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    mgr, store = _setup_with_bundle(tmp_path)
    try:
        # manifest 抜きの zip を自前で作る
        broken = tmp_path / "broken.zip"
        with zipfile.ZipFile(broken, "w") as zf:
            zf.writestr("plan.json", "{}")
        from fastmcp import FastMCP
        from lab_executor.tools.export import register_tools
        mcp = FastMCP("t")
        register_tools(mcp, mgr)
        tool = await mcp.get_tool("validate_experiment_bundle")
        res = await tool.run({"path": str(broken)})
        out = res.structured_content or {}
        assert out.get("status") == "error"
        assert any(
            (e.get("details") or {}).get("sub_class") == "missing_manifest"
            for e in (out.get("errors") or [])
        )
    finally:
        store.close()


@pytest.mark.asyncio
async def test_validate_bundle_checksum_mismatch(tmp_path, monkeypatch):
    """重要: 中身 1 byte 改ざんで sha256 mismatch を検出"""
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    monkeypatch.setattr(
        "lab_executor.tools.export.DEFAULT_EXPORT_DIR",
        tmp_path / "exports",
    )
    mgr, store = _setup_with_bundle(tmp_path)
    try:
        from fastmcp import FastMCP
        from lab_executor.tools.export import register_tools
        mcp = FastMCP("t")
        register_tools(mcp, mgr)
        export_tool = await mcp.get_tool("export_experiment_bundle")
        out = await export_tool.run({"job_id": "job_b"})
        original_path = Path((out.structured_content or {})["data"]["path"])

        # plan.json を改ざんした新 zip を作る (manifest はそのまま)
        tampered = tmp_path / "tampered.zip"
        with zipfile.ZipFile(original_path, "r") as src, \
             zipfile.ZipFile(tampered, "w") as dst:
            for n in src.namelist():
                data = src.read(n)
                if n == "job_record.json":
                    data = data + b"\n# tampered"
                dst.writestr(n, data)

        validate_tool = await mcp.get_tool("validate_experiment_bundle")
        res = await validate_tool.run({"path": str(tampered)})
        out2 = res.structured_content or {}
        assert out2.get("status") == "error"
        errs = out2.get("errors") or []
        assert any(
            (e.get("details") or {}).get("sub_class") == "checksum_mismatch"
            for e in errs
        )
        # checksum_errors に tampered ファイル (job_record.json) が入っている
        data = out2.get("data") or {}
        assert any(c["file"] == "job_record.json"
                   for c in data.get("checksum_errors", []))
    finally:
        store.close()


@pytest.mark.asyncio
async def test_inspect_bundle_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    monkeypatch.setattr(
        "lab_executor.tools.export.DEFAULT_EXPORT_DIR",
        tmp_path / "exports",
    )
    mgr, store = _setup_with_bundle(tmp_path)
    try:
        from fastmcp import FastMCP
        from lab_executor.tools.export import register_tools
        mcp = FastMCP("t")
        register_tools(mcp, mgr)
        export_tool = await mcp.get_tool("export_experiment_bundle")
        out = await export_tool.run({"job_id": "job_b"})
        bundle_path = (out.structured_content or {})["data"]["path"]

        inspect_tool = await mcp.get_tool("inspect_experiment_bundle")
        res = await inspect_tool.run({"path": bundle_path})
        data = (res.structured_content or {}).get("data") or {}
        assert data["manifest"]["bundle_version"] == "1.0"
        assert data["manifest"]["job_id"] == "job_b"
        # plan.json は recipe Job では生成されないため optional
        assert "job_summary" in data
        assert "result_row_count" in data
        assert data["has_audit"] is False
        assert data["has_monitor_data"] is False
        assert data["warnings"] == []
    finally:
        store.close()


@pytest.mark.asyncio
async def test_validate_bundle_not_found(tmp_path, monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    mgr, store = _setup_with_bundle(tmp_path)
    try:
        from fastmcp import FastMCP
        from lab_executor.tools.export import register_tools
        mcp = FastMCP("t")
        register_tools(mcp, mgr)
        tool = await mcp.get_tool("validate_experiment_bundle")
        res = await tool.run({"path": str(tmp_path / "no.zip")})
        out = res.structured_content or {}
        assert out.get("status") == "error"
        assert any(e["error_class"] == "not_found"
                   for e in (out.get("errors") or []))
    finally:
        store.close()


@pytest.mark.asyncio
async def test_validate_bundle_unsupported_version_warning(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    mgr, store = _setup_with_bundle(tmp_path)
    try:
        # 必須 files を入れつつ bundle_version=9.9 にした zip
        p = tmp_path / "future.zip"
        files = {
            "manifest.json": json.dumps({
                "bundle_version": "9.9",
                "visa_mcp_version": "9.9.9",
                "job_id": "j",
                "checksums": {},
            }),
            "plan.json": "{}",
            "job_record.json": "{}",
            "timeline.jsonl": "",
            "results.jsonl": "",
            "results.csv": "",
        }
        with zipfile.ZipFile(p, "w") as zf:
            for k, v in files.items():
                zf.writestr(k, v)
        from fastmcp import FastMCP
        from lab_executor.tools.export import register_tools
        mcp = FastMCP("t")
        register_tools(mcp, mgr)
        tool = await mcp.get_tool("validate_experiment_bundle")
        res = await tool.run({"path": str(p)})
        out = res.structured_content or {}
        # warning は出るが checksums 空 + 必須揃いで bundle_valid True
        data = out.get("data") or {}
        assert data["bundle_valid"] is True
        assert any(
            w["warning_class"] == "version_mismatch"
            for w in data.get("warnings", [])
        )
    finally:
        store.close()


# =========================================================
# repo format guard for v1.1 docs
# =========================================================


REPO_TEXT_TARGETS_V11 = [
    "docs/naming_and_repository_strategy.md",
    "docs/backend_abstraction.md",
    "src/lab_executor/backends/base.py",
    "src/lab_executor/backends/__init__.py",
    "tests/test_v11.py",
]


@pytest.mark.parametrize("rel", REPO_TEXT_TARGETS_V11)
def test_v11_repo_files_lf_only(rel):
    p = ROOT / rel
    assert p.exists(), f"missing: {p}"
    text = p.read_text(encoding="utf-8")
    assert "\r" not in text


@pytest.mark.parametrize("rel", REPO_TEXT_TARGETS_V11)
def test_v11_repo_files_multiline(rel):
    p = ROOT / rel
    text = p.read_text(encoding="utf-8")
    assert text.count("\n") + 1 >= 5
