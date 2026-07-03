"""FastAPI アプリ生成 (Web UI M1)。

``create_app(db_path)`` で読み取り専用モニタの FastAPI アプリを構築する。
fastapi / jinja2 は optional-dependencies ``[ui]`` に置くため、このモジュールは
``lab-executor ui`` サブコマンドから遅延 import される (未インストール時は cli が案内)。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from lab_executor import __version__ as PKG_VERSION
from lab_executor.job.store import default_store_path
from lab_executor.ui import UI_VERSION
from lab_executor.ui.readonly_store import ReadOnlyJobStore, UiStoreError
from lab_executor.ui.views import job_detail_view, job_row_view

_HERE = Path(__file__).resolve().parent
_TEMPLATES_DIR = _HERE / "templates"
_STATIC_DIR = _HERE / "static"

# health の稼働判定しきい値 (秒)。これ未満なら「稼働中」、以上なら「アイドル/停止」。
_HEALTH_ACTIVE_THRESHOLD_S = 30.0


def _build_job_rows(store: ReadOnlyJobStore, limit: int = 100) -> list[dict]:
    """一覧用 view model のリスト。各 job の最新 event_type を 1 件だけ引く。"""
    jobs = store.list_jobs(limit=limit)
    rows = []
    for job in jobs:
        events = store.list_events(job["job_id"], limit=1)
        last_event_type = events[0]["event_type"] if events else None
        rows.append(job_row_view(job, last_event_type))
    return rows


def _health_view(store: ReadOnlyJobStore) -> dict:
    h = store.health()
    secs = h.get("seconds_since_last_write")
    if secs is not None and secs < _HEALTH_ACTIVE_THRESHOLD_S:
        h["status_label"] = "● 稼働中"
        h["status_class"] = "running"
    else:
        h["status_label"] = "○ アイドルまたは停止"
        h["status_class"] = "idle"
    return h


def create_app(db_path: Path | str | None = None) -> FastAPI:
    """読み取り専用モニタ UI の FastAPI アプリを生成する。

    db_path 省略時は ``default_store_path()`` を使う。DB 不在時でもアプリ生成は
    成功し、リクエスト時に UiStoreError → error.html / JSON 503 へ変換する。
    """
    resolved = Path(db_path) if db_path is not None else default_store_path()
    store = ReadOnlyJobStore(resolved)

    app = FastAPI(title="lab-executor monitor", version=PKG_VERSION)
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    templates.env.globals["ui_version"] = UI_VERSION
    templates.env.globals["pkg_version"] = PKG_VERSION

    if _STATIC_DIR.exists():
        app.mount(
            "/static", StaticFiles(directory=str(_STATIC_DIR)), name="static"
        )

    # ----- exception handler: UiStoreError -----
    @app.exception_handler(UiStoreError)
    async def _ui_store_error_handler(request: Request, exc: UiStoreError):
        # api ルートは JSON 503、それ以外は error.html。
        if request.url.path.startswith("/api/"):
            return JSONResponse(
                status_code=503,
                content={"error": "state_db_unavailable", "detail": str(exc)},
            )
        return templates.TemplateResponse(
            request,
            "error.html",
            {"message": str(exc)},
            status_code=503,
        )

    # ----- HTML routes -----
    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        rows = _build_job_rows(store)
        health = _health_view(store)
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "jobs": rows,
                "health": health,
                "host_external": getattr(app.state, "host_external", False),
            },
        )

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    async def job_detail(request: Request, job_id: str):
        job = store.get_job(job_id)
        if job is None:
            return templates.TemplateResponse(
                request,
                "error.html",
                {"message": f"ジョブが見つかりません: {job_id}"},
                status_code=404,
            )
        detail = job_detail_view(
            job,
            store.list_steps(job_id),
            store.list_events(job_id),
            store.list_target_runs(job_id),
        )
        return templates.TemplateResponse(
            request,
            "job_detail.html",
            {"detail": detail},
        )

    # ----- htmx partials -----
    @app.get("/partials/jobs-table", response_class=HTMLResponse)
    async def partial_jobs_table(request: Request):
        rows = _build_job_rows(store)
        health = _health_view(store)
        return templates.TemplateResponse(
            request,
            "_jobs_table.html",
            {"jobs": rows, "health": health},
        )

    @app.get(
        "/partials/jobs/{job_id}/timeline", response_class=HTMLResponse
    )
    async def partial_timeline(request: Request, job_id: str):
        job = store.get_job(job_id)
        if job is None:
            return templates.TemplateResponse(
                request,
                "error.html",
                {"message": f"ジョブが見つかりません: {job_id}"},
                status_code=404,
            )
        detail = job_detail_view(
            job,
            store.list_steps(job_id),
            store.list_events(job_id),
            store.list_target_runs(job_id),
        )
        return templates.TemplateResponse(
            request,
            "_timeline.html",
            {"detail": detail},
        )

    # ----- JSON API -----
    @app.get("/api/jobs")
    async def api_jobs():
        return {"jobs": _build_job_rows(store)}

    @app.get("/api/jobs/{job_id}")
    async def api_job_detail(job_id: str):
        job = store.get_job(job_id)
        if job is None:
            return JSONResponse(
                status_code=404,
                content={"error": "job_not_found", "job_id": job_id},
            )
        detail = job_detail_view(
            job,
            store.list_steps(job_id),
            store.list_events(job_id),
            store.list_target_runs(job_id),
        )
        return detail

    @app.get("/api/health")
    async def api_health():
        h = _health_view(store)
        h["ui_version"] = UI_VERSION
        h["pkg_version"] = PKG_VERSION
        return h

    return app
