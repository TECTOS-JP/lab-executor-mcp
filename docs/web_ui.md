# lab-executor Web UI (M1: 読み取り専用モニタ / M2: SSE + グラフ)

`lab-executor ui` は localhost に **読み取り専用** の実験モニタ Web UI を起動する。
実験ランタイム (serve / MCP ツール面) には一切書き込まず、state DB を read-only で
読むだけの独立プロセスとして動く。

全体構想 (3 プレーン分離、M1〜M4) は AutoLaboKnowlege ウィキ
`wiki/concepts/lab-executor-web-ui.md` を参照。本書は **M1** の利用者向けドキュメント。

## インストール

Web UI は必須依存ではなく、optional-dependencies `[ui]` にある。

```bash
pip install "lab-executor-mcp[ui]"
```

`[ui]` extra が未インストールのまま `lab-executor ui` を実行すると、案内メッセージを
表示して exit 1 する:

```
lab-executor ui は [ui] extra が必要です: pip install lab-executor-mcp[ui]
```

追加される依存: `fastapi` / `uvicorn` / `jinja2`。htmx / htmx SSE 拡張 /
uPlot はリポジトリに同梱 (`src/lab_executor/ui/static/vendor/`) しており、
ラボがオフラインでも動く。

## 起動

```bash
lab-executor ui                       # http://127.0.0.1:8080 、DB は default_store_path()
lab-executor ui --port 9000           # ポート変更
lab-executor ui --db /path/state.sqlite   # 明示的に state DB を指定
```

| オプション | 既定値 | 説明 |
|---|---|---|
| `--host` | `127.0.0.1` | バインドホスト。外部ホスト指定時は認証なし警告を表示 |
| `--port` | `8080` | バインドポート |
| `--db` | `default_store_path()` | 読み込む state DB のパス (`VISA_MCP_STATE_DB` 環境変数 or `~/.visa-mcp/state.sqlite`) |

## 画面

### ダッシュボード (`GET /`)

- 全ジョブ一覧。8 状態 (queued / running / waiting / completed / failed /
  cancelling / cancelled / timeout / interrupted) を色分け表示。
- `current_phase` (observation の `compute_current_phase`) を併記。
- htmx が 2 秒ごとに `GET /partials/jobs-table` を取得して自動更新。
- ヘッダに serve プロセス死活の **目安**:
  - `● 稼働中` — 最終書き込みから 30 秒未満
  - `○ アイドルまたは停止` — それ以上、または未書き込み

  これは「最後に誰かが state DB に書いた時刻」に基づく目安であり、死活を断定する
  ものではない (stdio serve が複数立つ構成のため)。

### ジョブ詳細 (`GET /jobs/{job_id}`)

- ステップ実行履歴 (`job_steps`)。
- 正規化イベントタイムライン (observation の `normalize_event`。古い順に表示、
  severity で色分け)。実行中のジョブはタイムラインを 2 秒ポーリング更新。
- 終端ジョブなら `build_run_summary` によるサマリー (steps/targets 集計、verify、
  duration、failures)。
- 不在ジョブは 404 + 案内ページ。

### JSON API

| ルート | 内容 |
|---|---|
| `GET /api/jobs` | ジョブ一覧の view model |
| `GET /api/jobs/{job_id}` | ジョブ詳細の view model |
| `GET /api/health` | health() + UI / パッケージバージョン |

## M1 の制約 (割り切り)

- **読み取り専用**: SQLite は `file:...?mode=ro` + `PRAGMA query_only=ON` で開く。
  書き込み経路は存在しない。`JobStore` はコンストラクタで schema を書き込むため
  UI からはインスタンス化しない。
- **観測ロジックは再実装しない**: severity / phase / outcome / timeline 正規化は
  `lab_executor.observation` の既存純関数を import して使う。AI (MCP) と人間 (UI) が
  同じビューを見ることが設計の核。
- **認証なし**: localhost バインド前提。`--host` を外部にする場合は起動時に警告し、
  ダッシュボードにもバナーを出すが、認証・認可は M1 スコープ外。
- **DB 未作成 / 古いスキーマ**: 案内ページ (HTML は 503 + error.html、API は JSON 503)
  を返し、500 にはしない。

## M2 (v2.21.0): SSE ライブ更新 + スイープグラフ

M2 では M1 の read-only 境界を保ったまま、更新方式とグラフを追加した。
設計文書は `docs/web_ui_m2_plan.md`。

### SSE ライブ更新

ダッシュボード / ジョブ詳細の更新を htmx 2 秒ポーリングから
**SSE (Server-Sent Events)** に置き換えた。

| ルート | 内容 |
|---|---|
| `GET /sse/dashboard` | 約 1.5 秒間隔で jobs + health を読み、前回送信との**ハッシュ比較で変化時のみ** `_jobs_table.html` フラグメントを送る。15 秒毎に `: ping` keep-alive |
| `GET /sse/jobs/{id}` | 同様に timeline フラグメントを送る。ジョブが**終端状態になったら最終フラグメントと長い `retry` を送ってストリームを閉じる** (終端後の無限再接続を避ける) |

- SQLite 読み取りは `asyncio.to_thread` でイベントループを塞がず、
  切断は `request.is_disconnected()` で検出する。
- htmx の SSE 拡張 (`static/vendor/htmx-sse.js`) を同梱。
- 従来の partial ルート (`/partials/jobs-table` /
  `/partials/jobs/{id}/timeline`) はフォールバック・テスト用に残置。

### スイープグラフ (uPlot)

sweep ジョブ (DSL の `sweep` step) の測定値をジョブ詳細に折れ線グラフで表示する。

- 抽出は observation の `_extract_sweep_views` (MCP `get_job_sweep_view` と
  同じ純ヘルパ) を **import して再利用**。UI は再実装しない。
- `views.sweep_chart_view` が `{x, series, x_label}` (uPlot が食える形) に変換。
  `value_numeric` が None の点は gap として保持する (uPlot が線を欠く)。
- `GET /api/jobs/{id}/sweep` が同 JSON を返し、実行中ジョブは SSE timeline
  受信毎に再取得して `setData` でグラフを更新する。
- uPlot 1.6.30 を `static/vendor/` にベンダリング (オフライン動作)。
- sweep 点が 0 個のジョブではグラフセクションを出さない。

### N+1 解消

一覧の「job 毎に最新 event を引く」N+1 を、相関サブクエリ 1 発で取得する
`ReadOnlyJobStore.list_jobs_with_last_event(limit)` に置き換えた。

追加される static: `uPlot.iife.min.js` / `uPlot.min.css` / `htmx-sse.js`
(いずれも同梱。新たな Python 依存は無し)。

## M3: レシピエディタ (v2.22.0)

`lab-executor ui --edit-dir <PATH>` で起動すると、`<PATH>` 配下の機器定義
YAML 内のレシピをブラウザで編集できる。保存は「検証 → (任意で dry-run) →
git commit」の流れを強制する。

    lab-executor ui --edit-dir ./registry/instruments/mydev

### 書き込み境界 (M1/M2 との違い)

- UI が初めて **書き込み能力** を持つが、書き込み先は **`--edit-dir` 配下の
  YAML ファイルとその git 履歴のみ**。state DB への接続は引き続き read-only
  (`mode=ro` + `PRAGMA query_only=ON`)。
- **`--edit-dir` 未指定なら編集機能は完全無効** (M1/M2 と同一の read-only UI)。
  `/recipes` や `/api/edit/*` ルートは登録すらされない (404)。
- パストラバーサル防御: rel は `resolve()` 後に edit-dir 配下であることを検証
  し、`..` / 絶対パス / シンボリックリンク経由の脱出を拒否する。
- 検証ゲート: 保存時に必ずサーバ側で `validate_instrument_file` により再検証し、
  **errors があれば保存しない** (警告のみなら保存可)。クライアントの「検証成功
  で保存ボタン解放」は UX のみ。
- **外部 host との併用は起動拒否**: `--edit-dir` 指定時に `--host` が
  127.0.0.1 / localhost / ::1 以外だと exit 1 (認証なしの書き込み経路を外部
  公開しない。M1 の警告より一段強い措置)。

### 画面・API

| ルート | 内容 |
|---|---|
| `GET /recipes` | ファイル / レシピ一覧 |
| `GET /recipes/edit/{rel}` | エディタ (CodeMirror yaml + 検証/dry-run/保存パネル) |
| `GET /api/edit/files` | JSON 一覧 (rel + recipe 名) |
| `GET /api/edit/file/{rel}` | JSON `{content}` |
| `POST /api/edit/validate` | `{rel, content}` → ValidationReport (保存しない) |
| `POST /api/edit/dryrun` | `{rel, content, recipe, parameters}` → 展開 Step 列。パース/式評価エラーは 422 |
| `POST /api/edit/save` | `{rel, content, message}` → 検証 → LF 保存 → git commit。検証エラーは 422 |

POST 系は `Content-Type: application/json` のみ受け付ける。

### dry-run

編集中の YAML 文字列を `InstrumentDefinition` にパースし、指定レシピ +
パラメータで `recipe_to_plan` を実行して、`$target_v * 1.1` のような式が具体値に
解決された IR Step 列 (種別 / コマンド / instrument / 解決済み引数 / wait 秒数)
を返す。validate / dry-run / パースのロジックは既存 API を **import して再利用**
(再実装しない)。Mock 実行は M3 スコープ外。

### git

`git add <file>` + `git commit`。edit-dir が git repo でなければ初回に
`git init` する。author / committer は `lab-executor-ui` を `-c user.name/
-c user.email` で指定。commit message は `ui: edit <rel>` + ユーザー入力。
commit のみ失敗した場合 (変更なし等) は **ファイルは保存済み・commit のみ失敗**
を返り値 (`committed=False` + `commit_error`) で区別する。

追加される static: `codemirror.min.js` / `codemirror.min.css` / `cm-yaml.min.js`
(CodeMirror 5.65.16 をベンダリング。新たな Python 依存は無し)。

### スコープ外 (M3)

- フォーム⇔YAML 双方向同期、新規ファイル作成、Mock 実行での dry-run、
  実行中ジョブとの競合検知、認証。

## M4 以降の予定

- **M4**: ジョブ操作 (cancel など)

いずれも read-only 境界 (state DB) を保ったまま段階的に拡張する。詳細は
`wiki/concepts/lab-executor-web-ui.md` を参照。
