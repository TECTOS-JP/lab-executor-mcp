"""v0.9.3.1: external review response (P0/P1)

- P0: repo file format (LF only, multi-line)
- P1-6: AuditStore init 失敗時の visibility (audit_unavailable response)
- P1-2/3/4/5/7/8/9: docs/operational_integrity.md に追記
"""
from __future__ import annotations
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

ROOT = Path(__file__).parent.parent


# =========================================================
# P0: repo text format
# =========================================================


REPO_TEXT_TARGETS = [
    "src/lab_executor/audit.py",
    "src/lab_executor/tools/audit.py",
    "src/lab_executor/job/manager.py",
    "src/lab_executor/server.py",
    "tests/test_v093_audit_locks.py",
    "docs/operational_integrity.md",
    "docs/error_taxonomy.md",
    "README.md",
]


@pytest.mark.parametrize("rel", REPO_TEXT_TARGETS)
def test_v0931_repo_files_lf_only(rel):
    p = ROOT / rel
    assert p.exists(), f"missing: {p}"
    text = p.read_text(encoding="utf-8")
    assert "\r" not in text, f"CR found in {p}"


@pytest.mark.parametrize("rel", REPO_TEXT_TARGETS)
def test_v0931_repo_files_multiline(rel):
    p = ROOT / rel
    text = p.read_text(encoding="utf-8")
    assert text.count("\n") + 1 >= 5, f"{p} appears single-line"


# =========================================================
# P1-6: AuditStore unavailable visibility
# =========================================================


YAML_PSU = """
metadata: { manufacturer: T, model: PSU, category: power_supply }
commands:
  set_voltage:
    scpi: "VOLT {voltage}"
    type: write
    parameters:
      - { name: voltage, type: float, range: [0, 100] }
"""


def _setup(tmp_path):
    from lab_executor.job import JobManager, JobStore
    from lab_executor.models.instrument_def import InstrumentDefinition
    from lab_visa_mcp.session_manager import InstrumentSession
    from lab_executor.system_config import SystemConfig, InstrumentBinding

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
    return store, mgr


def test_audit_init_error_flag_default_false(tmp_path):
    """正常起動時 _audit_init_error は False、audit は使える"""
    store, mgr = _setup(tmp_path)
    try:
        assert mgr.audit is not None
        assert mgr._audit_init_error is False
    finally:
        store.close()


@pytest.mark.asyncio
async def test_query_audit_returns_audit_unavailable_when_disabled(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    store, mgr = _setup(tmp_path)
    try:
        # 模擬的に audit init 失敗状態にする
        mgr._audit = None
        mgr._audit_init_error = True
        from fastmcp import FastMCP
        from lab_executor.tools.audit import register_tools
        mcp = FastMCP("t")
        register_tools(mcp, mgr)
        tool = await mcp.get_tool("query_audit")
        res = await tool.run({})
        out = res.structured_content or {}
        assert out.get("status") == "error"
        errs = out.get("errors") or []
        assert any(
            (e.get("details") or {}).get("sub_class") == "audit_unavailable"
            for e in errs
        )
    finally:
        store.close()


@pytest.mark.asyncio
async def test_list_locks_returns_audit_unavailable_when_disabled(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    store, mgr = _setup(tmp_path)
    try:
        mgr._audit = None
        mgr._audit_init_error = True
        from fastmcp import FastMCP
        from lab_executor.tools.audit import register_tools
        mcp = FastMCP("t")
        register_tools(mcp, mgr)
        tool = await mcp.get_tool("list_locks")
        res = await tool.run({})
        out = res.structured_content or {}
        assert out.get("status") == "error"
        errs = out.get("errors") or []
        assert any(
            (e.get("details") or {}).get("sub_class") == "audit_unavailable"
            for e in errs
        )
    finally:
        store.close()


# =========================================================
# docs に追記すべきセクションが存在する
# =========================================================


def test_operational_integrity_docs_has_lock_source_of_truth():
    text = (ROOT / "docs" / "operational_integrity.md").read_text(encoding="utf-8")
    for kw in ("source of truth", "ResourceScheduler", "Stale lock",
               "audit_unavailable", "Audit retention", "blocked_by"):
        assert kw in text, f"docs/operational_integrity.md に {kw!r} 無し"


def test_operational_integrity_docs_has_v1_blocked_policy():
    text = (ROOT / "docs" / "operational_integrity.md").read_text(encoding="utf-8")
    # blocked + lock_conflict v1.0 方針
    assert "lock_conflict" in text
    assert "v1.0" in text
    assert "blocked" in text


def test_operational_integrity_docs_lists_audit_scope():
    text = (ROOT / "docs" / "operational_integrity.md").read_text(encoding="utf-8")
    # 監査対象 tool 範囲のセクション
    assert "監査対象 tool" in text or "audit 記録" in text
    for tool_name in ("start_experiment_job", "cancel_job", "resume_job"):
        assert tool_name in text
