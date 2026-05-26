"""v2.1.0: lab-executor MCP server composition root.

`lab-executor-mcp` runtime を **backend-independent** な MCP server
として起動するための合成 layer。v2.0 では cli.py 内 placeholder
だった `lab-executor serve` を、v2.1 で MockBackend 経由で実際に
起動可能にする。

主な責務:
- `InstrumentBackend` を外部から受け取る (default: `MockBackend`)
- `JobManager` / `SessionManager`-like を構成する
- `tools/*` の `register_tools` を順に呼んで MCP tool を expose する
- MCP tool 数 (Stable 43 + Experimental 7 = 50) を v2.0 から不変に

実機 backend が必要な場合は `visa-mcp` を install し `visa-mcp serve`
を使う。`lab-executor serve --backend mock` は PyVISA 非依存で
benchmark / dry-run / validation を行うための入口。
"""
from __future__ import annotations
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from lab_executor.backends import InstrumentBackend


def _make_session_manager_for_backend(backend: "InstrumentBackend"):
    """v2.1: backend に対する SessionManager 互換 facade を作る。

    `lab-executor-mcp` 内には `SessionManager` 実体は無い (visa-mcp に
    残った)。MockBackend 経由の動作では minimal facade で十分。
    """
    class _SessionFacade:
        """SessionManager のうち、tools/* が要求する最小 surface"""

        def __init__(self, backend):
            self._backend = backend
            self._sessions: dict[str, Any] = {}

        def get_session(self, name: str):
            return self._sessions.get(name)

        def register_session(self, name: str, session: Any) -> None:
            self._sessions[name] = session

        def list_sessions(self) -> list[str]:
            return list(self._sessions)

    return _SessionFacade(backend)


def _make_job_manager(backend: "InstrumentBackend",
                       session_facade: Any):
    """v2.1: JobManager を MockBackend と組み合わせて生成。

    JobManager は `visa: VisaManager` を要求する設計だが、v2.1 の
    backend layer はすべて `InstrumentBackend` Protocol 経由で
    `backend` を渡せばよい (TYPE_CHECKING の効果)。
    """
    # 遅延 import (PyVISA 不要)
    from lab_executor.job import JobManager, JobStore
    store = JobStore(":memory:")
    # v2.2.0: `backend=` keyword 推奨経路で渡す (`visa=` は
    # DeprecationWarning 経路、v3 で削除候補)。
    job_mgr = JobManager(
        backend=backend,
        session_mgr=session_facade,
        store=store,
    )
    return job_mgr


def create_server(
    backend: "InstrumentBackend | None" = None,
    *,
    name: str = "lab-executor",
    enable_experimental: bool = True,
) -> "FastMCP":
    """v2.1.0: lab-executor MCP server を生成する公開 API。

    引数:
      backend: `InstrumentBackend` 実装 (default: `MockBackend`)。
        実機 backend が必要なら `visa-mcp` の `PyVisaBackend` を
        外部から inject すること。
      name: MCP server 名
      enable_experimental: Experimental tool (7 件) を expose する
        かどうか (default: True、v1.0 から維持)

    返り値: `FastMCP` instance (server.run() などで起動可能)

    Raises:
      ImportError: fastmcp が install されていない場合
    """
    from fastmcp import FastMCP

    if backend is None:
        from lab_executor.backends import MockBackend
        backend = MockBackend()

    mcp = FastMCP(name=name)
    session_facade = _make_session_manager_for_backend(backend)
    job_mgr = _make_job_manager(backend, session_facade)

    # tools/* を順に register。各 register_tools は v1.0 凍結の MCP
    # tool 名 / 引数 / response を expose する。
    from lab_executor.tools import (
        audit as t_audit,
        commands as t_commands,
        dsl as t_dsl,
        export as t_export,
        groups as t_groups,
        info as t_info,
        jobs as t_jobs,
        monitor as t_monitor,
        observation as t_observation,
        pdf_extractor as t_pdf_extractor,
        recipes as t_recipes,
        waits as t_waits,
    )

    t_audit.register_tools(mcp, job_mgr)
    t_commands.register_tools(mcp, session_facade)
    t_dsl.register_tools(mcp, session_facade, job_mgr)
    t_export.register_tools(mcp, job_mgr)
    t_groups.register_tools(mcp, job_mgr)
    t_info.register_tools(mcp, session_facade, visa=backend,
                            job_mgr=job_mgr)
    t_jobs.register_tools(mcp, job_mgr)
    t_monitor.register_tools(mcp, job_mgr)
    t_observation.register_tools(mcp, job_mgr)
    t_pdf_extractor.register_tools(mcp)
    t_recipes.register_tools(mcp, session_facade)
    t_waits.register_tools(mcp, job_mgr)

    return mcp


def diagnose_tool_surface(server: "FastMCP") -> dict[str, Any]:
    """v2.1.1: server に登録された tool 数と `stability` の declaration
    との差分を返す診断 helper。

    Returns:
        {
            "registered_count": int,
            "declared_stable": int,    # 43
            "declared_experimental": int,  # 7
            "declared_total": int,     # 50
            "missing_from_registry": list[str],  # declared but not registered
            "extra_in_registry": list[str],      # registered but not declared
        }
    """
    from lab_executor import stability
    registered = set(list_registered_tools(server))
    declared_stable = [
        t for ts in stability.STABLE_TOOLS.values() for t in ts
    ]
    declared_exp = [
        t for ts in stability.EXPERIMENTAL_TOOLS.values() for t in ts
    ]
    declared = set(declared_stable) | set(declared_exp)
    return {
        "registered_count": len(registered),
        "declared_stable": len(declared_stable),
        "declared_experimental": len(declared_exp),
        "declared_total": len(declared),
        "missing_from_registry": sorted(declared - registered),
        "extra_in_registry": sorted(registered - declared),
    }


def list_registered_tools(server: "FastMCP") -> list[str]:
    """v2.1: server に登録された MCP tool 名を列挙 (test 用)"""
    import asyncio
    # FastMCP 2.x: list_tools() is async, returns list of Tool objects
    try:
        tools = asyncio.run(server.list_tools())
        return [t.name if hasattr(t, "name") else str(t) for t in tools]
    except Exception:
        pass
    # Fallback: 内部 registry
    for attr in ("_tools", "tools", "_tool_registry"):
        registry = getattr(server, attr, None)
        if registry:
            if isinstance(registry, dict):
                return list(registry.keys())
            try:
                return [
                    t.name if hasattr(t, "name") else str(t)
                    for t in registry
                ]
            except TypeError:
                pass
    return []
