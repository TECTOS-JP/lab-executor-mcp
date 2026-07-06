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

    routes = [
        Route("/control/health", health, methods=["GET"]),
        Route(
            "/control/jobs/{job_id}/cancel", cancel_job, methods=["POST"]
        ),
        Route(
            "/control/jobs/start-recipe", start_recipe, methods=["POST"]
        ),
    ]
    return Starlette(routes=routes)
