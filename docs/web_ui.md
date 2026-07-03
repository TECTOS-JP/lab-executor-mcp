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

## M3 以降の予定

- **M3**: レシピ (DSL plan) の閲覧・編集
- **M4**: ジョブ操作 (cancel など、書き込み経路の導入)

いずれも本 M1/M2 の read-only 境界を保ったまま段階的に拡張する。詳細は
`wiki/concepts/lab-executor-web-ui.md` を参照。
