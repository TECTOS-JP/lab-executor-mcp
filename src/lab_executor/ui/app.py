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
from pydantic import BaseModel

from lab_executor import __version__ as PKG_VERSION
from lab_executor.job.store import default_store_path
from lab_executor.ui import UI_VERSION
from lab_executor.ui.edit_store import EditDirStore, EditStoreError
from lab_executor.ui.readonly_store import ReadOnlyJobStore, UiStoreError
from lab_executor.ui.views import (
    dryrun_view,
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


def create_app(
    db_path: Path | str | None = None,
    edit_dir: Path | str | None = None,
    control_client=None,
) -> FastAPI:
    """読み取り専用モニタ UI の FastAPI アプリを生成する。

    db_path 省略時は ``default_store_path()`` を使う。DB 不在時でもアプリ生成は
    成功し、リクエスト時に UiStoreError → error.html / JSON 503 へ変換する。

    edit_dir を指定すると M3 のレシピ編集ルートを登録する (state DB への接続は
    引き続き read-only。書き込みは edit_dir 配下の YAML と git のみ)。
    ``edit_dir=None`` なら編集ルートは一切登録せず M1/M2 と同一の read-only UI。

    control_client (M4): コントロールプレーンへの ``ControlClient``。省略時は
    ``default_control_path()`` を見る既定 client を使う。コントロールプレーン用
    ルート ``/api/control/*`` は **常時登録** されるが、control.json が無い /
    到達不能なら ``available: false`` / 503 を返す (UI はボタンを隠す)。テストで
    monkeypatch できるよう引数で差し替え可能。
    """
    resolved = Path(db_path) if db_path is not None else default_store_path()
    store = ReadOnlyJobStore(resolved)

    edit_store: EditDirStore | None = None
    if edit_dir is not None:
        edit_store = EditDirStore(edit_dir)

    if control_client is None:
        from lab_executor.ui.control_client import ControlClient
        control_client = ControlClient()

    app = FastAPI(title="lab-executor monitor", version=PKG_VERSION)
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    templates.env.globals["ui_version"] = UI_VERSION
    templates.env.globals["pkg_version"] = PKG_VERSION
    templates.env.globals["edit_enabled"] = edit_store is not None

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
            pause=store.get_active_pause(job_id),
        )
        sweep = _build_sweep_chart(store, job_id)
        control_available = (
            await asyncio.to_thread(control_client.available)
        ) is not None
        return templates.TemplateResponse(
            request,
            "job_detail.html",
            {
                "detail": detail,
                "sweep_json": json.dumps(sweep) if sweep else None,
                "control_available": control_available,
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
            pause=store.get_active_pause(job_id),
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

    # ============================================================
    # M4: コントロールプレーン proxy ルート (常時登録)
    # ============================================================
    _register_control_routes(app, control_client)

    # ============================================================
    # M3: レシピ編集ルート (edit_store があるときだけ登録)
    # ============================================================
    if edit_store is not None:
        _register_edit_routes(app, templates, edit_store, control_client)

    return app


# ================================================================
# M4 コントロールプレーン proxy ルートの登録
# ================================================================


def _register_control_routes(app, control_client) -> None:
    """UI → コントロールプレーンへの proxy ルート群を app に登録する。

    実行系操作は UI プロセスでは行わず、必ず serve 内コントロールプレーンへ
    転送する。POST は M3 と同じく ``Content-Type: application/json`` のみ。
    コントロール無効 (control.json 無し / 到達不能) は 503。
    """

    def _require_json(request: Request) -> JSONResponse | None:
        ctype = request.headers.get("content-type", "")
        if not ctype.startswith("application/json"):
            return JSONResponse(
                status_code=415,
                content={
                    "error": "unsupported_media_type",
                    "detail": "Content-Type: application/json のみ受け付けます",
                },
            )
        return None

    @app.get("/api/control/status")
    async def api_control_status():
        info = await asyncio.to_thread(control_client.available)
        if info is None:
            return {"available": False, "backend_id": None}
        return {
            "available": True,
            "backend_id": info.get("backend_id"),
            "pid": info.get("pid"),
            "started_at": info.get("started_at"),
        }

    @app.post("/api/control/jobs/{job_id}/cancel")
    async def api_control_cancel(request: Request, job_id: str):
        bad = _require_json(request)
        if bad is not None:
            return bad
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse(
                status_code=422,
                content={"error": "invalid_json", "detail": "JSON body が不正です"},
            )
        if not isinstance(body, dict):
            return JSONResponse(
                status_code=422,
                content={"error": "invalid_json", "detail": "object を送ってください"},
            )
        cancel_mode = body.get("cancel_mode", "after_current_step")
        timeout_s = body.get("timeout_s", 30.0)
        status, resp = await asyncio.to_thread(
            control_client.cancel, job_id, cancel_mode, timeout_s,
        )
        return JSONResponse(status_code=status, content=resp)

    @app.post("/api/control/jobs/{job_id}/pause-response")
    async def api_control_pause_response(request: Request, job_id: str):
        """v2.30.0 (SP-4): pause への「続行 / 中止」応答をコントロールプレーンへ転送。"""
        bad = _require_json(request)
        if bad is not None:
            return bad
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse(
                status_code=422,
                content={"error": "invalid_json", "detail": "JSON body が不正です"},
            )
        if not isinstance(body, dict):
            return JSONResponse(
                status_code=422,
                content={"error": "invalid_json", "detail": "object を送ってください"},
            )
        action = body.get("action", "")
        if action not in ("continue", "abort"):
            return JSONResponse(
                status_code=422,
                content={
                    "error": "invalid_action",
                    "detail": "action は continue / abort のいずれかです",
                },
            )
        status, resp = await asyncio.to_thread(
            control_client.pause_response, job_id, action,
        )
        return JSONResponse(status_code=status, content=resp)

    @app.post("/api/control/start-recipe")
    async def api_control_start_recipe(request: Request):
        bad = _require_json(request)
        if bad is not None:
            return bad
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse(
                status_code=422,
                content={"error": "invalid_json", "detail": "JSON body が不正です"},
            )
        if not isinstance(body, dict):
            return JSONResponse(
                status_code=422,
                content={"error": "invalid_json", "detail": "object を送ってください"},
            )
        resource_name = body.get("resource_name") or ""
        recipe_name = body.get("recipe_name") or ""
        if not resource_name or not recipe_name:
            return JSONResponse(
                status_code=422,
                content={
                    "error": "invalid_request",
                    "detail": "resource_name と recipe_name は必須です",
                },
            )
        parameters = body.get("parameters") or {}
        status, resp = await asyncio.to_thread(
            control_client.start_recipe,
            resource_name,
            recipe_name,
            parameters if isinstance(parameters, dict) else {},
        )
        return JSONResponse(status_code=status, content=resp)


# ================================================================
# M3 edit ルートの登録
# ================================================================


class _ValidateBody(BaseModel):
    rel: str
    content: str


class _DryrunBody(BaseModel):
    rel: str
    content: str
    recipe: str
    parameters: dict = {}
    # v2.28.0 (SP-2): 実行時解決 / compute のテスト値注入 ("steps.x": 1.0 等)
    test_values: dict = {}


class _SaveBody(BaseModel):
    rel: str
    content: str
    message: str = ""


def _build_dryrun(
    content: str, recipe_name: str, parameters: dict,
    test_values: dict | None = None,
) -> dict:
    """編集中の YAML 文字列 + レシピ名 + パラメータ → 展開 Step 列 dict。

    パース / 式評価エラーは ValueError を送出する (ルート側で 422 に変換)。
    validate / dry-run のロジックは再実装せず、既存 API を import して使う。

    v2.28.0 (SP-2): definition を渡して ${...} の範囲宣言検証を有効化し、
    test_values があれば deferred / compute の解決値も表示する。
    """
    import yaml

    from lab_executor.models.instrument_def import InstrumentDefinition
    from lab_executor.recipe_executor import recipe_to_plan
    from lab_executor.utils.expression import ExpressionError

    try:
        raw = yaml.safe_load(content) or {}
        defn = InstrumentDefinition(**raw)
    except Exception as exc:  # noqa: BLE001 - パースエラーを 422 理由として返す
        raise ValueError(f"定義のパースに失敗しました: {exc}")

    recipe = defn.recipes.get(recipe_name)
    if recipe is None:
        raise ValueError(f"レシピが見つかりません: {recipe_name}")

    # パラメータ default を型に合わせて補完し、変数辞書を作る。
    variables: dict = {}
    for pdef in recipe.parameters:
        if pdef.name in parameters and parameters[pdef.name] is not None:
            variables[pdef.name] = parameters[pdef.name]
        elif pdef.default is not None:
            variables[pdef.name] = pdef.default
        # 欠落 (default 無しかつ未入力) は入れない → 式評価時に ExpressionError

    try:
        plan = recipe_to_plan(recipe, variables, definition=defn)
    except (ExpressionError, KeyError, ValueError, TypeError) as exc:
        raise ValueError(f"dry-run 展開に失敗しました: {exc}")

    return dryrun_view(plan, test_values=test_values or {})


def _register_edit_routes(
    app, templates, edit_store: EditDirStore, control_client=None
) -> None:
    """レシピ編集ルート群を app に登録する。"""

    @app.exception_handler(EditStoreError)
    async def _edit_store_error_handler(request: Request, exc: EditStoreError):
        # パス不正 / 検証失敗 / git 失敗はすべて 422 で理由を JSON 返却。
        return JSONResponse(
            status_code=422,
            content={"error": "edit_store_error", "detail": str(exc)},
        )

    def _require_json(request: Request) -> None:
        ctype = request.headers.get("content-type", "")
        if not ctype.startswith("application/json"):
            raise EditStoreError(
                "POST は Content-Type: application/json のみ受け付けます"
            )

    # ---- HTML ----
    @app.get("/recipes", response_class=HTMLResponse)
    async def recipes_list(request: Request):
        files = edit_store.list_files()
        control_available = False
        if control_client is not None:
            control_available = (
                await asyncio.to_thread(control_client.available)
            ) is not None
        return templates.TemplateResponse(
            request,
            "recipes.html",
            {
                "files": files,
                "edit_dir": str(edit_store.root),
                "control_available": control_available,
            },
        )

    @app.get("/recipes/edit/{rel:path}", response_class=HTMLResponse)
    async def recipe_edit(request: Request, rel: str):
        # read_file が存在チェック + パストラバーサル防御を兼ねる。
        content = edit_store.read_file(rel)
        return templates.TemplateResponse(
            request,
            "recipe_edit.html",
            {"rel": rel, "content": content},
        )

    # ---- JSON API ----
    @app.get("/api/edit/files")
    async def api_edit_files():
        return {"files": edit_store.list_files()}

    @app.get("/api/edit/file/{rel:path}")
    async def api_edit_file(rel: str):
        return {"rel": rel, "content": edit_store.read_file(rel)}

    @app.post("/api/edit/validate")
    async def api_edit_validate(request: Request, body: _ValidateBody):
        _require_json(request)
        # rel のパストラバーサル防御 (存在は必須ではないが範囲は検査)。
        edit_store._resolve(body.rel)
        return {"validation": edit_store.validate(body.content)}

    @app.post("/api/edit/dryrun")
    async def api_edit_dryrun(request: Request, body: _DryrunBody):
        _require_json(request)
        edit_store._resolve(body.rel)
        try:
            result = await asyncio.to_thread(
                _build_dryrun, body.content, body.recipe, body.parameters,
                body.test_values,
            )
        except ValueError as exc:
            return JSONResponse(
                status_code=422,
                content={"error": "dryrun_failed", "detail": str(exc)},
            )
        return {"dryrun": result}

    @app.post("/api/edit/save")
    async def api_edit_save(request: Request, body: _SaveBody):
        _require_json(request)
        result = await asyncio.to_thread(
            edit_store.save_file, body.rel, body.content, body.message
        )
        return result
