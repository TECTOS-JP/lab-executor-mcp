"""v1.1.1: external review response (P0/P1/P2)

- P0: repo file format (LF only, multi-line)
- P1-2: bundle docs に plan.json optional 明記
- P1-3: inspect_experiment_bundle に compatibility (can_be_replayed=false)
- P1-4: bundle inspection の zip 安全性 docs (no extract)
- P1-5: backend_abstraction docs に Open questions
- P1-6: naming strategy の default + exception 表現
- P2-7: v1_stability_policy に InstrumentBackend stable plugin API ではない明記
"""
from __future__ import annotations
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
# Version
# =========================================================


REPO_TEXT_TARGETS_V111 = [
    "docs/naming_and_repository_strategy.md",
    "docs/backend_abstraction.md",
    "docs/bundle_export.md",
    "docs/v1_stability_policy.md",
    "src/lab_executor/backends/base.py",
    "src/lab_executor/backends/__init__.py",
    "src/lab_executor/stability.py",
    "src/lab_executor/tools/export.py",
    "tests/test_v11.py",
    "tests/test_v111_review.py",
]


@pytest.mark.parametrize("rel", REPO_TEXT_TARGETS_V111)
def test_v111_repo_files_lf_only(rel):
    p = ROOT / rel
    assert p.exists(), f"missing: {p}"
    text = p.read_text(encoding="utf-8")
    assert "\r" not in text


@pytest.mark.parametrize("rel", REPO_TEXT_TARGETS_V111)
def test_v111_repo_files_multiline(rel):
    p = ROOT / rel
    text = p.read_text(encoding="utf-8")
    assert text.count("\n") + 1 >= 5


# =========================================================
# P1-2: bundle docs に plan.json optional 明記
# =========================================================


def test_bundle_export_docs_explains_plan_json_optional():
    text = (ROOT / "docs" / "bundle_export.md").read_text(encoding="utf-8")
    assert "plan.json" in text
    # "optional" / "DSL Job" のキーワードで plan.json の任意性を説明
    assert "optional" in text.lower() or "DSL Job" in text


# =========================================================
# P1-4: bundle docs に zip 安全性
# =========================================================


def test_bundle_export_docs_explains_zip_safety():
    text = (ROOT / "docs" / "bundle_export.md").read_text(encoding="utf-8")
    for kw in ("ファイルシステムへの展開は行わない", "zip slip",
               "zip bomb"):
        assert kw in text, f"bundle_export.md に {kw!r} 無し"


# =========================================================
# P1-5: backend_abstraction docs に Open questions
# =========================================================


def test_backend_docs_has_open_questions():
    text = (ROOT / "docs" / "backend_abstraction.md").read_text(encoding="utf-8")
    assert "Open questions" in text
    for kw in ("stateful session", "timeout", "binary transfer",
               "backend capability"):
        assert kw in text, f"backend_abstraction.md Open questions に "\
            f"{kw!r} 無し"


# =========================================================
# P1-6: naming strategy の default + exception
# =========================================================


def test_naming_strategy_uses_default_decision_phrasing():
    text = (ROOT / "docs"
            / "naming_and_repository_strategy.md").read_text(encoding="utf-8")
    assert "Default decision" in text
    assert "Exception" in text


# =========================================================
# P2-7: v1_stability_policy に InstrumentBackend 注記
# =========================================================


def test_v1_stability_policy_notes_backend_not_stable_plugin_api():
    text = (ROOT / "docs"
            / "v1_stability_policy.md").read_text(encoding="utf-8")
    assert "InstrumentBackend" in text
    assert "stable plugin API ではない" in text \
        or "not a stable backend plugin API" in text


# =========================================================
# P1-3: inspect_experiment_bundle に compatibility
# =========================================================


YAML_PSU = """
metadata: { manufacturer: T, model: PSU, category: power_supply }
commands:
  measure_voltage:
    scpi: "MEAS:VOLT?"
    type: query
"""


def _setup(tmp_path):
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
async def test_inspect_bundle_returns_compatibility(tmp_path, monkeypatch):
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")
    monkeypatch.setattr(
        "lab_executor.tools.export.DEFAULT_EXPORT_DIR",
        tmp_path / "exports",
    )
    mgr, store = _setup(tmp_path)
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
        compat = data.get("compatibility")
        assert compat is not None, "compatibility field missing"
        assert compat["bundle_version_supported"] is True
        assert compat["can_be_replayed"] is False
        assert "v1.1" in compat.get("reason", "")
    finally:
        store.close()
