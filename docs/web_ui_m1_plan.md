# Web UI M1 実装計画 (読み取り専用モニタ)

作成: 2026-07-03 / 計画: Claude Fable 5 / 実装・テスト: Claude Opus / 最終確認: Claude Fable 5

全体構想（3プレーン分離、M1〜M4）は AutoLaboKnowlege ウィキ
`wiki/concepts/lab-executor-web-ui.md` を参照。本書は **M1 のみ** の実装計画。

## M1 のゴール

`lab-executor ui` サブコマンドで localhost に読み取り専用の実験モニタ Web UI を起動できる。

- ダッシュボード: 全ジョブ一覧（8状態の色分け、現在フェーズ、htmx 2秒ポーリング更新）
- ジョブ詳細: ステップ実行履歴、正規化イベントタイムライン、終端ジョブのサマリー
- serve プロセス死活の目安表示（DB 最終書き込み時刻ベース）
- **実験ランタイムへの書き込みゼロ**（SQLite は read-only モードで開く）

## 絶対制約（変更してはならないもの）

1. **MCP ツール面 50 個に触れない** — `server.py` / `tools/` / serve 経路は一切変更しない
2. **SQLite への書き込み経路を作らない** — 接続は必ず `sqlite3.connect(f"file:{path}?mode=ro", uri=True)`。
   `JobStore` クラスは**インスタンス化しない**（コンストラクタが schema 作成 = 書き込みを行うため）
3. **observation ロジックを再実装しない** — severity / phase / outcome / timeline 正規化は
   `lab_executor.observation` の既存純関数（`normalize_event` / `event_kind` / `event_severity` /
   `compute_job_outcome` / `compute_current_phase` / `build_run_summary`）を import して使う。
   AI（MCP）と人間（UI）が同じビューを見ることが設計の核
4. **必須依存に追加しない** — fastapi / uvicorn / jinja2 は optional-dependencies `ui` に置き、
   cli 側は遅延 import + 未インストール時に `pip install lab-executor-mcp[ui]` を案内するエラー

## 新規ファイル構成

```
src/lab_executor/ui/
├── __init__.py
├── readonly_store.py   # read-only SQLite アクセサ
├── views.py            # observation 関数を使ったビューモデル構築（純関数）
├── app.py              # create_app(db_path) -> FastAPI
├── templates/
│   ├── base.html
│   ├── dashboard.html
│   ├── job_detail.html
│   ├── _jobs_table.html     # htmx ポーリング用パーシャル
│   ├── _timeline.html       # 同上
│   └── error.html           # DB 未作成・ジョブ不在などの案内
└── static/
    ├── style.css
    └── vendor/htmx.min.js   # ベンダリング（ラボはオフラインの可能性があるため CDN 不可）
tests/test_ui_m1.py
docs/web_ui.md               # 起動方法・画面説明の短い利用者向けドキュメント
```

## 各モジュールの仕様

### readonly_store.py

```python
class UiStoreError(Exception): ...        # DB 不在など、UI が案内ページを出すための例外

class ReadOnlyJobStore:
    def __init__(self, db_path: Path): ...   # 存在チェックのみ。接続はリクエスト毎
    def list_jobs(self, status_filter=None, limit=100) -> list[dict]
    def get_job(self, job_id) -> dict | None
    def list_events(self, job_id, limit=200, offset=0) -> list[dict]   # 新しい順
    def list_steps(self, job_id) -> list[dict]
    def list_target_runs(self, job_id) -> list[dict]
    def health(self) -> dict   # {"db_path", "last_write_at", "seconds_since_last_write", "active_jobs"}
```

- 行→dict 変換は `job/store.py` の `list_events` / `list_steps` / `list_target_runs` /
  `_row_to_record` と**同じキー名**を返すこと（views.py と observation.py がそのまま使えるように）。
  `JobStore._row_to_record` は staticmethod なので `row_factory=sqlite3.Row` を設定した上で
  再利用してよい（インスタンス化はしない）
- `health()` の `last_write_at` は `MAX(jobs.updated_at)` と `MAX(job_events.timestamp)` の大きい方
- DB ファイル不在 → `UiStoreError`。テーブル不在（古い DB）→ 同様に UiStoreError で案内
- WAL 副産物: read-only 接続でも `-wal`/`-shm` は writer（serve 側）が管理するので問題ない。
  ただし `PRAGMA query_only=ON` も併用して二重に保護する

### views.py（純関数のみ、FastAPI 非依存）

- `job_row_view(job: dict, last_event_type: str | None) -> dict`
  — status に `compute_job_outcome`（target_runs は一覧では省略可・None 扱い）と
  `compute_current_phase` を合成した表示用 dict
- `job_detail_view(job, steps, events, target_runs) -> dict`
  — `normalize_event` で timeline を構築（表示は古い順に並べ直す）、
  終端ジョブなら `build_run_summary` を含める
- `STATUS_COLORS: dict[str, str]` — 8状態 + outcome の色クラス名

### app.py

`create_app(db_path: Path | None = None) -> FastAPI`（省略時 `default_store_path()`）

| ルート | 内容 |
|---|---|
| `GET /` | ダッシュボード HTML |
| `GET /jobs/{job_id}` | ジョブ詳細 HTML（不在は 404 + error.html） |
| `GET /partials/jobs-table` | ジョブ一覧テーブル断片（htmx が 2 秒毎に取得） |
| `GET /partials/jobs/{job_id}/timeline` | タイムライン断片（実行中のみポーリング） |
| `GET /api/jobs` | JSON（一覧、view model そのまま） |
| `GET /api/jobs/{job_id}` | JSON（詳細） |
| `GET /api/health` | JSON（health() + UI バージョン） |

- `UiStoreError` は exception handler で error.html（HTML ルート）/ JSON 503（api ルート）に変換
- ダッシュボードのヘッダに health 表示: 最終書き込みからの経過秒で
  `● 稼働中`（<30s）/ `○ アイドルまたは停止`（それ以上）を表示（断定はしない。stdio serve が
  複数立つ構成なので「最後に誰かが書いた時刻」しか分からない）

### cli.py

- サブコマンド `ui`: `--host`(default 127.0.0.1) / `--port`(default 8080) / `--db`(default: `default_store_path()`)
- ハンドラ内で `from lab_executor.ui.app import create_app` と `import uvicorn` を遅延 import。
  ImportError 時は `[ui] extra が必要: pip install lab-executor-mcp[ui]` を stderr に出して exit 1
- 既存の `main()` の dispatch パターン（`if args.command == ...`）に合わせる

### pyproject.toml

```toml
[project.optional-dependencies]
ui = ["fastapi>=0.110", "uvicorn>=0.29", "jinja2>=3.1"]
dev = ["pytest", "pytest-asyncio", "pyyaml", "fastapi>=0.110", "uvicorn>=0.29", "jinja2>=3.1", "httpx"]
```

（httpx は starlette TestClient に必要。dev に ui 依存を含めて CI でテストが走るようにする）

## テスト仕様（tests/test_ui_m1.py）

fixture: `tmp_path` に **JobStore（書き込み可）でシード** → `ReadOnlyJobStore` / `create_app` はそのパスを読む。
シード内容: completed 1件（steps + events + result 付き）、running 1件（step 途中）、failed 1件（error_class 付き）。

| テスト | 検証内容 |
|---|---|
| test_dashboard_renders | `GET /` 200、シードした job_id / recipe 名 / 状態ラベルが HTML に含まれる |
| test_job_detail_completed | 詳細 200、timeline に step_completed 由来行、summary セクションあり |
| test_job_detail_running | phase が running 系、summary なし |
| test_job_not_found | `GET /jobs/nonexistent` → 404 |
| test_api_jobs_shape | `/api/jobs` の JSON キー（job_id/status/phase/…）と件数 |
| test_api_health | last_write_at が ISO8601、active_jobs 数一致 |
| test_missing_db_friendly | 存在しないパスで create_app → `GET /` が 500 でなく案内表示（503/200+error.html） |
| test_readonly_enforced | ReadOnlyJobStore の接続で INSERT を試み `sqlite3.OperationalError` になること、および一連の GET 後に DB ファイルの内容ハッシュが不変であること |
| test_partials_render | 2 つの partial ルートが 200 |

既存テストのリグレッション確認: `python -m pytest` を全件実行して green を確認（既存 conftest.py に注意）。

## 実装しないもの（M1 スコープ外）

- SSE / グラフ（M2）、レシピ編集（M3）、ジョブ操作（M4）
- 認証（localhost バインドのみで M1 は割り切る。--host を外部にする場合の警告表示だけ入れる）
- CHANGELOG / バージョン番号の変更（リリース手順は別途人間が判断）

## 作業手順の指定

1. readonly_store → views → app → templates → cli → pyproject の順で実装
2. htmx.min.js を https://unpkg.com/htmx.org@1.9.12/dist/htmx.min.js から取得して vendor 配置
   （取得不可ならテンプレートを素の meta refresh 5 秒にフォールバックし、その旨を報告）
3. `pip install -e .[dev]` 後、新規テスト → 全テストの順で実行
4. docs/web_ui.md を書く（起動方法、画面、M1 の制約、M2 以降の予定へのリンク）
5. **git commit はしない**（レビュー後に人間が判断）
