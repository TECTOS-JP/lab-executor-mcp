# Web UI M2 実装計画 (SSE ライブ更新 + スイープグラフ + N+1 解消)

作成: 2026-07-03 / 計画: Claude Fable 5 / 実装・テスト: Claude Opus / 最終確認: Claude Fable 5

前提: M1 (v2.20.0, `docs/web_ui_m1_plan.md`) がコミット済み。
M1 の絶対制約 4 項はすべて M2 でも有効:
**(1) MCP ツール面・serve 経路に触れない / (2) SQLite は read-only のみ /
(3) observation ロジックを再実装しない / (4) 必須依存に追加しない**。

## M2 のゴール

1. ダッシュボード・ジョブ詳細のライブ更新を htmx ポーリングから **SSE** に置き換える
   （partial ルートはフォールバック・テスト用に残す）
2. ジョブ詳細に **スイープグラフ**（uPlot）を表示する
3. 一覧の **N+1 クエリを解消**する（M1 の既知課題）

## 1. N+1 解消 (readonly_store.py)

`ReadOnlyJobStore.list_jobs_with_last_event(limit=100) -> list[dict]` を追加。
jobs と「各 job の最新 event_type」を 1 クエリで取る:

```sql
SELECT j.*, (
    SELECT e.event_type FROM job_events e
    WHERE e.job_id = j.job_id
    ORDER BY e.event_id DESC LIMIT 1
) AS last_event_type
FROM jobs j
ORDER BY j.created_at DESC, j.rowid DESC LIMIT ?
```

返り値は `_row_to_record().to_dict()` + `last_event_type` キー。
`app.py` の `_build_job_rows` をこれに置き換える（`job_row_view` は変更不要）。
テストで「旧 N+1 方式と同じ結果になること」を等価性検証する。

## 2. SSE ライブ更新

### サーバ側 (app.py)

- `GET /sse/dashboard` — `StreamingResponse(media_type="text/event-stream")`。
  ループ: 約 1.5 秒間隔で jobs + health を読み、**前回送信内容とハッシュ比較して
  変化時のみ** `event: jobs-table` として `_jobs_table.html` レンダリング済み
  フラグメントを送る（初回は必ず送る）
- `GET /sse/jobs/{job_id}` — 同様に `event: timeline` で `_timeline.html`
  フラグメントを送る。**ジョブが終端状態になったら最終フラグメントを送って
  ループを終了**（クライアント側で EventSource を閉じる。後述）
- 実装上の注意:
  - SQLite 読み取りは `await asyncio.to_thread(...)` でイベントループを塞がない
  - 各サイクルで `await request.is_disconnected()` をチェックして切断時に終了
  - SSE の data は複数行になるため、各行に `data: ` プレフィックスを付ける
    正しいフレーミングを実装する（ヘルパ関数 `_sse_frame(event, data) -> str`）
  - `Cache-Control: no-cache` ヘッダを付ける
  - keep-alive として 15 秒毎にコメント行 `: ping` を送る

### クライアント側 (templates)

- htmx SSE 拡張を https://unpkg.com/htmx.org@1.9.12/dist/ext/sse.js から取得して
  `static/vendor/htmx-sse.js` にベンダリング
- dashboard.html: `hx-ext="sse" sse-connect="/sse/dashboard"
  sse-swap="jobs-table"` に置き換え。既存の `hx-get` ポーリング属性は削除
  （partial ルート自体は残す）
- job_detail.html: 非終端ジョブのみ `sse-connect="/sse/jobs/{id}"
  sse-swap="timeline"`。終端イベント受信の判定はサーバがループを閉じることで
  EventSource の再接続が走るが、終端後の再接続は即座に最終状態を1回送って
  また閉じる（無限再接続を避けるため、終端時は `retry: 3600000` を送ってから
  閉じる方式にする）

## 3. スイープグラフ (uPlot)

### データ抽出 (views.py)

`sweep_chart_view(sweep_points: list[dict]) -> dict | None` を追加。
入力は `lab_executor.tools.observation._extract_sweep_views(store, job_id)` の
返り値（この private ヘルパは `store.list_steps()` しか呼ばないため
ReadOnlyJobStore をそのまま渡せる。M1 の `_row_to_record` と同様、
同一パッケージ内の意図的な再利用としてコメントを残す）。

出力（uPlot が直接食える形）:

```python
{
  "x": [sweep_value か、None なら sweep_index],          # 昇順
  "series": [
    {"label": "psu1: measure_voltage", "values": [ ... ]},  # (instrument, command) 毎
  ],
  "x_label": "sweep_value" | "sweep_index",
}
```

- `value_numeric` が None の点は null として保持（uPlot は gap 表示できる）
- sweep 点が 0 個なら None を返し、テンプレートはグラフセクションを出さない

### 表示

- uPlot を https://unpkg.com/uplot@1.6.30/dist/uPlot.iife.min.js と
  uPlot.min.css からベンダリング（`static/vendor/`）
- job_detail.html: sweep データがあれば `<script type="application/json"
  id="sweep-data">` に JSON を埋め込み、インライン JS で uPlot を初期化
- ライブ更新: 実行中ジョブは SSE の timeline イベント受信時に
  `GET /api/jobs/{id}/sweep`（新設、`sweep_chart_view` の JSON を返す）を
  再取得してチャートを `setData` で更新する

## 4. テスト (tests/test_ui_m2.py)

| テスト | 検証内容 |
|---|---|
| test_list_jobs_with_last_event_equivalence | 新メソッドの結果が「list_jobs + 各 job の list_events(limit=1)」の組と一致 |
| test_sse_dashboard_first_frame | httpx の stream で `/sse/dashboard` に接続し、最初のフレームが `event: jobs-table` + シードした job_id を含む |
| test_sse_job_terminal_closes | 終端ジョブの `/sse/jobs/{id}` がフラグメント送信後にストリーム終了する |
| test_sse_frame_helper | `_sse_frame` が複数行 data を正しく `data: ` プレフィックスでフレーミング |
| test_sweep_chart_view_series | sweep payload 付き steps をシードし、x / series / null 保持を検証 |
| test_sweep_chart_view_empty | sweep なしジョブで None |
| test_api_sweep_endpoint | `/api/jobs/{id}/sweep` の JSON 形状 |
| test_dashboard_has_sse_attrs | ダッシュボード HTML に sse-connect 属性、uPlot/sse.js の静的配信 200 |

sweep payload のシード形式は `_extract_sweep_views` の実装に合わせる
（step result に `sweep_index` / `sweep_value` / `instrument` / `command` /
`raw_response` / `value_numeric` 相当を持たせる。既存の export 系テストの
シード方法を参考にすること）。

## 5. バージョン・ドキュメント

- version 2.21.0（pyproject.toml + `__init__.py`）、CHANGELOG.md に
  v2.21.0 エントリ（既存の書式・合言葉スタイルに合わせる）
- docs/web_ui.md に M2 の内容（SSE・グラフ・環境変数なし）を追記
- AutoLaboKnowlege ウィキの更新は不要（Fable が最終確認後に行う）

## スコープ外

- レシピ編集（M3）、ジョブ操作（M4）、認証
- instrument 時系列グラフ（sweep 以外）は M2 では見送り
- WebSocket 化（SSE で足りる）

## 作業手順

1. readonly_store の新メソッド + 等価性テスト
2. `_sse_frame` ヘルパ + SSE ルート 2 本 + テンプレート改修
3. uPlot ベンダリング + sweep_chart_view + `/api/jobs/{id}/sweep` + テンプレート
4. tests/test_ui_m2.py 全件 → tests/test_ui_m1.py（リグレッション）→ 全スイート
5. **git commit はしない**（Fable の最終確認後にコミット）
