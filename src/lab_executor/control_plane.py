"""Web UI M4: serve プロセス内 HTTP コントロールプレーン。

serve プロセス内に **127.0.0.1 固定** の小さな HTTP サーバを立て、UI プロセス
から実行系操作 (ジョブキャンセル / レシピ実行) を受け付ける。すべての操作は
MCP ツール (``tools/jobs.py``) と **同一の** ``JobManager`` メソッド経由で行い、
safety / audit を共通化する。

絶対制約:
- ``create_control_app`` の import 時点では starlette を必要としない
  (starlette / JSONResponse は **関数内で遅延 import**、必須依存に足さない)。
- token は ``X-Control-Token`` ヘッダ必須。比較は ``secrets.compare_digest``。
- ``override_safety`` は body に来ても **常に False 固定** で無視する。
- audit は MCP ツールと同じ ``AuditStore(job_mgr.store)`` へ、
  ``tool_name="control.cancel_job"`` / ``"control.start_recipe_job"``、
  ``client_id="control-plane"`` で記録する。

discovery: serve がコントロール有効で起動すると ``control.json`` を
``default_store_path().parent`` に書く。UI プロセスはこれを読んで token を得る
(ブラウザには token を渡さない)。
"""
from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from starlette.applications import Starlette

    from lab_executor.job import JobManager


CONTROL_FILE_NAME = "control.json"


# ============================================================
# control.json discovery helpers
# ============================================================


def default_control_path() -> Path:
    """``default_store_path().parent / "control.json"``。

    ``VISA_MCP_STATE_DB`` が設定されていればその隣、無ければ
    ``~/.visa-mcp/control.json``。
    """
    from lab_executor.job.store import default_store_path

    return default_store_path().parent / CONTROL_FILE_NAME


def write_control_file(
    path: Path | str,
    *,
    url: str,
    token: str,
    pid: int,
    backend_id: str,
    started_at: str | None = None,
) -> None:
    """control.json を atomically に書き込む (親ディレクトリは自動生成)。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "url": url,
        "token": token,
        "pid": pid,
        "backend_id": backend_id,
        "started_at": started_at or _now_iso(),
    }
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    os.replace(tmp, p)


def read_control_file(path: Path | str) -> dict | None:
    """control.json を読む。無い / 壊れている / 必須キー欠落は None。"""
    p = Path(path)
    try:
        raw = p.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    if not data.get("url") or not data.get("token"):
        return None
    return data


def remove_control_file(path: Path | str) -> None:
    """control.json を削除 (無ければ何もしない)。"""
    try:
        Path(path).unlink()
    except FileNotFoundError:
        return
    except OSError:
        return


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# The only MCP tools the control plane may invoke. Every one of these reads:
# the instrument ones answer from loaded definitions, get_state runs the
# definition's declared state_query, and the two plan tools are documented to
# perform no instrument I/O at all — they render and check a plan against the
# definitions. Nothing here can change an instrument, and there is no endpoint
# that accepts a tool name from the caller, so this list is the whole of what a
# browser can reach.
_READ_ONLY_TOOLS: frozenset[str] = frozenset({
    "describe_instrument",
    "get_instrument_info",
    "list_commands",
    "list_safety_constraints",
    "get_state",
    "validate_experiment_plan",
    "dry_run_plan",
    # Job observation. These read what a run has already recorded, so they are
    # the one part of the surface that is safe to poll while an experiment is in
    # progress — including one an agent started.
    "list_jobs",
    "get_job_status",
    "get_job_live_view",
    "get_job_summary",
    "get_experiment_timeline",
    # Results. Recorded measurements, read back after the fact.
    "get_monitor_data",
    "get_experiment_results",
})


def _section_failed(section: Any) -> bool:
    """Did a tool report that it could not answer for this instrument?

    Two response shapes are in use across the tool surface, confirmed against a
    live pyvisa server: ``describe_instrument`` returns
    ``{"status": "error", "errors": [...]}`` while ``get_instrument_info`` and
    ``list_safety_constraints`` return ``{"success": False, "error": ...}``.
    Both have to be recognised, or an unknown instrument looks like a hit.
    """
    if not isinstance(section, dict):
        return False
    if section.get("status") == "error":
        return True
    return section.get("success") is False


# ============================================================
# Starlette app
# ============================================================


def create_control_app(
    job_mgr: "JobManager",
    *,
    token: str,
    backend_id: str = "mock",
    pid: int | None = None,
    started_at: str | None = None,
    mcp: Any = None,
) -> "Starlette":
    """コントロールプレーンの Starlette app を生成する。

    starlette は **この関数内で import** する (module top-level には置かない)。
    未インストール時は ImportError が伝播する (呼び出し側で案内する)。
    """
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from lab_executor.audit import AuditStore
    from lab_executor.job import CancelMode
    from lab_executor.job.state_machine import is_terminal

    _pid = pid if pid is not None else os.getpid()
    _started_at = started_at or _now_iso()
    audit = AuditStore(job_mgr.store)

    def _token_ok(request: Request) -> bool:
        given = request.headers.get("x-control-token", "")
        return bool(given) and secrets.compare_digest(given, token)

    def _unauthorized() -> JSONResponse:
        return JSONResponse(
            {"error": "unauthorized", "detail": "invalid or missing token"},
            status_code=401,
        )

    async def _read_json(request: Request) -> dict:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - 非 JSON body は空 dict 扱い
            return {}
        return body if isinstance(body, dict) else {}

    async def health(request: Request) -> JSONResponse:
        if not _token_ok(request):
            return _unauthorized()
        return JSONResponse({
            "ok": True,
            "pid": _pid,
            "backend_id": backend_id,
            "started_at": _started_at,
        })

    async def cancel_job(request: Request) -> JSONResponse:
        if not _token_ok(request):
            return _unauthorized()
        job_id = request.path_params["job_id"]
        body = await _read_json(request)
        mode_raw = body.get("cancel_mode", "after_current_step")
        timeout_s = body.get("timeout_s", 30.0)
        try:
            mode = CancelMode(mode_raw)
        except ValueError:
            return JSONResponse(
                {
                    "error": "invalid_cancel_mode",
                    "detail": f"不正な cancel_mode: {mode_raw}",
                    "valid": [m.value for m in CancelMode],
                },
                status_code=422,
            )
        try:
            timeout_val = float(timeout_s)
        except (TypeError, ValueError):
            timeout_val = 30.0

        owner = body.get("owner") or "web-ui"
        audit.record_event(
            "job_cancel_requested",
            severity="warning",
            owner=owner,
            client_id="control-plane",
            tool_name="control.cancel_job",
            job_id=job_id,
            request={"cancel_mode": mode.value, "timeout_s": timeout_val},
        )
        try:
            rec = await job_mgr.cancel(job_id, mode, timeout_s=timeout_val)
        except Exception as exc:  # noqa: BLE001
            audit.record_event(
                "job_cancel_failed",
                severity="error",
                status="error",
                owner=owner,
                client_id="control-plane",
                tool_name="control.cancel_job",
                job_id=job_id,
                error_class="internal",
                message=str(exc),
            )
            return JSONResponse(
                {"error": "cancel_failed", "detail": str(exc)},
                status_code=500,
            )
        data = {
            "job_id": rec.job_id,
            "status": rec.status.value,
            "is_terminal": is_terminal(rec.status),
            "cancel_mode": mode.value,
            "last_step_summary": rec.last_step_summary,
        }
        audit.record_event(
            "job_cancelled",
            severity="warning",
            status=rec.status.value,
            owner=owner,
            client_id="control-plane",
            tool_name="control.cancel_job",
            job_id=rec.job_id,
            resource=rec.resource_name,
            response=data,
        )
        return JSONResponse(data)

    async def pause_response(request: Request) -> JSONResponse:
        """v2.30.0 (SP-4): pause への「続行 / 中止」応答。

        token 認証・audit 記録は cancel と同じ流儀。応答は
        ``JobManager.respond_pause`` (job_pauses の resolution 更新) 経由。
        """
        if not _token_ok(request):
            return _unauthorized()
        job_id = request.path_params["job_id"]
        body = await _read_json(request)
        action = body.get("action", "")
        if action not in ("continue", "abort"):
            return JSONResponse(
                {
                    "error": "invalid_action",
                    "detail": f"不正な action: {action!r}",
                    "valid": ["continue", "abort"],
                },
                status_code=422,
            )
        responder = body.get("responder") or "web-ui"
        audit.record_event(
            "job_pause_response_requested",
            severity="warning" if action == "abort" else "info",
            owner=responder,
            client_id="control-plane",
            tool_name="control.pause_response",
            job_id=job_id,
            request={"action": action},
        )
        try:
            result = job_mgr.respond_pause(
                job_id, action, responder=responder,
            )
        except Exception as exc:  # noqa: BLE001
            audit.record_event(
                "job_pause_response_failed",
                severity="error",
                status="error",
                owner=responder,
                client_id="control-plane",
                tool_name="control.pause_response",
                job_id=job_id,
                error_class="internal",
                message=str(exc),
            )
            return JSONResponse(
                {"error": "pause_response_failed", "detail": str(exc)},
                status_code=500,
            )
        if not result.get("ok"):
            err = result.get("error", "pause_response_failed")
            status_code = 404 if err in ("not_found", "no_active_pause") else 422
            audit.record_event(
                "job_pause_response_failed",
                severity="warning",
                status="error",
                owner=responder,
                client_id="control-plane",
                tool_name="control.pause_response",
                job_id=job_id,
                error_class=err,
                message=result.get("detail"),
            )
            return JSONResponse(
                {"error": err, "detail": result.get("detail")},
                status_code=status_code,
            )
        audit.record_event(
            "job_pause_responded",
            severity="warning" if action == "abort" else "info",
            owner=responder,
            client_id="control-plane",
            tool_name="control.pause_response",
            job_id=job_id,
            response=result,
        )
        return JSONResponse(result)

    async def start_recipe(request: Request) -> JSONResponse:
        if not _token_ok(request):
            return _unauthorized()
        body = await _read_json(request)
        resource_name = body.get("resource_name") or ""
        recipe_name = body.get("recipe_name") or ""
        if not resource_name or not recipe_name:
            return JSONResponse(
                {
                    "error": "invalid_request",
                    "detail": "resource_name と recipe_name は必須です",
                },
                status_code=422,
            )
        parameters = body.get("parameters") or {}
        if not isinstance(parameters, dict):
            return JSONResponse(
                {
                    "error": "invalid_request",
                    "detail": "parameters は object である必要があります",
                },
                status_code=422,
            )
        owner = body.get("owner") or "web-ui"
        job_timeout_s = body.get("job_timeout_s")
        try:
            timeout_val = (
                float(job_timeout_s)
                if job_timeout_s not in (None, "")
                else None
            )
        except (TypeError, ValueError):
            timeout_val = None

        audit.record_event(
            "job_start_requested",
            severity="info",
            owner=owner,
            client_id="control-plane",
            tool_name="control.start_recipe_job",
            resource=resource_name,
            request={
                "resource_name": resource_name,
                "recipe_name": recipe_name,
                "parameters": parameters,
            },
        )
        try:
            # override_safety は **常に False 固定** (body に来ても無視)。
            rec = await job_mgr.start_recipe_job(
                resource_name,
                recipe_name,
                parameters,
                owner=owner,
                override_safety=False,
                override_reason="",
                job_timeout_s=timeout_val,
            )
        except Exception as exc:  # noqa: BLE001
            audit.record_event(
                "job_start_failed",
                severity="error",
                status="error",
                owner=owner,
                client_id="control-plane",
                tool_name="control.start_recipe_job",
                resource=resource_name,
                error_class="internal",
                message=str(exc),
            )
            return JSONResponse(
                {"error": "start_failed", "detail": str(exc)},
                status_code=500,
            )
        data = {
            "job_id": rec.job_id,
            "status": rec.status.value,
            "resource_name": rec.resource_name,
            "recipe": rec.recipe,
            "created_at": rec.created_at,
        }
        audit.record_event(
            "job_started",
            severity="info",
            status=rec.status.value,
            owner=owner,
            client_id="control-plane",
            tool_name="control.start_recipe_job",
            job_id=rec.job_id,
            resource=rec.resource_name,
            response=data,
        )
        # レシピ定義なし等の即時 failed も 200 で返す (UI 側で status を見て表示)。
        return JSONResponse(data)

    # ---------- instruments (read-only) ----------
    # A Web UI needs to show which devices exist, what they can be told to do,
    # and what state they are in. None of that is in the state DB, and a UI that
    # opened its own connections would fight the serve process for exclusive
    # transports (GPIB, serial, BLE) while an agent is mid-experiment. So the
    # serve process — the one owner of the hardware — answers these instead.
    #
    # Only the frozen BEF surface and an explicit allowlist of read-only MCP
    # tools are reachable here. There is deliberately no passthrough that could
    # invoke an arbitrary tool or send an arbitrary command.

    async def _read_only_tool(name: str, arguments: dict) -> Any:
        """Invoke one allowlisted read-only MCP tool, or return None if absent.

        Going through the same tool the agent calls is the point: the human and
        the agent then read one description of the instrument, not two that can
        disagree. A server composed without that tool simply omits the section.
        """
        if mcp is None or name not in _READ_ONLY_TOOLS:
            return None
        try:
            result = await mcp.call_tool(name, arguments)
        except Exception:  # noqa: BLE001 - an absent or failing tool is not fatal
            return None
        data = getattr(result, "structured_content", None)
        if isinstance(data, dict) and set(data) == {"result"}:
            return data["result"]
        return data if data is not None else getattr(result, "data", None)

    async def instruments(request: Request) -> JSONResponse:
        if not _token_ok(request):
            return _unauthorized()
        backend = getattr(job_mgr, "backend", None)
        if backend is None:
            return JSONResponse(
                {"error": "no_backend", "detail": "this server has no backend"},
                status_code=503,
            )
        try:
            names = await backend.list_resources()
        except Exception as exc:  # noqa: BLE001 - report, never crash the plane
            return JSONResponse(
                {"error": "list_failed", "detail": str(exc)}, status_code=502
            )
        return JSONResponse({
            "backend_id": backend_id,
            "resources": [{"resource_name": n} for n in names],
        })

    async def instrument_detail(request: Request) -> JSONResponse:
        if not _token_ok(request):
            return _unauthorized()
        name = request.path_params["resource_name"]
        payload: dict[str, Any] = {"resource_name": name}
        for key, tool in (
            ("description", "describe_instrument"),
            ("info", "get_instrument_info"),
            # describe_instrument lists command names only, with no indication
            # of which ones operate the instrument. A console has to show that
            # distinction, so the detailed list is fetched too.
            ("commands", "list_commands"),
            ("safety_constraints", "list_safety_constraints"),
        ):
            value = await _read_only_tool(tool, {"resource_name": name})
            if value is not None:
                payload[key] = value
        sections = [v for k, v in payload.items() if k != "resource_name"]
        if not sections:
            return JSONResponse(
                {
                    "error": "not_available",
                    "detail": (
                        "this server exposes no instrument description tools"
                    ),
                },
                status_code=503,
            )
        # The tools answer an unknown resource with an error payload rather than
        # raising, so a path naming no instrument would otherwise come back 200.
        # Report 404 instead, while still handing over the tools' own reasons —
        # the same ones the agent is given.
        if all(_section_failed(s) for s in sections):
            payload["error"] = "unknown_instrument"
            return JSONResponse(payload, status_code=404)
        return JSONResponse(payload)

    async def instrument_state(request: Request) -> JSONResponse:
        """Read the device's current state.

        Unlike the two endpoints above, this one talks to the instrument: it
        runs the definition's ``state_query`` commands. It is therefore audited
        and meant for an explicit user action, not background polling.
        """
        if not _token_ok(request):
            return _unauthorized()
        name = request.path_params["resource_name"]
        audit.record_event(
            "instrument_state_requested",
            severity="info",
            owner=request.query_params.get("owner") or "web-ui",
            client_id="control-plane",
            tool_name="control.get_state",
            resource=name,
        )
        value = await _read_only_tool("get_state", {"resource_name": name})
        if value is None:
            return JSONResponse(
                {"error": "not_available", "detail": "get_state is unavailable"},
                status_code=503,
            )
        return JSONResponse({"resource_name": name, "state": value})

    # ---------- plans (read-only) ----------
    # Checking and rendering a plan performs no instrument I/O, which is what
    # makes it safe to expose here: an operator can see what a procedure would
    # do before anything is sent. Starting one is a different matter and stays
    # on the existing job routes.

    async def _plan_tool(request: Request, tool: str) -> JSONResponse:
        if not _token_ok(request):
            return _unauthorized()
        body = await _read_json(request)
        plan = body.get("plan")
        if not isinstance(plan, dict):
            return JSONResponse(
                {"error": "invalid_request", "detail": "plan は object が必要です"},
                status_code=422,
            )
        result = await _read_only_tool(tool, {"plan": plan})
        if result is None:
            return JSONResponse(
                {
                    "error": "not_available",
                    "detail": f"this server does not expose {tool}",
                },
                status_code=503,
            )
        return JSONResponse({"tool": tool, "result": result})

    async def validate_plan(request: Request) -> JSONResponse:
        return await _plan_tool(request, "validate_experiment_plan")

    async def dry_run_plan(request: Request) -> JSONResponse:
        return await _plan_tool(request, "dry_run_plan")

    async def start_plan(request: Request) -> JSONResponse:
        """Run a plan on the instruments. The one execution path for plans.

        Mirrors the recipe route's discipline: ``override_safety`` is forced
        False whatever the body says, the request and its outcome are audited,
        and the runtime's own validation stays in front of the hardware.
        """
        if not _token_ok(request):
            return _unauthorized()
        body = await _read_json(request)
        plan = body.get("plan")
        if not isinstance(plan, dict):
            return JSONResponse(
                {"error": "invalid_request", "detail": "plan は object が必要です"},
                status_code=422,
            )
        owner = body.get("owner") or "web-ui"
        audit.record_event(
            "plan_start_requested",
            severity="warning",
            owner=owner,
            client_id="control-plane",
            tool_name="control.start_experiment_job",
            request={"name": plan.get("name", ""), "steps": len(plan.get("steps", []))},
        )
        if mcp is None:
            return JSONResponse(
                {"error": "not_available", "detail": "this server cannot start plans"},
                status_code=503,
            )
        try:
            # override_safety is deliberately not forwarded from the body: a
            # browser must never be able to switch the safety layer off.
            result = await mcp.call_tool(
                "start_experiment_job",
                {"plan": plan, "owner": owner, "override_safety": False},
            )
        except Exception as exc:  # noqa: BLE001 - report, never crash the plane
            audit.record_event(
                "plan_start_failed",
                severity="error",
                status="error",
                owner=owner,
                client_id="control-plane",
                tool_name="control.start_experiment_job",
                error_class="internal",
                message=str(exc),
            )
            return JSONResponse(
                {"error": "start_failed", "detail": str(exc)}, status_code=500
            )
        data = getattr(result, "structured_content", None)
        if isinstance(data, dict) and set(data) == {"result"}:
            data = data["result"]
        if data is None:
            data = getattr(result, "data", None)
        audit.record_event(
            "plan_started",
            severity="info",
            owner=owner,
            client_id="control-plane",
            tool_name="control.start_experiment_job",
            response=data if isinstance(data, dict) else {},
        )
        return JSONResponse({"result": data})

    async def jobs(request: Request) -> JSONResponse:
        if not _token_ok(request):
            return _unauthorized()
        arguments: dict[str, Any] = {}
        status = request.query_params.get("status")
        if status:
            arguments["status"] = status
        value = await _read_only_tool("list_jobs", arguments)
        if value is None:
            return JSONResponse(
                {"error": "not_available", "detail": "list_jobs is unavailable"},
                status_code=503,
            )
        return JSONResponse({"result": value})

    async def job_monitor(request: Request) -> JSONResponse:
        """Measurements a monitor recorded for this job, oldest first."""
        if not _token_ok(request):
            return _unauthorized()
        job_id = request.path_params["job_id"]
        try:
            limit = int(request.query_params.get("limit", "1000"))
        except ValueError:
            limit = 1000
        value = await _read_only_tool(
            "get_monitor_data", {"monitor_id": job_id, "limit": limit}
        )
        if value is None:
            return JSONResponse(
                {"error": "not_available", "detail": "get_monitor_data is unavailable"},
                status_code=503,
            )
        return JSONResponse({"result": value})

    async def job_results(request: Request) -> JSONResponse:
        if not _token_ok(request):
            return _unauthorized()
        job_id = request.path_params["job_id"]
        value = await _read_only_tool("get_experiment_results", {"job_id": job_id})
        if value is None:
            return JSONResponse(
                {"error": "not_available", "detail": "results are unavailable"},
                status_code=503,
            )
        return JSONResponse({"result": value})

    async def job_detail(request: Request) -> JSONResponse:
        if not _token_ok(request):
            return _unauthorized()
        job_id = request.path_params["job_id"]
        payload: dict[str, Any] = {"job_id": job_id}
        for key, tool in (
            ("status", "get_job_status"),
            ("live_view", "get_job_live_view"),
        ):
            value = await _read_only_tool(tool, {"job_id": job_id})
            if value is not None:
                payload[key] = value
        if len(payload) == 1:
            return JSONResponse(
                {"error": "not_available", "detail": "job tools are unavailable"},
                status_code=503,
            )
        return JSONResponse(payload)

    routes = [
        Route("/control/health", health, methods=["GET"]),
        Route("/control/plans/validate", validate_plan, methods=["POST"]),
        Route("/control/plans/dry-run", dry_run_plan, methods=["POST"]),
        Route("/control/plans/start", start_plan, methods=["POST"]),
        Route("/control/jobs", jobs, methods=["GET"]),
        Route("/control/jobs/{job_id}/monitor", job_monitor, methods=["GET"]),
        Route("/control/jobs/{job_id}/results", job_results, methods=["GET"]),
        Route("/control/jobs/{job_id}", job_detail, methods=["GET"]),
        Route("/control/instruments", instruments, methods=["GET"]),
        Route(
            "/control/instruments/{resource_name:path}/state",
            instrument_state, methods=["GET"],
        ),
        Route(
            "/control/instruments/{resource_name:path}",
            instrument_detail, methods=["GET"],
        ),
        Route(
            "/control/jobs/{job_id}/cancel", cancel_job, methods=["POST"]
        ),
        Route(
            "/control/jobs/{job_id}/pause-response",
            pause_response, methods=["POST"],
        ),
        Route(
            "/control/jobs/start-recipe", start_recipe, methods=["POST"]
        ),
    ]
    return Starlette(routes=routes)


# ============================================================
# control plane runner (公開 API; v2.24.0)
# ============================================================


def resolve_control_port(cli_value: int | None) -> int | None:
    """v2.24.0: コントロールプレーンのポートを解決する (公開 API)。

    ``cli_value`` (CLI ``--control-port`` 相当) が優先。``None`` なら env
    ``LAB_EXECUTOR_CONTROL_PORT``。どちらも無ければ ``None`` (無効)。
    env が非整数なら警告を stderr に出して ``None`` を返す。

    v2.24.0 で ``cli.py`` の ``_resolve_control_port`` からロジックを移設し、
    visa-mcp serve など外部からも同一の解決規則を再利用できるようにした。
    """
    if cli_value is not None:
        return int(cli_value)
    raw = os.environ.get("LAB_EXECUTOR_CONTROL_PORT")
    if raw is None or raw.strip() == "":
        return None
    try:
        return int(raw)
    except ValueError:
        import sys

        print(
            f"WARNING: LAB_EXECUTOR_CONTROL_PORT={raw!r} は整数ではありません "
            "(コントロールプレーン無効)。",
            file=sys.stderr,
        )
        return None


async def run_mcp_with_control(
    mcp: Any,
    job_mgr: "JobManager",
    control_port: int,
    *,
    backend_id: str,
    control_path: Path | None = None,
) -> None:
    """v2.24.0: MCP (stdio) とコントロールプレーン (uvicorn) を並走する (公開 API)。

    ``cli.py`` の ``_serve_with_control`` のコアを移設したもの。挙動不変。

    - bind は 127.0.0.1 固定 (外部 bind オプションは作らない)。
    - token は起動毎に ``secrets.token_hex(32)`` で生成。
    - port=0 (OS 任せ) を許可し、実ポートを control.json に書く。
    - 終了時に control.json を削除 (finally + atexit の二重化)。

    引数:
      mcp: ``run_async(transport="stdio")`` を持つ MCP server (FastMCP)。
      job_mgr: コントロールプレーンが共有する ``JobManager``。
      control_port: bind ポート (0 = OS 任せ)。
      backend_id: control.json / health に載せる backend 識別子
        (例: lab-executor="mock" / visa-mcp="pyvisa")。
      control_path: control.json の書き込み先。``None`` (default) なら
        ``default_control_path()``。
    """
    import asyncio
    import atexit
    import secrets

    import uvicorn

    token = secrets.token_hex(32)
    pid = os.getpid()
    app = create_control_app(
        job_mgr, token=token, backend_id=backend_id, pid=pid, mcp=mcp,
    )
    config = uvicorn.Config(
        app, host="127.0.0.1", port=control_port, log_level="warning",
    )
    ctl = uvicorn.Server(config)

    ctl_path = control_path if control_path is not None else \
        default_control_path()

    # kill -9 で残った control.json のため atexit でも掃除する (二重化)。
    atexit.register(remove_control_file, str(ctl_path))

    async def _write_control_when_bound() -> None:
        # uvicorn がソケットを bind するまで待ち、実ポートを control.json に書く。
        while not getattr(ctl, "started", False):
            await asyncio.sleep(0.02)
        actual_port = control_port
        try:
            servers = getattr(ctl, "servers", None) or []
            if servers and servers[0].sockets:
                actual_port = servers[0].sockets[0].getsockname()[1]
        except Exception:  # noqa: BLE001 - 取得失敗時は指定 port を使う
            actual_port = control_port
        write_control_file(
            ctl_path,
            url=f"http://127.0.0.1:{actual_port}",
            token=token,
            pid=pid,
            backend_id=backend_id,
        )

    try:
        await asyncio.gather(
            mcp.run_async(transport="stdio"),
            ctl.serve(),
            _write_control_when_bound(),
        )
    finally:
        ctl.should_exit = True
        remove_control_file(str(ctl_path))
