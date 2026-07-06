# Web UI M4 実装計画 (コントロールプレーン: ジョブキャンセル + レシピ実行)

作成: 2026-07-04 / 計画: Claude Fable 5 / 実装・テスト: Claude Opus / 最終確認: Claude Fable 5

前提: M1 (v2.20.0) / M2 (v2.21.0) / M3 (v2.22.0) コミット済み。
テスト 1825 passed / 28 skipped / 0 failed。

## M4 のゴール

UI からジョブのキャンセル（3モード）とレシピのジョブ投入ができる。
実行系操作は **serve プロセス内の HTTP コントロールプレーン**を必ず経由し、
MCP ツールと同じ JobManager メソッド・同じ safety/audit を通る。

## 制約の改訂（M4 で初めて変わるもの / 変わらないもの)

- **変わる**: serve 起動経路（cli の `_cmd_serve` と server.py）に**追加的**変更を
  加えてよい。ただし既定動作（コントロール無効時）は現行と完全同一であること
- **変わらない**: MCP ツール面 50 個（名前・引数・レスポンス）は不変。
  `create_server()` の公開シグネチャも不変。`tests/test_separation_boundary.py` と
  `diagnose_tool_surface` が green のまま
- **変わらない**: UI プロセスから state DB への接続は read-only。UI は実行系操作を
  自分では行わず、必ずコントロールプレーンへ**プロキシ**する
- **変わらない**: 必須依存に追加しない。コントロールプレーンは starlette + uvicorn を
  **遅延 import** し（fastmcp-slim は同梱しない）、無ければ
  「`pip install lab-executor-mcp[ui]` が必要」と案内してコントロール無効で継続 or
  明示指定時は exit 1

## アーキテクチャ

```
ブラウザ ──(同一オリジン)──> lab-executor ui (UIプロセス)
                                │  POST /api/control/... (プロキシ)
                                │  token は control.json から UI プロセスが読む
                                │  (ブラウザには渡さない)
                                ▼
                     serve プロセス内 control plane (127.0.0.1:PORT)
                                │  X-Control-Token 検証 (constant-time)
                                ▼
                     JobManager.cancel / start_recipe_job
                     (MCP ツールと同一経路 = safety/audit 共通)
```

### ディスカバリ: control.json

serve がコントロール有効で起動すると `default_store_path().parent / "control.json"`
（= `~/.visa-mcp/control.json`、`VISA_MCP_STATE_DB` 設定時はその隣）に書く:

```json
{"url": "http://127.0.0.1:8300", "token": "<32byte hex>", "pid": 12345,
 "started_at": "...", "backend_id": "mock"}
```

- token は `secrets.token_hex(32)` で起動毎に生成
- serve 終了時に削除（finally + atexit の二重化。ただし kill -9 は残る前提で、
  UI 側は使用前に必ず /control/health を叩いて生死確認する）
- 複数 serve は last-writer-wins（M4 の割り切り。UI は health が通った 1 個だけ使う）

## 新規/変更ファイル

```
src/lab_executor/control_plane.py   # 新規: Starlette app + token + control.json
src/lab_executor/server.py          # 変更: compose_server() 追加 (create_server は不変)
src/lab_executor/cli.py             # 変更: serve --control-port / ui 側プロキシは app.py
src/lab_executor/ui/control_client.py  # 新規: control.json 読み + HTTP 転送 (urllib)
src/lab_executor/ui/app.py          # 変更: /api/control/* プロキシ + 画面ボタン用 status
src/lab_executor/ui/templates/job_detail.html  # 変更: キャンセルボタン (3モード)
src/lab_executor/ui/templates/recipes.html     # 変更: レシピ実行フォーム
tests/test_control_plane_m4.py
tests/test_ui_m4.py
docs/web_ui.md                      # M4 追記
```

## control_plane.py 仕様

```python
def create_control_app(job_mgr, *, token: str) -> "Starlette"
```

ルート（すべて `X-Control-Token` ヘッダ必須。不一致は 401。
比較は `secrets.compare_digest`）:

| ルート | 内容 |
|---|---|
| `GET /control/health` | `{"ok": true, "pid", "backend_id", "started_at"}` |
| `POST /control/jobs/{job_id}/cancel` | body `{cancel_mode, timeout_s?}` → `job_mgr.cancel(job_id, CancelMode(mode), timeout_s=...)`。不正 mode は 422。返り値は job_id/status/is_terminal/last_step_summary |
| `POST /control/jobs/start-recipe` | body `{resource_name, recipe_name, parameters?, job_timeout_s?}` → `job_mgr.start_recipe_job(...)`。**`override_safety` は常に False 固定**（body に来ても無視） |

- audit: 各操作の前後に `AuditStore(job_mgr.store).record_event(...)` を記録。
  `tool_name="control.cancel_job"` / `"control.start_recipe_job"`、
  `client_id="control-plane"`、owner は body の `owner`（default "web-ui"）。
  AuditStore の引数形は `src/lab_executor/tools/audit.py` と `audit.py` 本体を
  読んで既存流儀に合わせること
- starlette は関数内 import。托底: `create_control_app` を import した時点では
  starlette 不要（モジュール top-level に starlette import を置かない）

```python
def write_control_file(path, *, url, token, pid, backend_id) -> None
def read_control_file(path) -> dict | None      # 無い/壊れは None
def remove_control_file(path) -> None
def default_control_path() -> Path              # default_store_path().parent/"control.json"
```

## server.py

`compose_server(backend, *, name, enable_experimental) -> tuple[FastMCP, JobManager]`
を追加（現行 create_server の本体を移し、create_server は
`compose_server(...)[0]` を返す薄いラッパに。公開シグネチャ・挙動は不変）。

## cli.py の serve 変更

- `serve` に `--control-port N`（default None）を追加。
  env `LAB_EXECUTOR_CONTROL_PORT` でも指定可（CLI 優先）。
  これは visa-mcp serve が lab_executor の runner を使わず独自に起動している場合に
  備えた将来互換（M4 では lab-executor serve のみ対応。visa-mcp 側の対応は別タスク）
- コントロール有効時の起動:

```python
async def _serve_with_control(server, job_mgr, port):
    token = secrets.token_hex(32)
    app = create_control_app(job_mgr, token=token)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    ctl = uvicorn.Server(config)
    write_control_file(default_control_path(), url=..., token=token, ...)
    try:
        await asyncio.gather(server.run_async(transport="stdio"), ctl.serve())
    finally:
        ctl.should_exit = True
        remove_control_file(default_control_path())
```

- bind は **127.0.0.1 固定**（外部 bind オプションを作らない）
- port=0（OS 任せ）を許可し、実ポートを control.json に書く
  （uvicorn の `ctl.servers[0].sockets` から取得。テストでも port 衝突を避けられる）
- コントロール無効（フラグ・env とも無し）なら従来どおり `server.run()`。
  1 行も挙動が変わらないこと

## UI 側 (control_client.py + app.py)

- `ControlClient(path=default_control_path())`:
  `available() -> dict | None`（control.json 読み → /control/health を token 付きで
  叩き、2xx なら info を返す。接続失敗/401 は None。timeout 2s、urllib 使用）、
  `cancel(job_id, mode, timeout_s)`、`start_recipe(resource, recipe, params)`
- app.py 追加ルート（edit_dir と独立。常時登録するが、control が無ければ 503）:
  - `GET /api/control/status` → `{"available": bool, "backend_id": ...}`
  - `POST /api/control/jobs/{job_id}/cancel` → 転送
  - `POST /api/control/start-recipe` → 転送
  - POST は `Content-Type: application/json` のみ（M3 と同じ流儀）
- job_detail.html: 非終端ジョブに「キャンセル」ボタン 3 種
  （after_current_step / immediate / safe_shutdown。safe_shutdown と immediate は
  confirm ダイアログ）。`/api/control/status` が available のときだけ表示
- recipes.html: 各レシピに「実行」フォーム（resource_name 入力 + パラメータ
  name=value 入力、M3 の dry-run パネルと同じ簡易形式）。同じく available 時のみ

## テスト

### test_control_plane_m4.py（serve プロセス側、TestClient で in-process）

| テスト | 検証 |
|---|---|
| test_health_requires_token | token 無し/不一致 401、一致 200 |
| test_cancel_invalid_mode | 422 |
| test_cancel_running_job | MockBackend で長い wait ジョブ→cancel(after_current_step)→終端 |
| test_start_recipe_creates_job | mock 定義のレシピで job 登録され store に載る |
| test_start_recipe_ignores_override_safety | body に override_safety=True を入れても False で実行される（JobManager 呼び出しを監視 or audit で確認） |
| test_audit_recorded | cancel / start それぞれ audit テーブルに tool_name="control.*" の行 |
| test_control_file_roundtrip | write→read→remove。壊れた JSON は None |

### test_ui_m4.py（UI プロセス側）

| テスト | 検証 |
|---|---|
| test_status_unavailable_without_file | control.json 無し → available: false、ボタン非表示 |
| test_proxy_cancel_and_start | fake control server（テスト内で uvicorn を立てず、ControlClient を monkeypatch するか、TestClient ベースの疑似転送）で転送・token 付与を検証 |
| test_proxy_rejects_non_json | 415/422 |
| test_job_detail_shows_cancel_buttons | available を monkeypatch して HTML にボタン出現、unavailable で消える |

### リグレッション
- `tests/test_separation_boundary.py` / `diagnose tool-surface`（50 個不変）
- test_ui_m1/m2/m3 → 全スイート 0 failed / 28 skipped

## バージョン・ドキュメント

- v2.23.0（pyproject + __init__ + CHANGELOG 既存書式）
- docs/web_ui.md に M4 追記（--control-port、control.json、UI ボタン、
  「visa-mcp serve での有効化は visa-mcp 側の対応が必要」の注記）

## スコープ外

- visa-mcp serve へのコントロールプレーン組み込み（別タスク）
- 認証の高度化（ユーザー管理等）、外部 bind、複数 serve の同時制御
- DSL プラン投入・グループジョブ投入（レシピ単発のみ）

## 作業手順

1. control_plane.py + test_control_plane_m4.py（token/audit/cancel を先に固める）
2. server.py compose_server + cli.py serve 統合（既定動作不変をテストで担保）
3. UI プロキシ + 画面 + test_ui_m4.py
4. docs + version + 全スイート（テスト完了を確認してから報告。3分無出力はハング切り分け）
5. git commit はしない（Fable 検証後）
