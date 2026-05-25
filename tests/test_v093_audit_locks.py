"""v0.9.3: audit / locks (Operational integrity) テスト"""
from __future__ import annotations
import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from lab_executor.audit import AuditStore, summarize_for_audit
from lab_executor.job import JobManager, JobStore
from lab_executor.job.state_machine import JobStatus
from lab_executor.models.instrument_def import InstrumentDefinition
from visa_mcp.session_manager import InstrumentSession
from lab_executor.system_config import SystemConfig, InstrumentBinding


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


# =========================================================
# migration / schema
# =========================================================


def test_audit_and_locks_tables_created_by_migration(tmp_path):
    store = JobStore(db_path=tmp_path / "x.sqlite")
    try:
        conn = store._connect()
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
        assert ver >= 3
        # audit table
        cols = [r[1] for r in conn.execute("PRAGMA table_info(audit)").fetchall()]
        for c in ("audit_id", "timestamp", "event_type", "severity", "owner",
                  "job_id", "resource", "status", "error_class",
                  "request_summary_json", "response_summary_json"):
            assert c in cols, f"audit.{c} 無し"
        # locks table
        lcols = [r[1] for r in conn.execute("PRAGMA table_info(locks)").fetchall()]
        for c in ("resource", "owner", "job_id", "acquired_at",
                  "lease_until", "lock_reason"):
            assert c in lcols, f"locks.{c} 無し"
    finally:
        store.close()


# =========================================================
# AuditStore.record_event + query
# =========================================================


def test_audit_records_event(tmp_path):
    store = JobStore(db_path=tmp_path / "a.sqlite")
    try:
        au = AuditStore(store)
        aid = au.record_event(
            "tool_called", severity="info",
            owner="agent_a", tool_name="start_experiment_job",
            job_id="job_1", resource="psu001",
            request={"foo": "bar"},
        )
        assert aid.startswith("aud_")
        evs, cursor = au.query()
        assert len(evs) == 1
        assert evs[0]["event_type"] == "tool_called"
        assert evs[0]["owner"] == "agent_a"
        assert cursor is None
    finally:
        store.close()


def test_audit_query_filters(tmp_path):
    store = JobStore(db_path=tmp_path / "f.sqlite")
    try:
        au = AuditStore(store)
        for i in range(5):
            au.record_event(
                "tool_called", owner="a" if i % 2 == 0 else "b",
                job_id=f"job_{i}", resource="psu001",
            )
        au.record_event("safety_blocked", owner="a", severity="warning",
                         resource="psu001")
        # event_type filter
        evs, _ = au.query(event_type="safety_blocked")
        assert len(evs) == 1
        # owner filter
        evs, _ = au.query(owner="b")
        assert all(e["owner"] == "b" for e in evs)
        # severity
        evs, _ = au.query(severity="warning")
        assert all(e["severity"] == "warning" for e in evs)
        # resource
        evs, _ = au.query(resource="psu001")
        assert len(evs) == 6
    finally:
        store.close()


def test_audit_query_pagination_cursor(tmp_path):
    """**重要**: cursor は {timestamp, audit_id} 複合"""
    store = JobStore(db_path=tmp_path / "p.sqlite")
    try:
        au = AuditStore(store)
        for i in range(10):
            au.record_event("tool_called", job_id=f"j{i}")
        page1, cur = au.query(limit=3)
        assert len(page1) == 3
        assert cur is not None
        assert "timestamp" in cur and "audit_id" in cur
        # next page
        page2, cur2 = au.query(limit=3, cursor=cur)
        assert len(page2) == 3
        # 異なる audit_id
        ids1 = {e["audit_id"] for e in page1}
        ids2 = {e["audit_id"] for e in page2}
        assert not (ids1 & ids2)
    finally:
        store.close()


def test_audit_include_details_default_false(tmp_path):
    store = JobStore(db_path=tmp_path / "d.sqlite")
    try:
        au = AuditStore(store)
        au.record_event("tool_called", request={"voltage": 5.0},
                         response={"ok": True})
        evs, _ = au.query()
        assert "request_summary" not in evs[0]
        evs, _ = au.query(include_details=True)
        assert evs[0].get("request_summary") == {"voltage": 5.0}
        assert evs[0].get("response_summary") == {"ok": True}
    finally:
        store.close()


# =========================================================
# Redaction
# =========================================================


def test_summarize_redacts_sensitive_keys():
    data = {
        "voltage": 5.0,
        "api_key": "secret123",
        "Authorization": "Bearer xxx",
        "nested": {"password": "p", "ok": True},
    }
    out = summarize_for_audit(data)
    assert out["voltage"] == 5.0
    assert out["api_key"] == "[REDACTED]"
    assert out["Authorization"] == "[REDACTED]"
    assert out["nested"]["password"] == "[REDACTED]"
    assert out["nested"]["ok"] is True


def test_summarize_truncates_long_strings():
    long = "x" * 1000
    out = summarize_for_audit({"data": long})
    assert isinstance(out["data"], dict)
    assert out["data"]["_truncated"] is True
    assert out["data"]["len"] == 1000


def test_summarize_truncates_long_lists():
    out = summarize_for_audit({"rows": list(range(20))})
    assert isinstance(out["rows"], dict)
    assert out["rows"]["_truncated_list"] is True
    assert out["rows"]["len"] == 20
    assert len(out["rows"]["head"]) == 5


def test_audit_redacts_sensitive_fields_in_db(tmp_path):
    """request/response の sensitive key が DB に redact されて保存される"""
    store = JobStore(db_path=tmp_path / "r.sqlite")
    try:
        au = AuditStore(store)
        au.record_event(
            "tool_called",
            request={"voltage": 5.0, "token": "secret_xyz"},
        )
        # DB から raw を読む
        row = store._connect().execute(
            "SELECT request_summary_json FROM audit"
        ).fetchone()
        text = row["request_summary_json"]
        assert "secret_xyz" not in text
        assert "REDACTED" in text
    finally:
        store.close()


# =========================================================
# Locks
# =========================================================


def test_lock_acquire_release(tmp_path):
    store = JobStore(db_path=tmp_path / "l.sqlite")
    try:
        au = AuditStore(store)
        r = au.acquire_lock("psu001", owner="agent_a", job_id="j1")
        assert r["acquired"] is True
        # 再取得 → blocked
        r2 = au.acquire_lock("psu001", owner="agent_b", job_id="j2")
        assert r2["acquired"] is False
        assert r2["blocked_by"]["owner"] == "agent_a"
        assert r2["blocked_by"]["job_id"] == "j1"
        # 解放
        assert au.release_lock("psu001", owner="agent_a") is True
        # 再取得可能
        r3 = au.acquire_lock("psu001", owner="agent_b", job_id="j2")
        assert r3["acquired"] is True
    finally:
        store.close()


def test_lock_only_owner_can_release(tmp_path):
    store = JobStore(db_path=tmp_path / "l2.sqlite")
    try:
        au = AuditStore(store)
        au.acquire_lock("psu001", owner="agent_a")
        # 別 owner では解放できない
        assert au.release_lock("psu001", owner="agent_b") is False
        # 本人なら可
        assert au.release_lock("psu001", owner="agent_a") is True
    finally:
        store.close()


def test_list_locks_filters_and_stale(tmp_path):
    store = JobStore(db_path=tmp_path / "ll.sqlite")
    try:
        au = AuditStore(store)
        au.acquire_lock("r1", owner="a", lease_seconds=3600)
        au.acquire_lock("r2", owner="b", lease_seconds=3600)
        # 手動で r2 を過去 lease に上書き
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(
            timespec="seconds",
        )
        store._connect().execute(
            "UPDATE locks SET lease_until=? WHERE resource='r2'", (past,),
        )
        all_locks = au.list_locks()
        assert len(all_locks) == 2
        stale_only = [l for l in all_locks if l["stale"]]
        assert len(stale_only) == 1 and stale_only[0]["resource"] == "r2"
        # include_stale=False
        active = au.list_locks(include_stale=False)
        assert len(active) == 1
        # owner filter
        evs = au.list_locks(owner="a")
        assert len(evs) == 1 and evs[0]["owner"] == "a"
    finally:
        store.close()


def test_release_stale_locks(tmp_path):
    store = JobStore(db_path=tmp_path / "s.sqlite")
    try:
        au = AuditStore(store)
        au.acquire_lock("r1", owner="a", lease_seconds=3600)
        au.acquire_lock("r2", owner="b", lease_seconds=3600)
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(
            timespec="seconds",
        )
        store._connect().execute(
            "UPDATE locks SET lease_until=? WHERE resource='r2'", (past,),
        )
        n = au.release_stale_locks()
        assert n == 1
        # r2 が削除されている
        remaining = au.list_locks()
        assert len(remaining) == 1
        assert remaining[0]["resource"] == "r1"
    finally:
        store.close()


def test_stale_lock_overwritten_on_acquire(tmp_path):
    """stale lock がある resource は新 owner で取得し直せる (上書き)"""
    store = JobStore(db_path=tmp_path / "s2.sqlite")
    try:
        au = AuditStore(store)
        au.acquire_lock("r1", owner="a", lease_seconds=3600)
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(
            timespec="seconds",
        )
        store._connect().execute(
            "UPDATE locks SET lease_until=? WHERE resource='r1'", (past,),
        )
        r = au.acquire_lock("r1", owner="b", lease_seconds=60)
        assert r["acquired"] is True
        assert r["lock"]["owner"] == "b"
    finally:
        store.close()


# =========================================================
# JobManager 統合 (server_started / job_started / cancel)
# =========================================================


def test_jobmanager_records_server_started(tmp_path):
    store, mgr = _setup(tmp_path)
    try:
        au = mgr.audit
        assert au is not None
        evs, _ = au.query(event_type="server_started")
        assert len(evs) >= 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_jobmanager_records_job_started_on_dsl(tmp_path, monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    store, mgr = _setup(tmp_path)
    try:
        plan = {
            "dsl_version": "0.8",
            "bindings": {"psu": "psu001"},
            "steps": [
                {"type": "command", "instrument": "$psu",
                 "command": "set_voltage", "args": {"voltage": 1.0}},
            ],
        }
        await mgr.start_experiment_job(plan_dict=plan, owner="alice")
        evs, _ = mgr.audit.query(event_type="job_started")
        assert len(evs) >= 1
        assert any(e["owner"] == "alice" for e in evs)
    finally:
        store.close()


# =========================================================
# MCP tools (query_audit / list_locks)
# =========================================================


@pytest.mark.asyncio
async def test_query_audit_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    store, mgr = _setup(tmp_path)
    try:
        mgr.audit.record_event("tool_called", owner="agent_x",
                                 job_id="j1")
        from fastmcp import FastMCP
        from lab_executor.tools.audit import register_tools
        mcp = FastMCP("t")
        register_tools(mcp, mgr)
        tool = await mcp.get_tool("query_audit")
        res = await tool.run({"owner": "agent_x"})
        data = (res.structured_content or {}).get("data") or {}
        events = data["events"]
        assert all(e["owner"] == "agent_x" for e in events)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_list_locks_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    store, mgr = _setup(tmp_path)
    try:
        mgr.audit.acquire_lock("psu001", owner="a", job_id="j1")
        from fastmcp import FastMCP
        from lab_executor.tools.audit import register_tools
        mcp = FastMCP("t")
        register_tools(mcp, mgr)
        tool = await mcp.get_tool("list_locks")
        res = await tool.run({})
        data = (res.structured_content or {}).get("data") or {}
        assert data["count"] >= 1
        assert data["locks"][0]["owner"] == "a"
    finally:
        store.close()
