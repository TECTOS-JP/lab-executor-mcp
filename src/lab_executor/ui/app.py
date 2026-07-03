"""FastAPI アプリ生成 (Web UI M1)。

``create_app(db_path)`` で読み取り専用モニタの FastAPI アプリを構築する。
fastapi / jinja2 は optional-dependencies ``[ui]`` に置くため、このモジュールは
``lab-executor ui`` サブコマンドから遅延 import される (未インストール時は cli が案内)。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from lab_executor import __version__ as PKG_VERSION
from lab_executor.job.store import default_store_path
from lab_executor.ui import UI_VERSION
from lab_executor.ui.readonly_store import ReadOnlyJobStore, UiStoreError
from lab_executor.ui.views import (
    is_terminal_status,
    job_detail_view,
    job_row_view,
    sweep_chart_view,
)

_HERE = Path(__file__).resolve().parent
_TEMPLATES_DIR = _HERE / "templates"
_STATIC_DIR = _HERE / "static"

# health の稼働判定しきい値 (秒)。これ未満なら「稼働中」、以上なら「アイドル/停止」。
_HEALTH_ACTIVE_THRESHOLD_S = 30.0

# SSE ループのポーリング間隔 (秒) と keep-alive ping 間隔 (秒)。
_SSE_POLL_INTERVAL_S = 1.5
_SSE_PING_INTERVAL_S = 15.0
# 終端ジョブ SSE の最終フレーム後に送る retry (ms)。無限再接続を避ける。
_SSE_TERMINAL_RETRY_MS = 3600000


def _sse_frame(event: str, data: str) -> str:
    """SSE フレームを組み立てる。

    ``data`` が複数行の場合、各行に ``data: `` プレフィックスを付ける
    (SSE 仕様: 改行を含む本文は行ごとに data フィールドを出す)。末尾は空行。
    """
    lines = [f"event: {event}"]
    for line in data.split("\n"):
        lines.append(f"data: {line}")
    return "\n".join(lines) + "\n\n"


def _build_job_rows(store: ReadOnlyJobStore, limit: int = 100) -> list[dict]:
    """一覧用 view model のリスト。

    v2.21.0 (M2): N+1 を解消。``list_jobs_with_last_event`` が jobs と
    各 job の最新 event_type を 1 クエリで取得する。
    """
    jobs = store.list_jobs_with_last_event(limit=limit)
    return [job_row_view(job, job.get("last_event_type")) for job in jobs]


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


def _build_sweep_chart(store: ReadOnlyJobStore, job_id: str) -> dict | None:
    """job の sweep 点列を uPlot 用 dict に変換する (sweep 無しは None)。

    observation の ``_extract_sweep_views`` を **再実装せず import** して使う。
    このヘルパは ``store.list_steps(job_id)`` しか呼ばないため ReadOnlyJobStore
    をそのまま渡せる (M1 の _row_to_record 再利用と同じ意図的再利用)。
    """
    from lab_executor.tools.observation import _extract_sweep_views

    points = _extract_sweep_views(store, job_id)
    return sweep_chart_view(points)


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
        sweep = _build_sweep_chart(store, job_id)
        return templates.TemplateResponse(
            request,
            "job_detail.html",
            {
                "detail": detail,
                "sweep_json": json.dumps(sweep) if sweep else None,
            },
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

    # ----- SSE ライブ更新 -----
    # SQLite 読み取りは asyncio.to_thread でイベントループを塞がない。
    # 変化検出は前回送信フラグメントとのハッシュ比較で行い、変化時のみ送る。

    def _render_jobs_table_fragment() -> str:
        rows = _build_job_rows(store)
        health = _health_view(store)
        tmpl = templates.get_template("_jobs_table.html")
        return tmpl.render(jobs=rows, health=health)

    def _render_timeline_fragment(job_id: str) -> tuple[str | None, bool]:
        """(_timeline.html レンダリング, is_terminal) を返す。job 不在は (None, True)。"""
        job = store.get_job(job_id)
        if job is None:
            return None, True
        detail = job_detail_view(
            job,
            store.list_steps(job_id),
            store.list_events(job_id),
            store.list_target_runs(job_id),
        )
        tmpl = templates.get_template("_timeline.html")
        return tmpl.render(detail=detail), bool(detail["is_terminal"])

    @app.get("/sse/dashboard")
    async def sse_dashboard(request: Request, max_cycles: int | None = None):
        """ダッシュボード SSE。

        ``max_cycles`` は **テスト・診断用** (ブラウザからは使わない)。
        指定サイクル数でループを打ち切りストリームを正常終了する。
        Starlette TestClient は無限ストリームのクローズをジェネレータに
        伝えられない (``is_disconnected`` が立たずデッドロックする) ため、
        テストはこれで有限化する。デフォルト None は従来どおり無限。
        """
        async def gen():
            last_hash: str | None = None
            since_ping = 0.0
            cycles = 0
            while True:
                if await request.is_disconnected():
                    break
                try:
                    fragment = await asyncio.to_thread(
                        _render_jobs_table_fragment
                    )
                except UiStoreError as exc:
                    yield _sse_frame("error", str(exc))
                    break
                digest = hashlib.sha256(fragment.encode("utf-8")).hexdigest()
                if digest != last_hash:
                    last_hash = digest
                    yield _sse_frame("jobs-table", fragment)
                    since_ping = 0.0
                cycles += 1
                if max_cycles is not None and cycles >= max_cycles:
                    break
                await asyncio.sleep(_SSE_POLL_INTERVAL_S)
                since_ping += _SSE_POLL_INTERVAL_S
                if since_ping >= _SSE_PING_INTERVAL_S:
                    yield ": ping\n\n"
                    since_ping = 0.0

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/sse/jobs/{job_id}")
    async def sse_job(
        request: Request, job_id: str, max_cycles: int | None = None
    ):
        """ジョブ timeline SSE。終端ジョブは最終フラグメント後に閉じる。

        ``max_cycles`` は **テスト・診断用** (ブラウザからは使わない)。
        非終端ジョブのストリームを有限化するためのループ上限。
        デフォルト None は従来どおり (終端まで) 無限。
        """
        async def gen():
            last_hash: str | None = None
            since_ping = 0.0
            cycles = 0
            while True:
                if await request.is_disconnected():
                    break
                try:
                    fragment, terminal = await asyncio.to_thread(
                        _render_timeline_fragment, job_id
                    )
                except UiStoreError as exc:
                    yield _sse_frame("error", str(exc))
                    break
                if fragment is None:
                    # ジョブ不在: 何も送らず終了 (retry 抑制)。
                    yield f"retry: {_SSE_TERMINAL_RETRY_MS}\n\n"
                    break
                digest = hashlib.sha256(fragment.encode("utf-8")).hexdigest()
                if digest != last_hash:
                    last_hash = digest
                    yield _sse_frame("timeline", fragment)
                    since_ping = 0.0
                if terminal:
                    # 終端: 最終フラグメント送信済み。長い retry を送って閉じる
                    # (終端後の無限再接続を避ける)。
                    yield f"retry: {_SSE_TERMINAL_RETRY_MS}\n\n"
                    break
                cycles += 1
                if max_cycles is not None and cycles >= max_cycles:
                    break
                await asyncio.sleep(_SSE_POLL_INTERVAL_S)
                since_ping += _SSE_POLL_INTERVAL_S
                if since_ping >= _SSE_PING_INTERVAL_S:
                    yield ": ping\n\n"
                    since_ping = 0.0

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
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

    @app.get("/api/jobs/{job_id}/sweep")
    async def api_job_sweep(job_id: str):
        """sweep グラフ用 JSON (uPlot が食える形)。sweep 無し / 不在は
        ``{"sweep": null}``。実行中ジョブが SSE timeline 受信時に再取得する。"""
        job = store.get_job(job_id)
        if job is None:
            return JSONResponse(
                status_code=404,
                content={"error": "job_not_found", "job_id": job_id},
            )
        return {"sweep": _build_sweep_chart(store, job_id)}

    @app.get("/api/health")
    async def api_health():
        h = _health_view(store)
        h["ui_version"] = UI_VERSION
        h["pkg_version"] = PKG_VERSION
        return h

    return app
