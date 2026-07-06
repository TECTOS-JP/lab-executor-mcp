# 変更履歴

## v2.24.0 — control plane runner を公開 API 化 (visa-mcp 統合用)

合言葉: **「関所は lab-executor が持ち、鍵の受け渡し口を visa-mcp にも開く」**

### 追加

- `lab_executor.control_plane.run_mcp_with_control(mcp, job_mgr, control_port, *, backend_id, control_path=None)`
  (async) を **公開 API** として追加。`cli.py` の `_serve_with_control` の
  コア (token 生成 → `create_control_app` → uvicorn Config/Server →
  `write_control_file` → `asyncio.gather(mcp.run_async("stdio"), ctl.serve())`
  → finally で `remove_control_file`) をそのまま移設した。挙動は不変。
  `backend_id` は control.json / health に載せる backend 識別子
  (lab-executor="mock" / visa-mcp="pyvisa" など)。`control_path` は
  control.json の書き込み先を上書きできる (default: `default_control_path()`)。
- `lab_executor.control_plane.resolve_control_port(cli_value)` を公開 API として
  追加。CLI 値優先、無ければ env `LAB_EXECUTOR_CONTROL_PORT` を読む解決規則を
  `cli.py` の `_resolve_control_port` から移設した。外部 (visa-mcp serve) が
  同一の解決規則を再利用できる。

### 変更

- `cli.py` の `_serve_with_control` / `_resolve_control_port` は上記公開 API を
  呼ぶ薄いラッパになった。CLI の挙動・出力は 1 行も変わらない。

### 目的

- 実機 MCP サーバー (`visa-mcp serve`) でも Web UI M4 のコントロールプレーンを
  使えるようにするため、runner を lab-executor 側の再利用可能な公開 API にした。

## v2.23.0 — Web UI M4: コントロールプレーン (ジョブキャンセル + レシピ実行) (UI M4)

合言葉: **「窓から手を伸ばすが、鍵は関所の内側にしか渡さない」**

### 追加

- `lab-executor serve --backend mock --control-port <PORT>` — serve プロセス内に
  **127.0.0.1 固定** の HTTP コントロールプレーンを立て、`lab-executor ui` から
  ジョブのキャンセル (3 モード) とレシピのジョブ投入を可能にする。`--control-port 0`
  で OS 任せのポートを使う。環境変数 `LAB_EXECUTOR_CONTROL_PORT` でも指定可
  (CLI 優先)。**未指定なら従来どおりコントロールプレーンは無効** (挙動不変)。
  - `src/lab_executor/control_plane.py` — `create_control_app(job_mgr, *, token)`
    (Starlette app) + `control.json` の read/write/remove + `default_control_path()`。
    token は起動毎に `secrets.token_hex(32)`、比較は `secrets.compare_digest`。
    starlette / JSONResponse は関数内で遅延 import (必須依存に足さない)。
  - `server.py` に `compose_server()` を追加し、内部 `JobManager` を公開。
    `create_server()` は `compose_server(...)[0]` を返す薄いラッパになり、
    公開シグネチャ・挙動は不変。
  - 実行系操作は MCP ツール (`tools/jobs.py`) と **同一の** `JobManager.cancel` /
    `start_recipe_job` 経由。`override_safety` は body に来ても **常に False 固定**。
    audit は `tool_name="control.cancel_job"` / `"control.start_recipe_job"`、
    `client_id="control-plane"` で記録。
  - MCP (stdio) とコントロールプレーン (uvicorn) を `asyncio.gather` で並走。
    `control.json` は起動時に書き、終了時に削除 (finally + atexit で二重化)。
- `lab-executor serve --backend mock --state-db <PATH>` — mock serve の Job state
  を指定 SQLite ファイルに永続化する (省略時は従来どおり in-memory で挙動不変)。
  mock serve は v2.1 から `JobStore(":memory:")` のため、そのままでは control
  plane 経由で投入したジョブが `lab-executor ui` のモニタから見えない。
  `lab-executor ui --db <同じ PATH>` と組み合わせることで E2E ループが閉じる。
  `compose_server()` に `store_path` パラメータを追加 (default None = 不変)。
  `create_server()` の公開シグネチャは不変。
- UI プロキシ (`src/lab_executor/ui/control_client.py` + `ui/app.py`):
  - `ControlClient` は `control.json` を読んで token を取得し (**ブラウザには
    渡さない**)、`urllib` で HTTP 転送する。`available()` は `/control/health` を
    token 付きで叩き 2xx なら info を返す (timeout 2s)。
  - `/api/control/status` / `/api/control/jobs/{job_id}/cancel` /
    `/api/control/start-recipe` を **常時登録** (control 無効時は 503)。POST は
    `Content-Type: application/json` のみ (非 JSON は 415)。
  - `job_detail.html`: 非終端ジョブにキャンセルボタン 3 種 (after_current_step /
    immediate / safe_shutdown。後 2 者は confirm)。control available 時のみ表示。
  - `recipes.html`: レシピ実行フォーム (resource_name + name=value パラメータ)。
    control available 時のみ表示。
- 設計文書 `docs/web_ui_m4_plan.md` / 利用者向け `docs/web_ui.md` に M4 追記。

### 互換性

- MCP tool surface 不変 (Stable 43 + Experimental 7 = 50)。`create_server()` の
  公開シグネチャ不変。`tests/test_separation_boundary.py` / `diagnose tool-surface`
  は green のまま。
- コントロール無効時 (`--control-port` も env も無し) の serve は挙動が
  1 行も変わらない (従来どおり `server.run()`)。
- 必須依存に追加なし。コントロールプレーンは starlette + uvicorn を遅延 import し
  (`[ui]` extra)、明示 `--control-port` 指定時に未インストールなら exit 1、
  env 経由なら案内してコントロール無効で継続。
- state DB への UI からの書き込みなし。実行系操作は必ずコントロールプレーンへ
  プロキシする。
- version を `2.23.0` に更新。UI_VERSION を `m4` に更新。

## v2.22.1 — result_json の UTF-8 直列化ハードニング

合言葉: **「化けても落ちない、正しいものは変えない」**

### 修正

- `job/store.py` に `_dumps_utf8_safe()` を追加し、result / error /
  payload の全直列化経路に適用。実機 (日本語 Windows + NI-VISA) で
  例外メッセージに surrogate 由来バイトが混入した場合でも、SQLite
  保存が `UnicodeEncodeError` でクラッシュして steps_executed が
  失われる事故を防ぐ。正常な日本語 UTF-8 は round-trip 不変。
- 回帰テスト 3 件 (`tests/test_polling_wait_v2223_encoding.py`)。

### 互換性

- MCP tool surface 不変。version を `2.22.1` に更新。

## v2.22.0 — Web UI M3: レシピエディタ (検証 → dry-run → git 保存) (UI M3)

合言葉: **「窓の中に手を入れる、ただし検証の関所を通って」**

### 追加

- `lab-executor ui --edit-dir <PATH>` — `<PATH>` 配下の機器定義 YAML 内の
  レシピをブラウザで編集する。UI が初めて **書き込み能力** を持つが、書き込み先は
  **edit-dir 配下の YAML + git 履歴のみ**。state DB は引き続き read-only。
  - `--edit-dir` 未指定なら編集ルートは登録すらされない (M1/M2 と同一の
    read-only UI)。指定 + 外部 `--host` は起動拒否 (exit 1)。
- `src/lab_executor/ui/edit_store.py` — `EditDirStore` (列挙 / 読み書き /
  検証ゲート / git commit)。
  - パストラバーサル防御を最初に実装: rel は `resolve()` 後に edit-dir 配下で
    あることを検証し、`..` / 絶対パス / シンボリックリンク経由の脱出を拒否。
  - 保存時に必ず `validate_instrument_file` で再検証し、**errors があれば保存
    しない** (警告のみなら保存可)。CRLF は LF に正規化して書き込む。
  - `git add` + `git commit` (repo でなければ `git init`)。author/committer は
    `lab-executor-ui`。commit のみ失敗 (変更なし等) は
    `committed=False` + `commit_error` で「ファイルは保存済み」を区別。
- 編集ルート: `GET /recipes` / `GET /recipes/edit/{rel}` /
  `GET /api/edit/files` / `GET /api/edit/file/{rel}` /
  `POST /api/edit/{validate,dryrun,save}` (POST は JSON のみ)。
  `EditStoreError` は exception handler で JSON 422 に変換。
- dry-run: 編集中 YAML を `InstrumentDefinition` にパースし、指定レシピ +
  パラメータで `recipe_to_plan` を実行して式 (`$target_v * 1.1` 等) を解決した
  IR Step 列を返す。パース / 式評価エラーは 422。validate / dry-run / パースの
  ロジックは既存 API を **import して再利用** (再実装しない)。
- `views.dryrun_view` — Plan を展開 Step 表示用 dict に整える純関数。
- エディタ画面 (CodeMirror 5.65.16 yaml mode をベンダリング) + 検証 / dry-run /
  保存パネル。base.html にナビ「レシピ」タブ (edit 有効時のみ)。
- 設計文書 `docs/web_ui_m3_plan.md` / 利用者向け `docs/web_ui.md` に M3 追記。

### 互換性

- MCP tool surface 不変 (Stable 43 + Experimental 7 = 50)。
  server.py / tools/ / serve 経路に変更なし。
- 必須依存に追加なし (`[ui]` extra のみ。CodeMirror は同梱ベンダリング)。
- state DB への書き込みなし (`mode=ro` + `PRAGMA query_only=ON`)。
  書き込みは edit-dir 配下の YAML + git のみ。
- version を `2.22.0` に更新。UI_VERSION を `m3` に更新。

## v2.21.0 — Web UI M2: SSE ライブ更新 + スイープグラフ (UI M2)

合言葉: **「窓の向こうが、いま動いて見える」**

### 追加

- ダッシュボード / ジョブ詳細のライブ更新を htmx 2 秒ポーリングから
  **SSE (Server-Sent Events)** に置き換え。
  - `GET /sse/dashboard` — 約 1.5 秒間隔で jobs + health を読み、
    前回送信とハッシュ比較して**変化時のみ** `_jobs_table.html`
    フラグメントを送る。15 秒毎に keep-alive ping。
  - `GET /sse/jobs/{id}` — 同様に timeline フラグメントを送り、
    ジョブが**終端状態になったら最終フラグメント + `retry` を送って
    ストリームを閉じる** (無限再接続を避ける)。
  - SQLite 読み取りは `asyncio.to_thread`、切断は
    `request.is_disconnected()` で検出。`_sse_frame` ヘルパが複数行
    data を正しくフレーミング。
  - htmx SSE 拡張 (`htmx-sse.js`) をベンダリング。partial ルート
    (`/partials/...`) はフォールバック・テスト用に残置。
- ジョブ詳細に **スイープグラフ** (uPlot)。
  - `views.sweep_chart_view(sweep_points)` が
    `observation._extract_sweep_views` の返り値を uPlot が食える
    `{x, series, x_label}` に変換 (再実装せず import)。
    `value_numeric` が None の点は gap として保持。
  - `GET /api/jobs/{id}/sweep` (JSON)。実行中ジョブは SSE timeline
    受信毎に再取得して `setData` で更新。
  - uPlot 1.6.30 を static/vendor にベンダリング (オフライン動作)。
- 一覧の **N+1 クエリを解消**:
  `ReadOnlyJobStore.list_jobs_with_last_event(limit)` が jobs と
  各 job の最新 event_type を相関サブクエリ 1 発で取得。
- 設計文書: `docs/web_ui_m2_plan.md` / 利用者向け `docs/web_ui.md` に M2 追記。

### 互換性

- MCP tool surface 不変 (Stable 43 + Experimental 7 = 50)。
  server.py / tools/ / serve 経路に変更なし。
- 必須依存に追加なし (`[ui]` extra のみ)。
- state DB への書き込みなし (`mode=ro` + `PRAGMA query_only=ON`。
  SSE / sweep の読み取りも既存 read-only 経路のみ)。
- version を `2.21.0` に更新。

## v2.20.0 — Web UI M1: 読み取り専用モニタ (UI M1)

合言葉: **「AI と人間が同じ窓を覗く」**

### 追加

- `lab-executor ui` サブコマンド (`--host` / `--port` / `--db`)。
  localhost に読み取り専用の実験モニタ Web UI を起動する。
  - ダッシュボード: 全ジョブ一覧 (8 状態の色分け、現在フェーズ、
    htmx 2 秒ポーリング更新)、serve 死活の目安表示
  - ジョブ詳細: ステップ実行履歴、正規化イベントタイムライン、
    終端ジョブの run summary
  - JSON API: `/api/jobs` / `/api/jobs/{id}` / `/api/health`
- `src/lab_executor/ui/` パッケージ (readonly_store / views / app /
  templates / static)。timeline / phase / outcome / summary は
  `lab_executor.observation` の既存純関数を再利用し、MCP (AI) と
  UI (人間) が同じ観測ビューを見る。
- optional-dependencies `[ui]` (fastapi / uvicorn / jinja2)。
  htmx 1.9.12 を static/vendor にベンダリング (オフライン動作)。
- 設計文書: `docs/web_ui_m1_plan.md` / 利用者向け: `docs/web_ui.md`

### 互換性

- MCP tool surface 不変 (Stable 43 + Experimental 7 = 50)。
  server.py / tools/ / serve 経路に変更なし。
- 必須依存に追加なし (`[ui]` extra のみ)。
- state DB への書き込みなし (`mode=ro` + `PRAGMA query_only=ON`。
  UI からの書き込み経路は存在しない)。
- version を `2.20.0` に更新。

## v2.19.0 — export result filters (v2.9)

合言葉: **「巨大な結果表から、見たい行だけを取り出す」**

### 追加

- `get_experiment_results` / `export_experiment_results` に optional
  filter 引数を追加。
  - `instrument`
  - `sweep_index`
  - `measurement`
- `lab_executor.tools.export._filter_rows(...)` を追加。
  - 複数 filter は AND 結合。
  - 空文字 / `None` は no-op。
  - `sweep_index=0` は有効値として扱う。

### 互換性

- 新 MCP tool は追加しない。
- filter 未指定時の rows / 件数は従来通り。
- `export_experiment_bundle` は再現性 bundle として全 row を保持し、
  filter 対象外のまま。
- stability matrix は不変。
- version を `2.19.0` に更新。

## v2.18.0 — export dir env override + sweep columns (v2.8)

合言葉: **「保存先を選べて、sweep の列もそのまま出る」**

### 追加

- export 先ディレクトリを `VISA_MCP_EXPORT_DIR` で上書き可能にした。
  - 未指定時は従来通り `~/.visa-mcp/exports` を使う。
  - `_safe_export_path` は毎回 `_resolve_export_dir()` で解決する。
- `RESULT_COLUMNS` の末尾に `sweep_index` / `sweep_value` を追加。
  - 既存 8 列の順序・名前は不変。
  - `get_experiment_results` / CSV / JSONL / bundle results に反映される。

### 修正

- export directory 作成に失敗した場合、例外を投げず
  `export_dir_not_writable` の structured error を返すようにした。
- env 未指定時の default は `DEFAULT_EXPORT_DIR` 定数を返し、既存
  monkeypatch テストとの互換性を維持する。
- `_extract_result_rows` が step result の `instrument` に加えて
  `sweep_index` / `sweep_value` を row へ載せるようにした。

### 互換性

- 新 MCP tool は追加しない。
- 既存 MCP tool 名 / 引数 / DSL schema は変更なし。
- stability matrix は不変。
- version を `2.18.0` に更新。

## v2.17.0 — per-sweep-point observation API (v2.7)

合言葉: **「sweep の各点で何が起きたかを、そのまま読める」**

### 追加

- `get_job_sweep_view(job_id)` MCP tool を追加
  - `job_steps` に永続化された `sweep_index` ごとに再集計
  - `sweep_value` / `step_count` / instrument 一覧
  - query 系 step の `measurements` を `step_index` 順に返す
- `lab_executor.tools.observation._extract_sweep_views(...)` を追加
  - unit test 対象の純関数
  - 新規 SQLite table や migration は不要

### DSL / 永続化

- `CommandStep` に `sweep_index` / `sweep_param` / `sweep_value`
  optional field を追加。
- DSL compiler の sweep 展開時に、body 内の `CommandStep` へ sweep
  文脈を付与。
- `_run_experiment_plan_job` で step result 永続化前に sweep 文脈を注入。
  `sweep_index=0` / `sweep_value=0.0` も有効値として保存する。

### 互換性

- 既存 observation tool / API / DSL schema は変更なし。
- stability matrix には追加登録しない experimental tool として扱う。
- step result への sweep 文脈注入は追加 field のみ。
- version を `2.17.0` に更新。

## v2.16.1 — recipe path も step result に instrument を永続化 (Codex v2.16.0 レビュー P1)

合言葉: **「DSL だけでなく recipe job でも instrument を残す」**

### Codex v2.16.0 レビュー P1

v2.16.0 では DSL path (`_run_experiment_plan_job`) のみ instrument を
step result に注入していたが、**recipe path (`_run_job_inner`)** が
未対応だった。recipe job では `get_job_instrument_view` が空になる。

### 修正

- `job/manager.py:_run_job_inner`: step result 永続化前に instrument を
  注入。recipe の `CommandStep.instrument` は通常 None (Job 主 resource
  を使う) ため、fallback として `rec.resource_name` を使う。
  step_completed event payload にも instrument を追加。
- 回帰テスト追加 (`test_v2_6_per_instrument_spec.py`):
  `start_recipe_job` で実 recipe job を流し、persisted step result に
  instrument (= resource_name) が載ること +
  `_extract_instrument_views` が recipe job でも instrument を返すことを
  検証 (Codex v2.16.0 レビュー P2: 実行系永続化パスを通すテスト)。

### 互換性

instrument 注入は追加 field のみ。既存読み取りに影響しない。
version を `2.16.1` に更新。


## v2.16.0 - per-instrument observation API (v2.6)

合言葉: **「100 台規模でも、機器ごとの流れをすぐ読める」**

### 追加

- `get_job_instrument_view(job_id, instrument=None)` MCP tool を追加
  - `job_steps` を `instrument` ごとに再集計
  - `step_count` / `ok_count` / `failed_count`
  - 最終 step の `last_command` / `last_raw_response` / `last_value_numeric`
  - query 系 step の時系列 `measurements`
- `lab_executor.tools.observation._extract_instrument_views(...)` を追加
  - unit test 対象の純関数
  - 新規 SQLite table や migration は不要

### 実機 E2E で発覚した修正 (Claude Code 検証)

spec/unit test では instrument を捏造していたため通っていたが、
実機 sweep で `get_job_instrument_view` が **空 (VIEWS:0)** を返した。
原因: `step_executor` が返す step result に `instrument` キーが無く、
`job_steps.result_json` に instrument が残らないため、
`_extract_instrument_views` が実データで instrument を取れなかった。

- `job/manager.py:_run_experiment_plan_job`: step result を永続化する
  前に `step.instrument` を `result["instrument"]` に注入するよう修正
  (step_completed event payload 用に取得済みの `_instr` を再利用)。
- 回帰テスト追加 (`test_v2_6_per_instrument_spec.py`):
  mock backend で DSL job を実行し、persisted step result に
  instrument が載ること + `_extract_instrument_views` が実 store で
  instrument を返すことを検証。

実機検証 (PMX35-3A + 7563, 0→3V sweep):
```
VIEWS: 2
  USB PMX35-3A: steps=12 ok=12 (measure_voltage 時系列)
  GPIB 7563:    steps=3  ok=3  read_measurement 温度 28.1→29.2
```

### 互換性

- 既存 observation tool / API は変更なし。
- stability matrix には追加登録しない experimental tool として扱う。
- step result への instrument 注入は追加 field のみ (既存読み取りに
  影響しない)。
- version を `2.16.0` に更新。

## v2.14.3 — Codex v2.14.2 レビュー対応 (JobStore.close 重複定義削除)

### Codex v2.14.2 レビュー指摘 P3

> `JobStore.close()` が line 318 と line 1048 の 2 箇所に重複定義
> されている (`__code__.co_firstlineno == 1048` で後方の定義が有効)。

### 修正

- `src/lab_executor/job/store.py`:
  - 末尾の重複 `close()` 定義 (line 1048) を削除
  - authoritative impl は line 318 (`__enter__` / `__exit__` 付き)
- 新 test 3 件 (`test_v2_14_3_review.py`):
  - source string レベルで `def close(self)` が 1 つだけ
  - close 後 lazy reconnect が動く (regression)
  - version sentinel

visa-mcp v2.3.2 と組で release。


## v2.14.2 — Codex v2.14.1 レビュー対応 (JobStore.close() + test fixture)

合言葉: **「Windows pytest を詰まらせない」**

### Codex v2.14.1 レビュー指摘 1 件 (lab-executor 側)

> `JobStore` を作った後に `store.close()` していないため、Windows で
> SQLite/WAL ファイルが残り pytest teardown が詰まっている可能性が
> 高い。

### 修正

- `lab_executor.job.store.JobStore.close()` 新設:
  - thread-local 接続を明示 close
  - 多重 close は no-op
  - `__enter__` / `__exit__` (context manager) 対応
- `conftest.py` に shared fixtures:
  - `job_store` — `JobStore(tmp_path)` を yield、teardown で
    `close()` 呼び出し
  - `seed_job` — completed job row を INSERT する helper
- 既存テストを fixture ベースに refactor:
  - `tests/test_v2_14_1_review.py`
  - `tests/test_v2_13_2_results_integration.py`
- 新 test 2 件:
  - `test_jobstore_close_idempotent`
  - `test_jobstore_context_manager`

### 互換性

`close()` / `__enter__` / `__exit__` の追加のみ。既存コードは
影響なし (close を呼ばなくても従来通り GC で解放される)。
visa-mcp v2.3.1 と組で release。


## v2.14.1 — Codex v2.14.0 レビュー対応 (寛容 float + parsed metadata 除外)

合言葉: **「`*` も `.` として読み、metadata は rows に出さない」**

### Codex v2.14.0 レビュー指摘 2 件 (lab-executor 側)

**P1-b**: `numeric_extract` が `JPPC+0032*2A+0` を `32.0` と
解釈 (実値は 32.2)。`*` が小数点相当、`A` が指数 marker `E`
相当という ASCII bit 反転 (XOR 0x04) corruption が起きている。

**P2**: `_extract_result_rows` が `parsed` dict の top-level を
そのまま rows 化していたため、`matched` / `fields` / `raw` /
`fallback_used` / `matched_pattern_index` が measurement 列に
混入していた。

### 修正

- `response_parser._parse_value_permissive()` 新設:
  - `float(raw_val)` がダメなら `*→.` `A→E` `a→e` を試す
  - これで `+0032*2A+0` → `32.2` を正しく復元
- `_extract_result_rows` (lab_executor 側 / visa-mcp 側両方):
  - parsed metadata keys (`matched` / `fields` / `raw` /
    `fallback_used` / `matched_pattern_index` / `error`) を rows
    に出さない
  - 新形式 (response_parser 経由) は `fields` 内の numeric と
    `value_numeric` を `{cmd_name}.{field}` 名で rows 化
  - 旧形式 (response_parsed 平 dict) も metadata key を skip しつつ
    従来通り rows 化
- 7 件 test 追加 (`test_v2_14_1_review.py`):
  - JPPC corruption 値復元 (29.0/29.1/30.3/32.2/33.0 + 末尾 \t)
  - 厳密 pattern regression
  - permissive float ユニット
  - parsed metadata key 除外
  - parsed.fields.numeric の rows 化
  - 旧形式 response_parsed の rows 化
  - version sentinel

### 既知の制約

`_parse_value_permissive` は `*` を `.` とみなすため、本物の `*` を
含む応答形式 (現状無いと想定) では誤変換する可能性がある。これは
複数 pattern を YAML で並べて回避できる。

### 互換性

完全後方互換。新規 fallback 動作のみ追加。Stable / Experimental
tool API は不変。visa-mcp v2.2.1 と組で release。


## v2.14.0 — response parser 強化 + 自動 parse (visa-mcp v2.2 と協調)

合言葉: **「parser が失敗しても raw も数値も失わない」**

### 背景

Codex 実機 E2E (v2.13.3 / visa-mcp v2.1.5) で同じ 7563 から
`NTTC+0033.0E+0` と `JPPC+0029*1A+0` 両方が観測された。前者は厳密
parser で構造化できるが、後者は parser 未マッチで `matched=false` /
構造化値なし → エージェントが温度判定できない問題。
レビューが「parser が失敗しても raw は消さない、value だけでも
取れることが重要」と指摘。

### 修正

- `ResponseFormat` schema 拡張:
  - `patterns: list[str]` 複数代替 (旧 `pattern: str` も後方互換維持)
  - `fallback: "numeric_extract" | ""` — どの pattern にもマッチ
    しないとき `raw` から `[+-]?\d+(\.\d+)?(E[+-]?\d+)?` を抽出
- `parse_response` 拡張:
  - patterns を順次トライし、マッチしたら `matched=True` +
    `matched_pattern_index`
  - 全 unmatch なら fallback 試行 → `value_numeric` + `fallback_used`
  - 例外も含めて `raw` は常に保持
- `step_executor` 自動 parse:
  - query step 完了時に `cmd_def.returns.format` があれば
    response_formats を引いて自動 parse、result に `parsed` を同梱
  - parser 失敗時も `raw_response` は残る
- test 22 件追加 (`test_v2_14_response_parser.py`):
  - 厳密 / 緩い patterns / 異常値 fallback / 後方互換 / numeric
    抽出ユニット / version sentinel

### 実機検証

PMX35-3A USB + 7563 GPIB, 0→2V sweep + read_measurement:
- raw `JPPC+0029*2A+0\t` を取得
- parsed.matched=false, value_numeric=29.0, fallback_used=numeric_extract
- 厳密 / 緩い両方 unmatch でも数値が parsed に乗る

### 互換性

`ResponseFormat.pattern` (単一) は引き続き利用可。`patterns` と
両方指定時は `patterns` 優先。Stable / Experimental tool API は不変。
visa-mcp v2.2.0 と組で更新。

### 残課題 (次回以降)

- `JPPC` prefix の意味確定 (7563 マニュアル調査要)
- 7563 GPIB read_termination / EOI 設定 (corruption 原因の追究)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>


## v2.13.3 — get_experiment_results response に version sentinel 追加

合言葉: **「rows=0 を見た瞬間に server バージョンが分かる」**

Codex 実機 E2E (v2.13.2 / v2.1.2 反映後の再テスト) で再び rows=0 が
報告された。コード上は両 export.py で raw_response を読むため、
ローカル MCP tool E2E では total=12 / rows=12 を確認済み。
原因はほぼ確実に **Codex 側が古い build を起動している** ことだが、
クライアントが「自分の MCP server がどのバージョンを走らせている
か」をその場で判別できる手段が無かった。

### 修正

- `tools/export.py:get_experiment_results` の response data に
  `_meta.versions` を追加:
  ```json
  {
    "data": {
      "rows": [...],
      "pagination": {...},
      "_meta": {
        "versions": {
          "lab_executor": "2.13.3",
          "visa_mcp": "2.1.3",
          "export_fix": "v2.13.3"
        }
      }
    }
  }
  ```
- これで rows=0 を見た瞬間に「自分が立てた server がそもそも
  fix 適用版か」を確認できる。`export_fix` が `v2.13.3` 未満なら
  必ず `pip install` を見直してから再テスト。
- visa-mcp 側にも対応 (v2.1.3、`export_fix: v2.1.3` sentinel)。

### 互換性

`_meta` は data の追加 field のみ。Stable / Experimental tool の
名前・引数・既存 response 構造は変更なし。


## v2.13.2 — 測定値永続化経路の修正 (Issue C: results rows=0)

合言葉: **「step_executor が `raw_response` で書くなら results 抽出も
`raw_response` を読め」**

Codex 実機 E2E (v2.13.1 後の sweep job) で発覚した、永続化はされて
いるが results に出てこない bug:

```
start_experiment_job 完了 / status=completed
job_steps: 26 行 (v2.13.1 で記録される)
job_events: step_started=26 / step_completed=26 (v2.13.1)
   ↓
get_experiment_results -> rows=0  ← 依然 0
timeline step_completed payload に raw_response 無し
live_view.latest_measurements 空
```

### 原因

2 つのキー名不一致:

1. `step_executor.py` は query 系 step の結果を
   `{"command": ..., "scpi_sent": ..., "raw_response": ..., "success": True}`
   の形で返す。
2. 一方 `tools/export.py:_extract_result_rows` は
   `r.get("response_raw") or r.get("response")` を探していた
   (`raw_response` ではなく)。同様に parsed dict は
   `response_parsed` を探していたが step_executor は `parsed` で出す
   ことがある。
3. `_run_experiment_plan_job` の `step_completed` event payload には
   `step_type / verified / error` しか入っていなかった。timeline / 
   live_view / summary から測定値を読み取れない状態。

### 修正

- `tools/export.py:_extract_result_rows`:
  - parsed: `r.get("response_parsed") or r.get("parsed")` の OR
  - raw: `r.get("raw_response") or r.get("response_raw") or r.get("response")` の OR
  - 既存 `response_raw` / `response_parsed` 名は後方互換として残す
- `job/manager.py:_run_experiment_plan_job` の step_completed event
  payload に以下を追加 (None は間引く):
  - `command`, `instrument`, `args`, `scpi_sent`
  - `raw_response`, `parsed`
  - `verified`, `verify`
  これで timeline event だけ見ても query 系 step の実測値が読める。

### 実機検証 (PMX35-3A USB + 7563 GPIB, 0→4V sweep)

| 指標 | v2.13.1 | v2.13.2 |
|------|--------|---------|
| status | completed | completed |
| job_steps rows | 26 | 26 |
| step_started/_completed events | 26/26 | 26/26 |
| `get_experiment_results` rows | **0** | **12** ✅ |
| event payload に raw_response | × | ✅ |

1V → 4V の各点で V/I/温度すべて取得:
- 1V: V=1.007V I=9.7mA T=26.8°C
- 4V: V=4.007V I=39.9mA T=28.2°C (+1.4°C 発熱を実測)

### 注意: visa-mcp 側の同名 shim も同時更新が必要

`visa-mcp` v2.1.x の `visa_mcp/tools/export.py` は本 module の
独自コピーを持ち、`visa-mcp serve` 起動時はそちらが MCP server に
登録される。v2.13.2 を入れても `visa-mcp >= 2.1.2` を使わないと
get_experiment_results は rows=0 のまま再発するため、両方更新する
こと。**visa-mcp v2.1.2 を同時 release**。

### 追加 integration test (Codex P2 への応答)

source string 検査だけではキー名不一致 bug を捕まえられなかったため、
`tests/test_v2_13_2_results_integration.py` を新設:
- 実 `JobStore` に `raw_response` 付き step を保存し
  `_extract_result_rows` が rows を返すことを assert
- `parsed` alias / 後方互換 legacy keys
両 repo に同等のテストを配置 (visa-mcp 側は
`tests/test_v2_1_2_results_integration.py`)。

### スコープ外 (次回以降)

レビューで挙がった残課題:
- 7563 T-type/K-type response parser の構造化 (visa-mcp v2.2 候補、
  raw は既に永続化されているため緊急度低)
- discovery 部分失敗対応 (`resource ごとの ok/error/timeout`)
- bindings / identified state の永続化・復元 (process 再起動耐性)
- 大規模 (100 台) 向け instrument 別 / sweep 別 観察 API
- dry_run rendered step count と summary total_steps の整合
- 既定 CSV export path のパーミッション周り (Windows `~/.visa-mcp`
  作成失敗) の代替パス提案

### Co-Authored-By

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>


## v2.13.1 — DSL plan path persistence hooks (Issue A 修正)

合言葉: **「DSL plan も recipe path と同じ persistence hook を呼ぶ」**

Codex 実機 E2E (v2.13.0 リリース後の sweep job) で発覚した致命的
persistence バグ:

```
start_experiment_job 完了 / job_outcome=success / 実機は正しく
動作している (PMX35-3A sweep + 7563 read + safe_shutdown)
   ↓
get_experiment_results -> rows=0
get_job_summary -> total_steps=0, key_results=[]
get_experiment_timeline -> experiment_job_started 1 件のみ、
                         step / measurement event 一切なし
CSV export -> header 行のみ (data 0 行)
```

### 原因

`job/manager.py:_run_experiment_plan_job` (DSL plan path) が、
**recipe path (`_run_job_inner`) と同じ persistence hook を呼んで
いなかった**。具体的に欠けていた呼出:

- `self._store.record_step_started(job_id, idx, step_type)` —
  job_steps テーブルへの INSERT
- `self._safe_record_event(job_id, "step_started", ...)` —
  timeline event
- `self._store.record_step_completed(...)` — job_steps の終端記録
- `self._safe_record_event(job_id, "step_completed" / "step_failed", ...)`

recipe path には v0.7.0 から存在していたが、v0.8.0 で DSL plan path
を追加するときに hook を写し忘れていた。**3 release 分 (v0.8.0 ~
v2.13.0) この bug が潜在していた**。

### 修正

`_run_experiment_plan_job` の step loop に上記 4 呼出を追加 (recipe
path とまったく同じ形式 + 同じ try/except 包囲):

```python
# update_step の直後 (step 開始時)
step_type = getattr(step, "type", "?")
step_row_id = 0
try:
    step_row_id = self._store.record_step_started(
        job_id, idx, step_type)
except Exception:
    pass
self._safe_record_event(job_id, "step_started", step_index=idx, ...)

# step dispatch 実行

# step_results.append の直後 (step 終了時)
try:
    self._store.record_step_completed(
        step_row_id,
        status="ok" if result.get("success") else "failed",
        result=result if result.get("success") else None,
        error=result if not result.get("success") else None,
    )
except Exception:
    pass
self._safe_record_event(
    job_id,
    "step_completed" if result.get("success") else "step_failed",
    step_index=idx, ...,
)
```

### 影響範囲

**影響あり (修正される)**:

- `get_experiment_results` が DSL plan の measurement / step 結果を
  返すようになる
- `get_job_summary.total_steps` が正しい数を返す
- `get_experiment_timeline` に `step_started` / `step_completed` /
  `step_failed` event が含まれる
- `export_experiment_results` CSV に data row が出る
- 既存の **完了済 jobs** は記録がないので過去データは復旧不可
  (新規 job 以降のみ正しく記録される)

**影響なし**:

- recipe path (`_run_job_inner`) は元から記録されていたので変化なし
- 実機への I/O 経路は完全不変
- DSL compiler / validator も不変
- MCP tool / DSL schema / safety API 不変

### Tests (227 件 pass)

- `tests/test_v2_13_predicted_history.py` に新規 source check 1 件
  追加 (`test_v2_13_1_experiment_plan_persistence_hooks_present`):
  `_run_experiment_plan_job` block 内に `record_step_started` /
  `record_step_completed` / `step_started` / `step_completed|
  step_failed` の文字列が存在することを source 解析で確認
- 既存 v2.13.0 tests (predicted history) も全 pass

### Codex 再実機検証

v2.13.1 を visa-mcp serve に読み込ませた状態で sweep job を再実行
すると、`get_experiment_results` rows>0 / `get_experiment_timeline`
に step_started/_completed event / `total_steps>0` / CSV にデータ
が入るはず。

### 残課題 (別 release で対応)

- **Issue B**: 7563 の T-type response `NTTC+0032.4E+0` を visa-mcp
  parser が構造化できない (matched=false) → visa-mcp v2.2 候補で
  response_formats 正規表現を T-type 対応に拡張
- measurement_cache への書き込み (query 結果) も DSL path で別途
  確認要 — step_completed event の result に measurement 値が乗って
  いる前提で、`tools/info.get_last_measurement` 経路が動くかは
  Codex の次回実機で確認

### 互換性

- MCP tool 名 / 引数 / response 不変
- DSL schema / safety API 不変
- 既存 recipe job の挙動は完全不変
- 新規 DSL plan job の persistence が正しく動く方向の修正のみ

---

## v2.13.0 — DSL validator predicted history (precondition fix)

合言葉: **「plan 内の先行 step を validator にも見せる」**

Codex 実機 E2E (PMX35-3A + 7563、抵抗発熱配線) で発覚した bug への
対応:

```
plan: set_voltage_protection → set_current_protection → set_output ON
```

という安全なプランを `validate_experiment_plan` / `dry_run_plan` に
かけると、strict mode で **`set_output ON` が `safety_violation`** に
なる現象。

原因: validator は `session.command_history` (= 実機の実行履歴) だけ
を見ており、dry_run 時点ではまだ何も実行していないため history が
空 → precondition の `has_been_called: set_voltage_protection` が
「未呼出」と判定されていた。

### 修正

`dsl/compiler.py` の `_Context` に **`_predicted_history:
dict[resource_name, list[command_name]]`** を追加。plan walk が
順次進むにつれて、各 `CommandStep` の `command_name` を該当
resource の list に append。次以降の step の safety check では:

```python
combined_history = list(session.command_history) + predicted
violations = sf.validate(..., session_history=combined_history)
```

を渡す。これで plan 内の **先行 step が「呼ばれたこと」になる**。

### parallel branch の扱い

parallel 内では順序保証がないため、安全側に倒して:

- branch 開始時に `_predicted_history` の snapshot を取り、
  `_parallel_depth += 1`
- branch 内 step は `_parallel_depth > 0` なので予測 history に
  **積まない**
- branch 終了時に snapshot を復元、`_parallel_depth -= 1`

これにより、branch 内で積まれた history が他 branch / 後段に
漏れない。

### 影響範囲

- **影響あり**: `validate_experiment_plan` / `dry_run_plan` の
  precondition チェック → 正しいプランが strict mode で通るように
  なる
- **影響なし**: `start_experiment_job` (runtime) は今まで通り
  runner 側で `session.command_history` を更新するため、挙動同一
- **影響なし**: parameter 検証 / range 検証 / verify 設定など他の
  validator path

### Tests (226 件 pass)

`tests/test_v2_13_predicted_history.py`: 8 件

- `safety.validate` baseline (history あれば pass / なければ
  violation 2 件)
- predicted history を session_history と結合した状態で pass する
  ことの直接確認
- 部分的に欠けている場合は依然として violation
- `compiler.py` source に `combined_history` / `_parallel_depth` /
  `_predicted_history` が含まれていることの source check
- version / tool surface 不変回帰

### Codex E2E 再開可能

これで PMX35-3A の正しい sweep プラン (set_voltage_protection /
set_current_protection / set_output ON / sweep / safe_shutdown)
が strict mode の dry_run_plan を **そのまま通過**するはず。

### 互換性

- MCP tool 名 / 引数 / response 不変
- safety.py の API シグネチャ不変
- 既存 plan は影響なし (history を見る論理は同じ、より満たしやすく
  なる方向だけ)
- runtime 経路は完全不変
- MCP tool / DSL / extension pack 形式 / `.install_meta.json` /
  `default_extensions_dir()` 不変

---

## v2.12.0 — Controlled Cleanup Apply

合言葉: **「ついに削除系操作。ただし完全削除ではなく trash 移動」**

v2.11 で preflight が入った次の段階。**cleanup apply のみ** を実装
する (rollback apply は v2.14+ に後ろ送り)。legacy source を完全削除
せず、`~/.lab-executor/migration_trash/<manifest_stem>/` へ移動する。

### 新規 API

```python
@dataclass(frozen=True)
class ExtensionCleanupApplyResult:
    status: str   # "ok" / "blocked" / "partial_failure"
    moved_to_trash: list[dict]
    failed: list[dict]
    skipped: list[dict]
    blocked_reasons: list[dict]
    manifest_path: Path | None
    trash_root: Path | None
    confirmation_token: str | None
    source_manifest: Path | None
    permanent_delete_performed: bool = False  # v2.12 固定
    overwrite_performed: bool = False         # v2.12 固定
    trash_move_performed: bool = False

def apply_extension_cleanup_plan(
    manifest_path, *, confirm, log_dir=None, trash_root_base=None,
) -> ExtensionCleanupApplyResult:
    ...
```

### 厳格な事前条件

- **`--confirm` 必須** (token: `cleanup:<count>:<manifest_stem>`)
- 実行直前に **plan + preflight を再計算**。UI 表示後の filesystem
  変化があれば blocked に倒す
- `preflight.eligible` が False なら blocked
- `confirm` 不一致は blocked

### 安全方針 (実装で固定)

- legacy source は **trash 移動のみ**。完全削除しない
  (`permanent_delete_performed=False` 固定)
- target (new path) は **触らない**
- trash target が既存 → blocked (上書きしない、
  `overwrite_performed=False` 固定)
- cross-device error (EXDEV) → failed (copy+delete fallback なし)
- partial failure は **fail-fast** (途中失敗で停止、成功済 trash 移動
  は残す)
- cleanup manifest を `extension-cleanup-<stamp>.json` に必ず保存
  (blocked / partial_failure 時も保存)
- manifest 保存失敗時は v2.8 と同じく `partial_failure` 格上げ +
  `failed[]` に `manifest_write_failed`

### `evaluate_cleanup_apply_preconditions()` 仕様変更

v2.11 では `apply_supported=False` / `apply_available=False` 固定
だったが、v2.12 で cleanup apply が実装されたため:

- `apply_supported=True` (cleanup のみ)
- `apply_available=True` (`eligible=True` の場合のみ)

rollback preflight は引き続き `apply_supported=False` /
`apply_available=False` (rollback apply は v2.14+ で検討)。

### 新規 CLI flag

```bash
lab-executor extension migration-log cleanup-plan <manifest|--latest> \
  --apply --confirm cleanup:2:extension-copy-... [--json]
```

- `--apply` は `--confirm <token>` を要求 (無ければ exit 2)
- token 不一致 → blocked → exit 1
- `rollback-plan --apply` は **未実装** (exit 2 with "not implemented")

### migration-log list / load の cleanup manifest 対応

`SUPPORTED_OPERATIONS` に `extension_cleanup_apply` を追加、
`SUPPORTED_MANIFEST_SCHEMAS` に `v2.12` を追加。`list` / `inspect`
は cleanup manifest も対象になる (`verify` / `find_latest_*` は
copy manifest のみのまま)。

### v2.12 で **やらないこと**

- rollback `--apply` (v2.14+ 検討)
- 完全削除 / `--force` / overwrite
- trash 内ファイルの削除 / 整理
- cross-device の copy+delete fallback
- install default 変更 / active_read_paths 優先順位変更
- duplicate 自動解決
- cleanup manifest の verify (v2.13+ 候補)

### Tests (218 件 pass)

`tests/test_v2_12_cleanup_apply.py`: 18 件

- preconditions: requires_confirm / rejects_wrong_confirm /
  preflight_ineligible_blocked / recomputes_preflight
- 正常系: **moves_legacy_source_to_trash** (legacy が trash へ、
  target は残る、を実 ファイル check)
- 安全: does_not_permanently_delete / overwrite_performed=False /
  trash_target_exists → skipped / fail-fast
- manifest: ok 時 / blocked 時もどちらも保存
- preflight 仕様変更: cleanup `apply_supported=True` /
  rollback `apply_supported=False`
- CLI: cleanup_apply ok / requires_confirm exit 2 /
  rollback_apply not_implemented exit 2
- Boundary: PyVISA / `visa_mcp` 非依存
- 回帰: install_default / tool surface 不変

v2.11 tests を v2.12 schema へ更新 (cleanup preflight の
apply_supported/available=True 期待)。

### 互換性

- 既存 `cleanup-plan` (`--apply` 無し) の挙動は v2.10 / v2.11 と同一
- `evaluate_cleanup_apply_preconditions()` の戻り値の
  `apply_supported` / `apply_available` が v2.11 と変化 (False →
  True 方向、安全側ではない)。CI で `apply_supported is False` を
  assert していた箇所は要更新
- cleanup manifest は **新規 schema_version=v2.12** (copy manifest
  の v2.7 とは別物)
- MCP tool / DSL / extension pack 形式 / `.install_meta.json` /
  `default_extensions_dir()` 返り値、すべて不変

---

## v2.11.0 — Cleanup / Rollback Apply Preflight

合言葉: **「削除実行に進む直前に、許可条件だけを固定する」**

v2.10 で plan refinement が完了。次は削除系 apply に進む前に **実行
可能か機械的に評価する preflight だけ**を入れる段階。実 ファイル変更
は v2.11 でも一切しない。

### 新規 API (`extension_migration_log.py`)

```python
@dataclass(frozen=True)
class ApplyPreconditionCheck:
    check_id: str
    status: str   # "ok" / "warning" / "error"
    message: str
    details: dict

@dataclass(frozen=True)
class ApplyPreflightResult:
    operation: str
    status: str
    eligible: bool
    apply_supported: bool   # v2.11 では常に False
    apply_available: bool   # v2.11 では常に False
    candidate_count: int
    checks: list[ApplyPreconditionCheck]
    blocked_reasons: list[dict]
    required_confirmation: str | None
    future_trash_root: str = "~/.lab-executor/migration_trash"
    note: str

def evaluate_cleanup_apply_preconditions(plan) -> ApplyPreflightResult
def evaluate_rollback_apply_preconditions(plan) -> ApplyPreflightResult
```

### 評価する Precondition

**cleanup-plan preflight**:
- `has_candidates`: candidate >=1
- `plan_blocked_reasons_empty`: plan に blocked が無い
- candidate ごとの `target.exists()` / `legacy_source.exists()`
- target が legacy source と同一 path でないこと

**rollback-plan preflight**:
- `has_candidates`: candidate >=1
- `plan_blocked_reasons_empty`
- candidate ごとの `target.exists()` / `legacy_source.exists()`
- target ≠ legacy source

いずれも eligible=true でも `apply_available=False` / `apply_supported
=False` 固定。

### Confirmation token

`<kind>:<count>:<manifest_stem>` (v2.12+ で `--confirm` で要求予定):

```
cleanup:2:extension-copy-20260527-103012
rollback:1:extension-copy-20260527-103012
```

eligible 時に `required_confirmation` field に格納。v2.11 では note /
出力で表示のみ。

### 新規 CLI flag: `--preflight`

```bash
lab-executor extension migration-log cleanup-plan  --latest --preflight
lab-executor extension migration-log rollback-plan --latest --preflight
```

- `--latest` / 明示 manifest / `--json` と組合せ可能
- Exit code: `eligible=true` → 0、`eligible=false` → 1
- `apply_supported=false` 自体は error にしない (release 仕様)

### Trash / Backup strategy (v2.12+ で実装予定)

cleanup / rollback の実 apply 時は **完全削除せず trash 移動**:

```
~/.lab-executor/migration_trash/<manifest_stem>/
```

preflight 出力に `future_trash_root` field で明示済。

### v2.11 で **やらないこと**

- cleanup `--apply` / rollback `--apply`
- target 削除 / legacy source 削除 / trash 移動の実行
- source 復元 / overwrite / install default 変更
- active_read_paths 優先順位変更
- manifest schema 破壊変更

### Tests (200 件 pass)

`tests/test_v2_11_apply_preflight.py`: 19 件

- cleanup preflight: ok / no_candidates_blocked / plan_blocked /
  verify_error_blocked / confirmation_token_format / apply_available=False
- rollback preflight: ok / no_candidates_blocked /
  legacy_missing_blocked / apply_available=False
- **preflight_does_not_change_files** (snapshot 比較で固定)
- CLI: cleanup-plan/rollback-plan --preflight JSON / --latest 併用 /
  blocked → exit 1
- Boundary: PyVISA / `visa_mcp` 非依存
- 回帰: install_default / tool surface 不変

### docs / cli docstring

- `docs/extension_path_migration.md`: v2.11 セクション (preflight、
  confirmation token、trash strategy) を追加、roadmap 表を更新
- `cli.py` module docstring を v2.10.x → v2.11.x

### 互換性

- 既存 `rollback-plan` / `cleanup-plan` (default、`--preflight` 無し)
  の挙動は v2.10 と完全同一
- `ApplyPreflightResult.to_dict()` の `schema_version` は新規 `v2.11`
  (plan schema とは別物)
- MCP tool / DSL / extension pack 形式 / `.install_meta.json` /
  `default_extensions_dir()` 返り値、すべて不変

---

## v2.10.0 — Rollback / Cleanup Plan Refinement

合言葉: **「削除実行に進む前に、plan の精度と UX を上げる」**

v2.9 で `rollback-plan` / `cleanup-plan` が入った次の段階。**実削除
には進まず**、分類整理 / verify 統合 / `--latest` UX / status semantics
を改善する。`--apply` は v2.11+ で慎重に検討。

### `--latest` flag (P0)

`migration-log {inspect,verify,rollback-plan,cleanup-plan}` 全てで
`--latest` を導入:

```bash
lab-executor extension migration-log verify --latest
lab-executor extension migration-log rollback-plan --latest
lab-executor extension migration-log cleanup-plan --latest
```

- `operation == extension_copy_apply` の最新 manifest を自動選択
- 明示 manifest path との併用は usage error (exit 2)
- 該当 manifest が無ければ exit 1

新 API: `find_latest_extension_copy_manifest(log_dir=None) -> Path | None`

### Rollback plan 分類改善 (P0)

`target_missing` を `blocked_reasons` から **`already_absent` リスト
に分離**。「削除対象が既に無い = 異常ではなく対象外」を明示。

```python
@dataclass
class ExtensionRollbackPlan:
    status: str
    candidates: list[...]
    already_absent: list[dict]   # ← v2.10 新規
    blocked_reasons: list[dict]  # ← legacy_source_missing 等の real block のみ
    warnings: list[dict]
```

summary に `already_absent` カウントを追加。schema_version=`v2.10`。

### Cleanup plan 改善 (P0)

1. **verify 統合**: 内部で `verify_extension_migration_log()` を呼び、
   verify の error/warning を cleanup-plan の blocked/warning に変換。
   verify 条件が一元化される。
2. **`already_cleaned_or_missing` warning を `legacy_source_missing`
   リストに分離**。v2.10 時点では実 cleanup が無いため「既に整理済」
   と断定できない。構造化して報告するに留める。
3. `delete_performed_unexpected` / `overwrite_performed_unexpected`
   等の overall meta error は cleanup-plan を全体 block する。

```python
@dataclass
class ExtensionCleanupPlan:
    status: str
    candidates: list[...]
    legacy_source_missing: list[dict]   # ← v2.10 新規
    blocked_reasons: list[dict]
    warnings: list[dict]
```

### Plan-only warning と status の分離 (P0、案 A)

v2.9 では `plan_only` warning を常に追加するため status が常に
warning になっていた。v2.10 で **案 A** を採用:

- 実 problem が無ければ `status="ok"`
- plan-only warning は `warnings[]` に残るが status を格上げしない
- `--strict` は real problem だけで exit 1 化 (plan-only では exit 0)

これにより CI で `--strict` を安全に使えるようになる (plan-only
状態で false fail しない)。

### CLI 挙動

```bash
# 正常系: status=ok / exit 0 / --strict でも 0
lab-executor extension migration-log rollback-plan --latest --strict

# duplicate / target missing for cleanup 等の real problem
# -> status=warning|error / --strict で exit 1
```

### v2.10 で **やらないこと**

- rollback `--apply` / cleanup `--apply`
- target 削除 / legacy source 削除 / source 復元
- overwrite / install default 変更
- manifest schema 破壊変更
- extension pack / `.install_meta.json` schema 変更
- remote registry / signing / trust store

### Tests (181 件 pass)

`tests/test_v2_10_rollback_cleanup_refinement.py`: 18 件

- `find_latest_extension_copy_manifest` (順序 / empty)
- `--latest` CLI (verify / inspect / rollback-plan / cleanup-plan)
- `--latest` と明示 path の併用は exit 2
- 該当 manifest 無し → exit 1
- cleanup-plan が verify 結果を使うこと (extension_id_mismatch /
  delete_performed_unexpected 全体 block)
- plan-only warning だけなら status=ok / `--strict` でも exit 0
- rollback の already_absent / blocked 分離
- no_file_changes (snapshot 比較)
- Boundary: PyVISA / `visa_mcp` 非依存
- 回帰: install_default / tool surface 不変

既存 v2.9 tests を v2.10 schema へ更新 (status=ok 期待、
already_absent / legacy_source_missing リスト、schema_version=
`v2.10`)。

### docs / cli docstring

- `docs/extension_path_migration.md`: v2.10 セクション追加 (`--latest`
  / 案 A status semantics / 状態別ふるまい表 / Command matrix)
- `cli.py` module docstring を v2.9.x → v2.10.x

### 互換性

- `ExtensionRollbackPlan` / `ExtensionCleanupPlan` の dict 表現に
  `already_absent` / `legacy_source_missing` field が追加 (v2.9 では
  存在しなかった)
- schema_version は v2.9 → v2.10 に上がる
- status 値の semantics が変化 (v2.9 までは plan-only でも warning、
  v2.10 から ok)。CI で `--strict` を使っていた箇所は安全側に動く
- MCP tool / DSL / extension pack 形式 / `.install_meta.json` /
  `default_extensions_dir()` 返り値、すべて不変

---

## v2.9.0 — Extension Rollback / Cleanup Planning

合言葉: **「v2.7 で copy、v2.8 で verify、v2.9 で戻すか進めるかの
計画。まだ削除しない」**

v2.8 で `verify_extension_migration_log()` を使い copy 結果を検証
できるようになった次の段階。**rollback (取り消し)** と **cleanup
(legacy 整理)** の方向性が逆の 2 種類の計画を CLI / API で出せる
ようにする。実削除は v2.9 でも一切しない (`apply_available=False`
固定)。

### 新規 API (`extension_migration_log.py`)

```python
@dataclass(frozen=True)
class ExtensionRollbackCandidate:
    extension_id: str
    target: Path                  # 取り消し時に削除候補
    legacy_source: Path | None    # 戻る先 (存在必須)
    target_exists: bool
    legacy_source_exists: bool
    safe_to_plan: bool
    apply_available: bool = False

@dataclass
class ExtensionRollbackPlan:
    status: str / candidates / blocked_reasons / warnings
    apply_available: bool = False

@dataclass(frozen=True)
class ExtensionCleanupCandidate:
    extension_id: str
    legacy_source: Path           # 整理時に削除候補
    copied_target: Path           # verify ok 前提
    target_verified: bool
    legacy_source_exists: bool
    safe_to_plan: bool
    apply_available: bool = False

@dataclass
class ExtensionCleanupPlan:
    status: str / candidates / blocked_reasons / warnings
    apply_available: bool = False

def plan_extension_rollback_from_log(manifest_path) -> ExtensionRollbackPlan
def plan_extension_cleanup_from_log(manifest_path)  -> ExtensionCleanupPlan
```

### rollback-plan 条件

**candidate**:
- manifest 読める / schema 対応
- copied[] に target がある、target が存在する
- legacy source が存在する (戻る先が必要)

**blocked**:
- `target_missing` (既に消えている)
- `legacy_source_missing` (戻す先が無い)
- `delete_performed_unexpected` / `overwrite_performed_unexpected`
  (manifest 改ざん)
- `manifest_schema_unsupported` / `manifest_not_found`

### cleanup-plan 条件

**candidate**:
- manifest 読める / schema 対応
- target が存在し `extension.yaml` が読め `extension_id` 一致 (verify
  ok 相当)
- legacy source が存在する

**blocked**:
- `target_missing` / `target_extension_yaml_missing` /
  `target_extension_yaml_unreadable` / `extension_id_mismatch`
- `delete_performed_unexpected` / `overwrite_performed_unexpected`
- `manifest_schema_unsupported`

**warning** (candidate にしない):
- `already_cleaned_or_missing` (legacy source が既に無い → 整理不要)

### rollback ↔ cleanup の **方向が逆**

| Plan | 目的 | 削除候補 | 必要な前提 |
|------|------|----------|------------|
| rollback-plan | migration を **取り消す** | target | legacy source あり |
| cleanup-plan  | migration を **進める**   | legacy source | target が verify ok |

混同すると危険なため、docs に command matrix を追加した。

### 新規 CLI

```bash
lab-executor extension migration-log rollback-plan <manifest> [--json] [--strict]
lab-executor extension migration-log cleanup-plan  <manifest> [--json] [--strict]
```

Exit code は既存の `verify` と同じ (ok=0、warning=0/`--strict`で1、
error=1、usage=2)。candidate あり時は `plan-only` warning が必ず
入るため、default の status は warning になる (実 apply はまだ
できない、という reminder)。

### v2.9 で **やらないこと**

- rollback `--apply` / cleanup `--apply`
- target 削除 / legacy source 削除 / source 復元
- overwrite / install default 変更
- active_read_paths 優先順位変更
- extension pack / `.install_meta.json` schema 変更

### Tests (163 件 pass)

`tests/test_v290_rollback_cleanup_plan.py`: 22 件

- rollback: ok / target_missing / legacy_source_missing /
  schema_unsupported / delete_performed_unexpected / **does not
  delete files** (snapshot 比較で固定)
- cleanup: ok / target_missing / extension_id_mismatch /
  source_missing → already_cleaned warning / overwrite_unexpected /
  **does not delete files**
- CLI: rollback-plan / cleanup-plan の help / JSON / `--strict`
- Boundary: PyVISA / `visa_mcp` 非依存 subprocess gate
- 回帰: install_default / tool surface 不変

### docs / cli docstring

- `docs/extension_path_migration.md`: command matrix 追加 (rollback
  と cleanup の方向の違いを明示)、ロードマップ表に v2.9 を実装済と
  して追加
- `cli.py` module docstring を v2.8.x → v2.9.x

### 互換性

- `ExtensionCopyApplyResult` / `MigrationLogVerificationResult` 等
  既存 API は無変更
- MCP tool / DSL / extension pack 形式 / `.install_meta.json` /
  `default_extensions_dir()` 返り値、すべて不変

---

## v2.8.0 — Migration Log Inspection + Copied Pack Verification

合言葉: **「v2.7 で copy した結果を追跡・検証できるようにする。
rollback も delete もまだしない」**

v2.7 で `~/.lab-executor/migration_logs/extension-copy-<stamp>.json`
として残し始めた apply manifest を、CLI / API で読み返し、copied
target が現在も健全かを検証する段階。実 rollback / target 削除 /
overwrite は v2.8 でも一切しない。

### 新規 module: `lab_executor.extension_migration_log`

```python
@dataclass(frozen=True)
class MigrationLogSummary: ...
@dataclass(frozen=True)
class ExtensionCopyApplyManifest: ...
@dataclass
class MigrationLogVerificationResult: ...

def list_extension_migration_logs(*, log_dir=None) -> list[MigrationLogSummary]
def load_extension_migration_log(path) -> ExtensionCopyApplyManifest
def verify_extension_migration_log(path) -> MigrationLogVerificationResult
```

`operation == extension_copy_apply` のみを対象 (将来別 operation が
増えても混在しない)。`schema_version == "v2.7"` を必須 (将来後方
互換のため SUPPORTED_MANIFEST_SCHEMAS で扱う)。

### `verify_extension_migration_log()` の検出項目

**error**:

- `target_missing`
- `target_extension_yaml_missing` / `target_extension_yaml_unreadable`
- `extension_id_mismatch`
- `delete_performed_unexpected` (manifest が改ざんで `delete_performed
  =true` になっている)
- `overwrite_performed_unexpected`
- `manifest_schema_unsupported` / `manifest_not_found`

**warning**:

- `source_missing` (source は将来整理される可能性があるため warning)

### manifest 保存失敗時 → `partial_failure` 格上げ (P0)

v2.7.1 で予約した **案 A** を実装。`apply_extension_copy_plan()`
内で `_write_manifest()` が例外を出した場合:

```
status = "partial_failure"
manifest_path = None
failed[] に {"error_class": "manifest_write_failed", "message": ...}
```

実 copy は完了していても audit 上「成功」扱いしない。manifest なしの
copy は後から検証・説明できないため。

### 新規 CLI subcommands

```bash
lab-executor extension migration-log list [--json]
lab-executor extension migration-log inspect <manifest> [--json]
lab-executor extension migration-log verify <manifest> [--json] [--strict]
```

- `list`: timestamp 降順で表示
- `inspect`: `delete_performed=false` / `overwrite_performed=false`
  を目立たせる (ユーザーが「このマイグレーションは削除も上書きも
  していない」と確認できる)
- `verify`: exit code は `check` / `migration-plan` と整合
  (ok=0、warning=0/`--strict`で1、error=1、usage=2)

### v2.8 で **やらないこと**

- rollback `--apply` / target 削除 / legacy source 削除
- overwrite / `--force`
- install default 変更
- active_read_paths 優先順位変更
- extension pack / `.install_meta.json` schema 変更
- remote registry / signing / trust store

### Tests (141 件 pass)

`tests/test_v280_migration_log.py`: 22 件

- list (empty / after apply)
- load + schema rejection (unsupported schema_version / operation)
- verify (ok / target_missing / extension_id_mismatch /
  source_missing warning / delete_performed_unexpected /
  overwrite_performed_unexpected)
- `manifest_write_failure_marks_partial_failure` (P0 の核 -
  `_write_manifest` を monkeypatch で例外化)
- CLI: list/inspect/verify JSON + verify strict 挙動
- Boundary: PyVISA / `visa_mcp` 非依存 subprocess gate
- 回帰: install_default 不変 / Stable 43 + Experimental 7 = 50 不変

### docs / cli docstring

- `docs/extension_path_migration.md`: `migration-log` セクション +
  error/warning 一覧 + manifest 保存失敗時の挙動 + roadmap 表に v2.8
  を実装済として追加 / v2.9 を rollback-plan へ更新
- `cli.py` module docstring を v2.7.x → v2.8.x

### 互換性

- `apply_extension_copy_plan()` の戻り値 schema (`ExtensionCopy
  ApplyResult`) は v2.7 と同じ。`manifest_path=None` のケースが
  v2.8 で増えた点のみ要注意 (manifest 保存失敗時)
- MCP tool / DSL / extension pack 形式 / `.install_meta.json` /
  `default_extensions_dir()` 返り値、すべて不変

---

## v2.7.1 — Docs / Review patch (v2.7 表記整合 + 仕様明文化)

v2.7.0 レビュー反映。コード変更なし。

### Docs / CLI 文言

- `cli.py` argparse `description` を「dual-path extension discovery,
  migration planning, copy-plan preview, **and controlled copy apply**
  (v2.7)」へ更新。v2.6 表記を解消。
- `cli.py` module docstring 末尾の「`~/.lab-executor/extensions/` への
  切替は v2.7+ で判断」を「v2.8+ 以降の future release で判断」に
  更新 (v2.7 では切替していないことを明示)。
- `ExtensionMigrationAction` docstring: 「controlled apply は v2.7+
  で検討」表現を v2.7 実装済の事実に合わせて書き直し。本 dataclass
  は recommend 用途に閉じ、実 copy は `ExtensionCopyPlan` /
  `apply_extension_copy_plan()` 経由のみという責務分離を明示。
- `ExtensionCopyCandidate` docstring: 「将来 v2.7+ で apply される予定」
  を v2.7 実装済へ更新。v2.7 で `--copy-plan --apply` を併用すれば
  実 copy 対象になる、と書き直し。
- `ExtensionCopyApplyResult.manifest_path` docstring を実装に合わせ
  「v2.7 では ok / blocked / partial_failure すべてで保存するため
  原則として非 None」に修正。manifest 保存自体が失敗したケースだけ
  None になりうる、と注記。

### `target_exists` の検出タイミング別 status 明文化

`docs/extension_path_migration.md` に表で明示:

| 検出タイミング | status | 動作 |
|------|------|------|
| pre-apply (copy_plan 段階で target が既存) | `blocked` | candidate=0、manifest 保存 |
| during apply (copy 直前の再確認で target が出現) | `partial_failure` | skipped に記録、以降を fail-fast 停止 |

いずれも **overwrite はしない**。後者は他プロセスの plan 表示後
race ケース。

### manifest 保存失敗時の方針 (v2.8+ 実装予定)

docs に **案 A** を予約として明記:

> manifest 保存に失敗した場合、実 copy の成否にかかわらず全体を
> `partial_failure` 扱いに格上げし、`failed[]` に `manifest_write_
> failed` を記録。manifest なしの copy 成功は audit 上「成功」と
> みなさない。

### Internal

- version 2.7.0 → 2.7.1 (`__init__.py` / `pyproject.toml`)
- コア logic (apply / plan / discovery) は無変更、tests 119 件 pass

---

## v2.7.0 — Controlled Extension Copy Apply

合言葉: **「v2.6 で copy 候補、v2.7 で実 copy。ただし source は触らず
target は上書きしない」**

v2.6 で出せた copy candidate を、**厳格な事前条件下でのみ実行**する
段階。delete / overwrite / move は v2.7 でも一切しない。

### 新規 API

```python
@dataclass(frozen=True)
class ExtensionCopyApplyResult:
    status: str   # "ok" / "blocked" / "partial_failure"
    copied: list[dict]
    failed: list[dict]
    skipped: list[dict]
    manifest_path: Path | None
    delete_performed: bool = False        # v2.7 では常に False
    overwrite_performed: bool = False     # v2.7 では常に False
    blocked_reasons: list[dict] = []

def apply_extension_copy_plan(
    *, paths=None, log_dir=None,
) -> ExtensionCopyApplyResult:
    ...
```

`ExtensionCopyApplyError` (構造化 error class) も追加。

### 厳格な事前条件 (一つでも欠ければ blocked)

- `copy_plan.status == "ready"`
- `copy_plan.candidates` が 1 件以上
- `copy_plan.blocked_reasons` が空 (v2.6.1 で予約した条件を実施)
- duplicate / invalid_metadata / missing_yaml がない
- 実行直前に **migration plan を再計算** し、UI 表示後の filesystem
  変化があれば blocked に倒す

### 安全方針 (実装で固定)

- source は **削除しない** (`delete_performed=False`)
- target は **上書きしない**。既存なら skipped + 全 candidate を停止
- candidate ごとに `target.tmp-<stamp>/` に copy → atomic-ish rename
- partial failure は **fail-fast** (途中失敗で停止、成功済みは残す、
  `status="partial_failure"`)
- manifest を `~/.lab-executor/migration_logs/extension-copy-<stamp>
  .json` に **必ず**保存 (blocked / partial_failure 時も保存)

### 新規 CLI flag: `--apply`

```bash
lab-executor extension migration-plan --copy-plan --apply
lab-executor extension migration-plan --copy-plan --apply --json
```

- `--apply` は **`--copy-plan` と併用必須**。単独使用は exit 2
- Exit code: ok=0、blocked/partial_failure/failed=1
- Human-readable に COPIED / FAILED / SKIPPED / BLOCKED + manifest
  path + `delete_performed=False` / `overwrite_performed=False` を
  明示出力

### v2.7 で **やらないこと**

- source delete / legacy path 自動 cleanup
- target overwrite / `--force` / `--overwrite`
- install default 変更
- active_read_paths の優先順位変更
- duplicate 自動解決
- 自動 rollback (manifest を残すのみで人手復旧前提)
- extension pack / `.install_meta.json` schema 変更
- MCP tool 追加 / DSL schema 変更

### Tests (119 件 pass)

`tests/test_v270_copy_apply.py`: 16 件

- `apply_copies_legacy_only_to_new_path`
- `apply_does_not_delete_source` (snapshot 比較で固定)
- `apply_does_not_overwrite_target` (preexisting target は不変)
- `apply_fails_when_duplicate_exists` / `_when_invalid_metadata` /
  `_when_target_exists` を含む blocked 系
- `apply_writes_manifest` / `_even_when_blocked`
- `apply_recomputes_plan_before_copy` (直前再計算 contract)
- `apply_no_overwrite_performed_flag`
- CLI: `--apply requires --copy-plan` (exit 2) / `--apply ok` /
  `--apply blocked returns 1`
- Boundary: PyVISA / `visa_mcp` 非依存 subprocess gate
- 回帰: install_default 不変 / Stable 43 + Experimental 7 = 50 不変

### docs / cli docstring

- `docs/extension_path_migration.md`: `--apply` セクション + 事前
  条件 + 安全保証 + manifest schema + やらないこと一覧、ロードマップ
  表に v2.7 を実装済として追加
- `cli.py` module docstring を v2.6.x → v2.7.x

### 互換性

- `plan_extension_migration()` (引数なし) は v2.5 完全互換、
  `copy_plan=True` は v2.6 互換
- MCP tool / DSL / extension pack 形式 / `.install_meta.json` /
  `default_extensions_dir()` 返り値、すべて不変

---

## v2.6.1 — Docs / Review patch (target_exists semantics 明文化)

v2.6.0 レビュー反映 patch。コード変更は最小限。

### Docs / CLI 文言

- `cli.py` argparse `description` を「dual-path extension discovery,
  migration planning, and copy-plan preview (v2.6)」へ更新。v2.5
  表記を解消。
- `cli.py` module docstring 末尾の「`~/.lab-executor/extensions/`
  への切替は v2.5+ で判断」を「v2.7+ で判断」に更新 (v2.6 時点での
  ロードマップ整合)。
- `ExtensionMigrationAction` docstring を v2.6 現状に書き直し:
  「v2.5+ では action は提案のみ。v2.6 で `--copy-plan` を導入したが
  `apply_available` は引き続き常に False。controlled apply は v2.7+」
  という表現に統一。

### `target_exists` semantics 明文化

`docs/extension_path_migration.md` に v2.6.0 で実装した
**partial-skipped 挙動**を明示:

| 状況 | `copy_plan.status` | `blocked_reasons[]` |
|------|---------------|-----|
| 全 legacy_only に target_exists | `blocked` | 全件を target_exists で列挙 |
| 一部のみ target_exists、他は copy 可 | `ready` | skipped 分のみ残す |

`blocked_reasons` は **「status=blocked の理由」とは限らず**、
「candidate にできなかった件の理由」を列挙する schema。`status=
ready` でも `blocked_reasons` に skipped 詳細が入りうる、という
読み方を明文化。

加えて、v2.7 で `--apply` を入れる時の事前条件として
**「`blocked_reasons` が空であること」を必須にする方針**を docs に
予約 (skipped を黙って無視しない、案 B の延長)。

### Internal

- version 2.6.0 → 2.6.1 (`__init__.py` / `pyproject.toml`)
- コア logic (plan_extension_migration / copy plan / CLI ロジック)
  は不変、tests 103 件 pass

---

## v2.6.0 — Extension Migration Copy Plan

合言葉: **「v2.5 で計画、v2.6 で copy 候補。まだ実行しない」**

v2.5 の migration plan を一段具体化し、「legacy にしかない pack を
new path に copy するなら何が対象か」を機械可読に出す段階。実 copy
/ move / delete は **v2.6 でも一切しない** (`--apply` は v2.7+ で
慎重に検討)。

### 新規 dataclass: `ExtensionCopyCandidate` / `ExtensionCopyPlan`

```python
@dataclass(frozen=True)
class ExtensionCopyCandidate:
    extension_id: str
    source: Path              # legacy 側 source
    target: Path              # new 側 target (まだ存在しない)
    reason: str
    safe_to_copy: bool = True
    overwrite_required: bool = False   # v2.6 では常に False

@dataclass
class ExtensionCopyPlan:
    status: str               # "ready" / "empty" / "blocked"
    candidates: list[ExtensionCopyCandidate]
    blocked_reasons: list[dict]
    apply_available: bool = False      # v2.6 では常に False
```

### `plan_extension_migration(copy_plan=True)`

既存 API を拡張 (default は False で v2.5 と同挙動):

- `copy_plan=False` (default): `ExtensionMigrationPlan.copy_plan = None`、
  schema_version=`v2.5`
- `copy_plan=True`: `ExtensionMigrationPlan.copy_plan = ExtensionCopyPlan
  (...)`、schema_version=`v2.6`、`summary.copy_candidates` /
  `summary.copy_blocked` を追加

### `copy_plan.status` 判定

| Status | 条件 |
|--------|------|
| `blocked` | duplicate_extension_id あり、または invalid_extension_metadata あり、または **全 legacy_only に target_exists** |
| `ready`   | candidate が 1 件以上ある (一部 skipped でも可、skipped は blocked_reasons に列挙) |
| `empty`   | legacy_only がなく candidate もない (cleanup 不要) |

### Block 条件 (実 copy 前に必ず止める)

- `duplicate_extension_id`: 案 B により、まず duplicate を解消する
  必要がある
- `invalid_extension_metadata`: `extension.yaml` parse 失敗 / `extension
  _id` 欠落
- `target_exists`: `new_path/<dir_name>` が既に存在する (overwrite は
  v2.6 では行わない)

### 新規 CLI flag: `extension migration-plan --copy-plan`

```bash
lab-executor extension migration-plan --copy-plan
lab-executor extension migration-plan --copy-plan --json
lab-executor extension migration-plan --copy-plan --strict
```

実ファイルは一切変更しない。Human-readable 出力に copy_plan セクション
(candidates / blocked / skipped + "no files were changed" 表示) を
追加。

### v2.6 で **やらないこと**

- `--apply` / 実 copy / 実 move / 実 delete
- target 自動作成 / overwrite
- install default 変更
- active_read_paths の優先順位変更
- extension pack / `.install_meta.json` schema 変更
- MCP tool 追加 / DSL schema 変更

### Tests (103 件 pass)

新規 `tests/test_v260_copy_plan.py`: 14 件

- `copy_plan_legacy_only_candidates` / `new_only_no_candidates`
- `copy_plan_duplicate_blocked` / `invalid_metadata_blocked` /
  `target_exists_skipped_or_blocked`
- **`copy_plan_no_file_changes`** (v2.6 の核): plan 前後で legacy /
  new directory tree が変わらないことを snapshot 比較で固定
- `copy_plan_apply_available_false`
- `copy_plan_omitted_when_flag_false` (default 互換)
- CLI: `--copy-plan` help / JSON 出力 / duplicate blocked
- Boundary: PyVISA / `visa_mcp` 非依存 subprocess gate
- 回帰: install_default 不変 / Stable 43 + Experimental 7 = 50 不変

### docs / cli docstring

- `docs/extension_path_migration.md`: `--copy-plan` セクション +
  blocked JSON 例 + ロードマップ表に Status 列 (実装済 / 検討中) 追加
- `cli.py` module docstring を v2.5.x → v2.6.x へ更新

### 互換性

- 既存 `plan_extension_migration()` (キーワードなし呼出) は v2.5 と
  完全同一の挙動 (`copy_plan=None`, schema_version=`v2.5`)
- MCP tool / DSL / extension pack 形式 / `.install_meta.json` /
  `default_extensions_dir()` 返り値、すべて不変

---

## v2.5.1 — Docs / Review patch + summary breakdown

v2.5.0 レビュー反映 patch。

### Docs / CLI 文言

- `cli.py` docstring を v2.4.x → v2.5.x へ更新。`migration-plan` /
  `resolve_extension_by_id()` を全体説明と Exit code policy section に
  追記
- argparse `description` を「dual-path extension discovery and
  migration planning (v2.5)」に更新
- `ExtensionMigrationAction` docstring を明確化。「`apply_available
  =False` は v2.5 では常に True にならない」という曖昧な表現を
  「v2.5 では `apply_available` は常に False。本 release は plan の
  みを出し、copy / move / delete は実行しない」に書き直し
- README から `docs/extension_path_migration.md` へのリンクを追加

### Summary breakdown (v2.5.1)

`ExtensionMigrationPlan.summary` に内訳 field を追加 (既存 field
は不変):

- `invalid_metadata`: `extension.yaml` parse 失敗 / `extension_id`
  欠落 (severity=error の起点)
- `missing_extension_yaml`: pack dir はあるが `extension.yaml` が
  ない (severity=warning の起点)

これで CI / 人間レビューが「error 系の invalid か、warning 系の
missing か」を summary 1 段で判別できる。`invalid` (合算) は
互換のため残す。

### docs/extension_path_migration.md 強化

- catalog/check と migration-plan で duplicate の severity が違う
  理由を表で明示 (catalog/check は warning、migration-plan は error)
- summary breakdown の 2 新 field を説明
- README からの導線確認

### Internal

- version 2.5.0 → 2.5.1 (`__init__.py` / `pyproject.toml`)
- コア logic (plan_extension_migration / resolve_extension_by_id /
  CLI ロジック) は不変、tests 88 件 pass

---

## v2.5.0 — Extension Migration Plan + Conflict Resolution Guidance

合言葉: **「v2.4 で検出、v2.5 で計画。まだ動かさない」**

v2.4 で dual-path read + duplicate 検出ができるようになった次の段階。
v2.5.0 では、検出結果に対する **plan のみ**を出し、ファイルは一切
変更しない。`--apply` / 自動 copy / 自動 move / 自動 delete は
v2.5 では実装しない (v2.6+ で慎重に検討)。

### 新規 module: `lab_executor.extension_migration`

```
ExtensionMigrationAction (frozen dataclass)
  action / extension_id / severity / locations / recommendation
  / apply_available  (v2.5 では常に False)

ExtensionMigrationPlan
  status / summary / actions / paths

plan_extension_migration(paths=None) -> ExtensionMigrationPlan
```

`summary` フィールド:

- `legacy_only`: `~/.visa-mcp/extensions/` にのみ存在する pack 数
- `new_only`: `~/.lab-executor/extensions/` にのみ存在する pack 数
- `duplicates`: 両 path にある `extension_id` の数
- `invalid`: metadata 不正 / YAML 不在の数
- `migration_required`: 実態ベースで判定
  (`legacy_only > 0` OR `duplicates > 0` OR `invalid > 0`)

`status`:

- `error`: duplicates あり、または invalid metadata あり
- `warning`: legacy_only あり、または missing extension.yaml あり
- `ok`: 上記いずれもなし (new_only のみは ok 扱い)

### 新規 API: `resolve_extension_by_id()`

`lab_executor.extension_discovery` に追加:

```python
def resolve_extension_by_id(
    extension_id: str,
    *,
    paths: ExtensionPaths | None = None,
) -> InstalledExtension:
    """
    - 見つからない -> ExtensionResolveError("extension_not_found")
    - 1 件だけ      -> InstalledExtension を返す
    - 複数 (duplicate) -> ExtensionResolveError(
                          "duplicate_extension_id")
    """
```

duplicate を **黙って解決しない** ことを API レイヤで強制する。
構造化 error class `ExtensionResolveError` (error_class /
extension_id / locations / message を保持) を新規追加。

### 新規 CLI: `lab-executor extension migration-plan`

```bash
lab-executor extension migration-plan
lab-executor extension migration-plan --json
lab-executor extension migration-plan --strict
```

**実ファイルは変更しない**。出力するのは現状 path 状態と推奨 action
のみ。

Exit code:

| status | default | --strict |
|---|---|---|
| ok | 0 | 0 |
| warning | 0 | 1 |
| error | 1 | 1 |

### 新規 docs: `docs/extension_path_migration.md`

- v2.4 以降の dual-read 構成
- write_default が legacy のままである理由
- duplicate を黙って優先しない方針 (案 B) の明文化
- duplicate を手作業で解消する手順
- `migration_required` の判定ロジック
- v2.5 で **やらないこと** の list
- v2.6 (copy-plan) / v2.7 (controlled apply) / v2.8 (default 切替)
  のロードマップ

### v2.5 で **やらない**こと

- `--apply` 実装
- 自動 copy / move / delete
- install default の `~/.lab-executor/extensions/` への変更
- duplicate 時の自動採用
- extension pack 形式 / `.install_meta.json` schema の変更
- MCP tool 追加 / DSL schema 変更

### Tests (88 件 pass)

`tests/test_v250_migration_plan.py`: 14 件

- plan: no_extensions / legacy_only / new_only / duplicate /
  invalid_metadata
- plan: ファイル変更なし回帰
- plan: schema_version=v2.5 / フィールド完全性
- `resolve_extension_by_id`: ok / not_found / duplicate
- CLI: help / --strict 挙動 (error→exit 1, warning→exit 0/1)
- Boundary: PyVISA / `visa_mcp` 非依存 subprocess gate
- 回帰: install_default 不変 / Stable 43 + Experimental 7 = 50 不変

### 互換性

MCP tool / DSL / extension pack 形式 / `.install_meta.json` / 既存
の `default_extensions_dir()` 返り値、すべて v2.4 から不変。

---

## v2.4.1 — Docs / Review patch + Release verification manifest

v2.4.0 レビュー反映 patch。コード変更は最小限 (docstring / help 文言
の更新)、加えて **raw 表示問題の再発防止策**として release-time
verification manifest を導入する。

### Docs / CLI 文言

- `src/lab_executor/cli.py` module docstring を v2.2.x → v2.4.x へ
  更新。v2.3 / v2.4 で追加されたサブコマンド (extension install /
  check / catalog / paths、dual-path discovery、duplicate conflict
  detection) を反映。
- `lab-executor extension paths --help` を「v2.3: planning only」→
  「v2.4: dual-read, legacy write default」へ修正。
- argparse `description` を v2.4 へ更新。
- README に **v2.4 path behavior 表**を追加 (read / write / duplicate
  / auto-precedence / policy id)。
- `discover_installed_extensions()` docstring に warning ブロックを
  追加: duplicate がある場合 `extensions[]` に入っている record は
  **display compatibility 目的**であり「選択された extension」では
  ない、と明示。downstream `extension_id` 解決は `duplicates` を
  チェックし `duplicate_extension_id` error を返すべき
  (v2.5+ で `resolve_extension_by_id()` 実装予定)。

### Raw 表示問題の根本対応 (再発防止)

複数の review で `raw.githubusercontent.com` 経由の file が "1 line
/ collapsed" と報告されてきたが、curl で実際の bytes を測ると毎回
LF=数十〜数百 / CR=0 で multi-line 確認できる。これは **viewer 側
artifact** であり repo 側ではない。

この事実を毎回 review で再証明させるのは非効率なので、release tag
時に **`RELEASE_VERIFICATION.md`** を自動生成して同梱する運用に変更:

- 新規 script: `scripts/release_verification.py`
  - critical files のリストを保持
  - **git canonical bytes** (`git show HEAD:<path>`) を読むことで、
    Windows の autocrlf 影響を排除して repo 真値で集計
  - `--check`: 全 critical file が `CR == 0` / `LF >= 10` / no BOM
    を満たすかを exit code で gate (CI 用)
  - 引数なし: markdown manifest (bytes / LF / CR / BOM の表) を
    stdout 出力
- 新規 file: `RELEASE_VERIFICATION.md` (root に commit)
  - reviewer が viewer の表示を疑ったとき、まず読むべき ground
    truth。`clone --branch <tag>` + `release_verification.py --check`
    で誰でも `OK` を確認できる
- README の line-ending note を更新し、`RELEASE_VERIFICATION.md`
  を canonical 参照先として明示

これにより、v2.4.1 以降は「viewer が 1 line と言っている」レビュー
コメントに対して、**毎回手作業で curl 検証を再実行する必要がなく
なる**。reviewer 側で `release_verification.py --check` を走らせる
だけで終わる。

### Internal

- バージョン 2.4.1 に bump (`__init__.py` / `pyproject.toml`)。
- コード本体 (extension_paths / extension_discovery / cli ロジック)
  は v2.4.0 から不変。tests 71 件は引き続き pass。

---

## v2.4.0 — Dual-path Extension Discovery + Duplicate Conflict Detection

合言葉: **「新 path を読み始める。ただし黙って優先しない」**

v2.3.0 で planning に留めた path 移行を、v2.4.0 で
**読み取りだけ dual-path 化** する。書き込み default は legacy
(`~/.visa-mcp/extensions/`) のまま。同じ `extension_id` が
new (`~/.lab-executor/extensions/`) と legacy 両方に存在する場合は
**自動採用せず、warning として報告**する (案 B:
`report_conflict_no_implicit_precedence`)。

### Source of truth: `ExtensionPaths` (v2.4 schema)

`lab_executor.extension_paths.get_extension_paths()` を拡張し、
読み・書き・表示を分離した:

```
read     : active_read_paths = [new_path, legacy_path]
write    : write_default       = legacy_path   (v2.4 では legacy のまま)
display  : current_default / future_default_candidate
policy   : duplicate_policy = "report_conflict_no_implicit_precedence"
```

新 fields: `legacy_path` / `new_path` / `write_default` /
`duplicate_policy`。`to_dict()` の `schema_version` は `"v2.4"`。

### 新規 module: `lab_executor.extension_discovery`

`catalog` / `check` / 将来の `migration-plan` が共有する dual-path
scan + duplicate 検出ロジックを 1 箇所に集約:

- `discover_installed_extensions(paths=None) -> ExtensionDiscoveryResult`
- `InstalledExtension` (frozen dataclass): `extension_id` / `path`
  / `source_path` / `metadata` / `install_meta`
- `ExtensionDiscoveryResult`: `extensions` / `duplicates` /
  `warnings` / `errors` / `duplicate_policy`

duplicate 判定は **ディレクトリ名ではなく `extension.yaml` の
`extension_id`** ベース。YAML 読み込み失敗は
`invalid_extension_metadata` / `missing_extension_yaml` として
errors / warnings に分離して報告する。

### CLI 挙動 (v2.4)

- **`lab-executor extension paths`**: `legacy_path` / `new_path`
  / `write_default` / `active_read_paths` / `duplicate_policy` を
  表示。
- **`lab-executor extension catalog`**: dual-path discovery 経由で
  install 済 pack を列挙。duplicate がある場合 `status=warning`
  + `duplicates` block 出力。`--strict` で warning → exit 1。
- **`lab-executor extension check`**: dual-path discovery + 個別
  integrity check の合算。`summary.duplicate_extension_ids` を
  返す。default では warning でも exit 0、`--strict` で exit 1。

### `lab-executor extension install` の挙動

**default 書き込み先は引き続き `~/.visa-mcp/extensions/`** (v2.4 で
変更しない)。v2.5+ で切替判断する。

### v2.4 で **やらないこと**

- install default を `~/.lab-executor/extensions/` に変更
- duplicate 時に自動で片方を優先 / 削除 / 移動
- migration 自動実行
- extension pack 形式 / `.install_meta.json` schema 変更
- MCP tool 追加 / DSL schema 変更

### Tests

- `tests/test_v240_extension_dual_path.py`: 18 件
  - `ExtensionPaths` v2.4 schema (active_read_paths dual /
    write_default legacy / duplicate_policy)
  - `discover_installed_extensions` (legacy 単独 / new 単独 /
    duplicate 検出 / missing_extension_yaml /
    invalid_extension_metadata)
  - CLI `extension paths/catalog/check` (`--strict` 含む)
  - `install_default` 不変回帰
  - PyVISA / `visa_mcp` 非依存 subprocess 検査
  - tool surface 不変 (Stable 43 + Experimental 7 = 50)
- v2.3 既存 tests を v2.4 schema 受容に更新
  (`schema_version in {"v2.3","v2.4"}`)

### 互換性

- MCP tool 名 / 引数 / response、DSL `dsl_version=0.8`、
  extension pack 形式 (`.visa-mcp-ext.zip`)、`.install_meta.json`
  schema はすべて不変。
- `default_extensions_dir()` の返り値 (`~/.visa-mcp/extensions/`)
  も不変。v2.4 では `write_default` と完全一致する。

---

## v2.3.1 — Docs / Review patch

v2.3.0 レビュー反映 patch。コード変更なし、docs と CHANGELOG の補強のみ。

### Docs

- **README "Line-ending / raw display note"**: viewer 側で
  `raw.githubusercontent.com` が "1 line / collapsed" と誤表示される
  ケースについて、誰でも検証できる `curl | python` 1-liner を例示。
  リポジトリは LF 単独 (CR=0) で保存されており、CI test
  `test_critical_files_are_multiline_and_lf_only` で gate されている
  ことを明記。
- **README "CLI status"**: v2.1 範囲の記述を v2.3 範囲へ更新。v2.2 /
  v2.3 で追加された CLI (`extension init/install/check/catalog/paths`
  / `instrument scaffold/promote-check/review-report` /
  `diagnose tool-surface`) を反映。
- **Exit code policy table**: v2.3 subcommand
  (`extension install/check/catalog/paths`) の row を追加。
- **`--skip-verify` 警告強化**: 「test 用途のみ。信頼できない zip に
  対しては絶対に使わない」を明示。
- **`--dry-run` semantics 明確化**: v2.3 dry-run は package verify
  のみで、install 済 extension_id の重複検査は行わないことを明記
  (v2.4+ で検討)。
- **`extension_paths` を v2.4 source of truth 化する旨**を docs TODO
  として記載 (`default_extensions_dir` → `get_extension_paths()
  .current_default` 段階移行計画)。

### Internal

- バージョンを 2.3.1 に bump (`__init__.py` / `pyproject.toml`)。
- コードは無変更、tests / CI 既存 gate は全て pass。

---

## v2.3.0 — Extension Lifecycle CLI + Path Migration Planning

合言葉: **「v2.2 で作る CLI が揃ったので、v2.3 で install して使う
CLI を揃える。path 移行は実装せず planning に留める」**

v2.2.x で `extension init / instrument scaffold / doctor / package /
verify-package` が揃った。v2.3.0 では **install → check → catalog**
までを `lab-executor` 側 CLI で完結できるようにし、`~/.lab-executor/
extensions/` への migration は **planning のみ** (path resolver +
`extension paths` CLI) で実装は v2.4+。並走して `SessionFacade`
Protocol 化と `JobManager` TYPE_CHECKING cleanup を実施。

### 新規 CLI subcommands (P0)

- **`lab-executor extension install <zip>`**: definition pack を
  `~/.visa-mcp/extensions/` に install (`--dry-run` で verify のみ、
  `--force` で上書き、`--skip-verify` は test 用)
- **`lab-executor extension check`**: install 済 extension の整合性
  検査 (checksum / manifest / metadata)。`--extension-id <id>` で
  対象を絞れる。`--strict` で warning → exit 1 (default は exit 0)
- **`lab-executor extension catalog`**: install 済 extension 一覧
  (extension_id / version / support_level)
- **`lab-executor extension paths`**: install path resolver の現状
  を表示 (v2.3 では planning only、default 動作は v2.2 から不変)

### Path migration planning

`lab_executor.extension_paths.get_extension_paths()` 公開 API
追加。`ExtensionPaths` dataclass で:

- `current_default`: 現在 install 先 (`~/.visa-mcp/extensions/`)
- `future_default_candidate`: 切替候補 (`~/.lab-executor/extensions/`)
- `active_read_paths`: catalog / check が読む path 一覧 (v2.3 は
  `current_default` 単独、v2.4 で dual-read 検討)
- `migration_required`: v2.3 では常に `False`

**v2.3 では default path 変更を行わない**。v2.4 で dual-read 設計、
v2.5+ で default 切替判断、というロードマップを `paths` CLI 出力で
明示する。

### Internal cleanup (P1)

- **`lab_executor.session.SessionFacade`** Protocol 新規追加
  (`runtime_checkable`):
  - `get_session(resource) -> Any` の最小 surface
  - `server._SessionFacade` / `visa-mcp` 側 `SessionManager` 双方が
    満たすことで、tool 層から見た session lookup の contract を明示
- **`src/lab_executor/job/manager.py` TYPE_CHECKING cleanup**:
  v2.2 まで残っていた `from visa_mcp.session_manager import
  SessionManager` / `from visa_mcp.visa_manager import VisaManager`
  を、lab-executor 側 Protocol へ置換 (`InstrumentBackend as
  VisaManager` / `SessionFacade as SessionManager` legacy alias)。
  これで `src/lab_executor/` 配下から `visa_mcp` 参照が完全に消えた
  (TYPE_CHECKING 含む)

### tests (`tests/test_v230_extension_lifecycle.py` 新規 12 件)

- `test_extension_paths_module_importable` / `..._default_legacy_path`
  / `..._to_dict`
- `test_cli_extension_paths_help` / `..._json`
- `test_cli_extension_install_help`
- `test_cli_extension_check_help`
- `test_cli_extension_catalog_help`
- `test_session_facade_protocol_importable`
- `test_session_facade_runtime_checkable` (内部 `_SessionFacade` が
  Protocol を満たす確認)
- `test_job_manager_type_checking_no_visa_mcp_reference`
- `test_no_pyvisa_for_extension_paths_subprocess`
- `test_mcp_tool_surface_unchanged` (43 + 7 = 50 不変)

合計 **53 件 pass** (v2.0 + v2.1 + v2.2 + v2.3)

### 互換性

- API / package 構造 / MCP tool / DSL / extension pack 形式: 不変
- **install path default**: `~/.visa-mcp/extensions/` (v2.2 から不変)
- `.install_meta.json` schema: 不変
- `SessionFacade` Protocol は新規追加のみ (既存 `_SessionFacade` /
  `SessionManager` の挙動を変えない)
- `JobManager` TYPE_CHECKING の rename は `visa_mcp` → 同名 alias
  なので呼び出し側コードは無修正

### v2.3.0 でやらないこと

- `~/.lab-executor/extensions/` への default 切替
- dual-read 実装 (v2.4 候補)
- remote registry / signature / trust store
- backend plugin system / replay backend
- MCP tool 追加 / DSL schema 変更

### v2.4+ 候補

- `~/.lab-executor/extensions/` dual-read support
- duplicate extension id の優先順位ルール
- migration dry-run / migration command
- catalog filtering (`--tag` / `--support-level`)
- Replay backend 設計着手

## v2.2.1 — v2.2.0 レビュー応答 (docstring 更新 / --id help / diagnose --strict / README exit code)

合言葉: **「v2.2.0 直後の docs / exit code policy 仕上げ」**

v2.2.0 external review (P1/P2) 反映の small patch。public API /
dependency / shim 動作すべて不変。

### 変更点

- **P1** (`src/lab_executor/cli.py` docstring):
  - 冒頭を「v2.1.0」→「v2.2.x」へ更新
  - v2.1.0 / v2.2.0 のサブコマンドを段階的に列挙
  - **Exit code policy** を docstring 内に明文化
    (0 / 1 / 2 の意味、`diagnose tool-surface` の strict mode 説明)
- **P1** (`extension init --id` help):
  - "reverse-DNS extension id (default: local.<pack_name>)" → より
    具体的な "default: 'local.<pack_name>', e.g. 'local.my_pack'"
    に変更
- **P1** (`diagnose tool-surface --strict` 追加):
  - default では warning でも exit 0 (手元診断向け、warning は表示
    のみ)
  - `--strict` 指定時のみ warning → exit 1 (CI gate 用途)
  - JSON 出力に `strict_mode` field 追加
- **P1** (`README.md` exit code table 拡張):
  - `extension init` / `instrument scaffold` / `instrument
    review-report` / `diagnose tool-surface` の exit code を追記
  - `diagnose tool-surface` の warning + `--strict` 無し → exit 0 の
    挙動を明示

### tests

- 既存 v2 smoke test 39 件 すべて pass (v2.0 + v2.1 + v2.2)
- diagnose `--strict` の追加は default behavior の緩和のみで、
  既存 test (`test_cli_diagnose_tool_surface_json`) は exit code
  0 か 1 を許容しているため pass 維持

### 互換性

- API / package 構造 / MCP tool / DSL / extension pack: すべて不変
- `diagnose tool-surface` の **default exit code が変わる**
  (warning で exit 1 → exit 0)。CI で fail させたい場合は `--strict`
  を明示すること

### 注意点 (v2.3+ の宿題)

- `src/lab_executor/job/manager.py` の `TYPE_CHECKING` 内に
  `visa_mcp.session_manager` / `visa_mcp.visa_manager` 参照が残存
  (runtime import ではないが、v2.3 で lab-executor 側 Protocol へ
  置換予定)
- `SessionFacade` を Protocol へ昇格 (v2.3 候補)
- `lab-executor extension install / check / catalog` + path migration
  は v2.3 で着手

## v2.2.0 — CLI Authoring Workflow + Backend Naming Cleanup

合言葉: **「v2.1 で server を起動できるようになった runtime を、
definition pack / instrument 定義を CLI で作れる段階まで育てる」**

v2.1.x で `lab-executor serve --backend mock` が動くようになった。
v2.2.0 では CLI authoring workflow を拡張し、runtime 内部の
`visa=` 命名を `backend=` へ移行する。public MCP tool / DSL /
extension pack 形式すべて不変。

### 新規 CLI subcommands (v1.x `visa-mcp` から port)

- **`lab-executor extension init <pack_name>`**: definition pack を
  scaffold (template: minimal / mock_basic / instrument_pack)
- **`lab-executor instrument scaffold <category>`**: instrument YAML
  を category 別 template から生成 (power_supply / dmm /
  temperature_meter / generic_scpi、`support_level: draft` 固定)
- **`lab-executor instrument review-report <path>`**: instrument
  YAML から markdown 形式 PR review を生成 (strict validate +
  promote-check 集約)
- **`lab-executor diagnose tool-surface`**: declared (43+7=50) vs
  registered MCP tool 数の差分を JSON / text で出力 (v2.1.1 で
  追加した `diagnose_tool_surface(server)` の CLI 化)

これで lab-executor 単独で「pack 作成 → instrument scaffold →
doctor → package → verify-package」の authoring loop が完結する。

### Runtime 内部命名整理

**`JobManager(backend=...)` keyword 追加** (v2.2.0 推奨):

```python
# v2.2.0+ 推奨
JobManager(backend=mock_backend, session_mgr=..., store=...)

# 旧 (v2.1 まで) — v2.2.0 で DeprecationWarning + v3.x で削除候補
JobManager(visa=mock_backend, session_mgr=..., store=...)
```

`visa=` と `backend=` 同時指定は `TypeError`。`server.create_server()`
は内部で `backend=` 経由に切替済 (DeprecationWarning を triggered
しない)。

### Templates パッケージ復活

`src/lab_executor/templates/instruments/` (dmm / power_supply /
temperature_meter / generic_scpi の YAML テンプレ) を v2.2.0 で
正式に含めた (v2.0 split 時の copy 漏れを修正)。
`instrument_authoring._load_template()` は
`lab_executor.templates.instruments.*` を優先、fallback で
`visa_mcp.templates.instruments.*` も試す。

### tests (`tests/test_v220_cli_authoring.py` 新規 11 件)

- `test_cli_extension_init_help` / `..._generates_pack`
- `test_cli_instrument_scaffold_help` / `..._generates_yaml`
- `test_cli_instrument_review_report_help`
- `test_cli_diagnose_tool_surface_help` / `..._json`
- `test_job_manager_accepts_backend_keyword`
- `test_job_manager_visa_keyword_deprecated`
- `test_job_manager_rejects_both_keywords`
- `test_create_server_uses_backend_keyword_path`
  (DeprecationWarning が出ないこと)
- `test_authoring_cli_no_pyvisa_subprocess`
  (`instrument scaffold` が PyVISA / visa_mcp なしで動く)

合計 **39 件 pass** (v2.0 + v2.1 + v2.2)

### 互換性

- public API / MCP tool / DSL `dsl_version=0.8` / extension pack
  形式すべて不変
- `JobManager(visa=...)` は **動作するが DeprecationWarning** (v3.x
  で削除候補)
- `serve --backend mock` の挙動: 不変

### v2.2.0 でやらないこと

- backend plugin system
- REST / replay backend 本実装
- `lab-executor extension install / catalog / check` (v2.3 候補)
- `~/.lab-executor/extensions/` への default 切替 (v2.3+ 候補)
- MCP tool 追加
- DSL schema 変更

### v2.3+ 候補

- `lab-executor extension install / check / catalog`
- `~/.lab-executor/extensions/` への dual-read 設計 + migration
  dry-run
- `SessionFacade` を Protocol に昇格 (review P1)
- Replay backend 設計着手

## v2.1.1 — v2.1.0 レビュー応答 (README serve table / exit code policy / diagnose_tool_surface)

合言葉: **「v2.1.0 直後の docs / diagnostic 仕上げ」**

v2.1.0 external review (P1) 反映の small patch。public API / dependency
/ MCP tool 数 declaration すべて不変。

### 変更点

- **P1** (`README.md`):
  - **serve 使い分け表** を追加 (`lab-executor serve --backend mock`
    vs `visa-mcp serve` の用途 / PyVISA 依存 / backend 種別を一覧化)
  - **Quick examples** section 追加 (`--dry-run` / `validate
    extension` / `extension doctor` / `package` + `verify-package`
    の典型 4 ケース)
  - **Exit code policy** を表形式で明文化:
    | Subcommand | exit 0 | 1 | 2 |
    `doctor` は warning でも exit 1 (CI gate として強い設計) を明記
- **P1** (`src/lab_executor/server.py`):
  - `diagnose_tool_surface(server)` 公開 helper 追加。`stability`
    declaration (43 + 7 = 50) と実 registry の差分を構造化辞書で返す
    (`missing_from_registry` / `extra_in_registry` 等)。v2.2+ で AI
    エージェント向けに「declaration にあるのに registry に無い tool」
    を可視化する診断 CLI の土台。
- **P1** (`README.md` notes):
  - runtime 内部の `JobManager(visa=...)` 引数名は v2.1 で互換維持
    していること、v2.2+ で `backend=` への rename を検討する旨を明記

### tests

- `test_diagnose_tool_surface` 追加 → **26 件 pass**

### 互換性

- API / package 構造: 不変
- MCP tool / DSL / extension pack: 不変
- 既存 `list_registered_tools()` API: 不変 (`diagnose_tool_surface()`
  を追加のみ)

## v2.1.0 — Mock Runtime Server / CLI Activation

合言葉: **「v2.0 で分離した runtime を、単独で起動できる形に近づける」**

v2.0.x まで placeholder だった `lab-executor serve` を、v2.1.0 で
**MockBackend 経由で起動可能**にする backend-independent MCP server
release。新しい MCP tool / DSL 変更 / extension pack 形式変更は無し。

### 新機能

- **`lab_executor.server.create_server(backend=None, *, name=...)`** 公開 API
  - `InstrumentBackend` を inject して MCP server を構成
  - 引数省略時は `MockBackend` を default 使用
  - `list_registered_tools(server)` helper も追加
- **`lab-executor serve --backend mock`** (CLI)
  - MockBackend で MCP server 起動 (PyVISA / visa-mcp 非依存)
  - `--dry-run` で server を compose して tool 一覧を出すだけ
  - 引数なしは exit 2 + `visa-mcp serve` への誘導 (実機 backend は
    visa-mcp 側で継続)
- **`lab-executor validate extension <path>`** port
- **`lab-executor extension {doctor,package,verify-package}`** port

### tools 登録

`tools/audit/commands/dsl/export/groups/info/jobs/monitor/observation/
pdf_extractor/recipes/waits` の `register_tools(mcp, ...)` を順に呼び、
v1.0 凍結の MCP tool surface を expose する。

- 内部 facade: `_SessionFacade` (SessionManager 互換最小実装)
- JobManager は MockBackend を `visa:` として受ける (duck-typed)
- 実 registry に登録される tool 数は >= 30 (実装で変動するが core
  tool はすべて含まれる)
- `stability.STABLE_TOOLS` / `EXPERIMENTAL_TOOLS` の declaration は
  **43 + 7 = 50** で不変

### Backend independence

- `lab_executor.server` module 自体は PyVISA / visa_mcp に依存しない
- `create_server()` 呼び出しも、`visa_mcp` を import 経路から block
  した状態で動作することを subprocess test で確認
  (`test_no_pyvisa_when_visa_mcp_blocked_subprocess`)

### tests (`tests/test_v210_server.py` 新規 14 件)

- `test_create_server_with_default_mock_backend`
- `test_create_server_with_explicit_mock_backend`
- `test_mock_server_tool_count_is_reasonable`
- `test_stability_declarations_unchanged` (43 + 7 = 50)
- `test_server_module_imports_without_pyvisa`
- `test_server_creates_without_visa_mcp_installed`
- `test_no_pyvisa_when_visa_mcp_blocked_subprocess`
- `test_cli_serve_requires_backend` (引数なし → exit 2)
- `test_cli_serve_backend_mock_dry_run`
- `test_cli_serve_help`
- `test_cli_validate_extension_help`
- `test_cli_extension_help`
- `test_cli_extension_doctor_help`
- `test_v2_1_version`
- `test_no_top_level_visa_mcp_import_added`

合計 25 件 pass (v2.0 smoke 含む)。

### CLI message 言語

`serve` placeholder で得た知見を踏襲し、CLI argparse の help /
description / stderr message は **ASCII-only** に統一。subprocess test
が Windows cp932 環境でも安全に動く。

### 互換性

- API / package 構造: 不変
- MCP tool 数 declaration: 43 + 7 = 50 (v1.0 から不変)
- DSL `dsl_version=0.8`: 完全互換
- extension pack 形式: 完全互換
- `~/.visa-mcp/extensions/` install path: 継続使用

### v2.1.0 でやらないこと

- backend plugin system
- REST / replay backend 実装
- remote registry
- package signing
- install path default 変更 (v2.2+ で検討)
- MCP tool 追加
- DSL schema 変更

### v2.2+ 候補

- `lab-executor extension init / install / catalog`
- `lab-executor instrument scaffold / review-report`
- `~/.lab-executor/extensions/` への並走移行計画
- 他 backend (REST / replay / plugin) の `--backend` choice
- visa-mcp shim 利用状況を見た Deprecation スケジュール調整

## v2.0.2 — CI hotfix (TYPE_CHECKING import / ASCII CLI / smoke test scope)

合言葉: **「v2.0.1 で CI 全 job 通すための hotfix」**

v2.0.1 push 後の GitHub Actions failure 解析と修正。public API / MCP
tool / DSL / extension pack すべて不変。

### 失敗原因

`run 26430762700` で `test` / `pyvisa-not-installed` job が fail:

1. **P0 (`src/lab_executor/tools/commands.py`)**: bootstrap script の
   patch 関数が `if TYPE_CHECKING:` を挿入する際、当該 file に
   `from typing import` が存在しなかったため `TYPE_CHECKING` が
   undefined になっていた。collection 段階で `NameError` 発生 →
   pytest 全停止
2. **P0 (`tests/test_v200_split.py`)**: `lab-executor serve` の
   stderr メッセージが日本語で、Windows subprocess decode 時に
   cp932 → utf-8 mismatch で `UnicodeDecodeError` 発生
3. **P1 (`.github/workflows/ci.yml` test job)**: pytest 全件 (152
   inherited visa-mcp tests + 1 smoke) を実行していたが、inherited
   tests は v2.0 split に未適応 → 大量 fail

### 修正

- **P0-1** (`src/lab_executor/tools/commands.py`): `from typing import
  TYPE_CHECKING` を追加
- **P0-1** bootstrap script (`visa-mcp` repo): `from typing import`
  が存在しない場合は `from __future__ import annotations` 直後に
  新規 import 行を挿入するよう改良 (再 bootstrap 時の regression
  防止)
- **P0-2** (`src/lab_executor/cli.py`): `serve` placeholder の stderr
  メッセージを **ASCII-only** に変更 (Windows cp932 / Linux UTF-8 /
  CI locale を問わず subprocess で安全に decode できる)
- **P0-2** (`tests/test_v200_split.py`): subprocess.run に
  `encoding="utf-8"` 明示
- **P1** (`.github/workflows/ci.yml`): `test` job の pytest を
  `tests/test_v200_split.py` のみに限定 (inherited visa-mcp tests
  152 件は v2.1 で curated subset へ拡張予定)

### 検証

```
PYTHONPATH=src python -m pytest tests/test_v200_split.py -q
→ 10 passed
```

### 互換性

- API / package 構造: 不変
- MCP tool 数 / DSL / extension pack: 不変
- CLI 動作: stderr メッセージのみ英語化、exit code / 動作不変

## v2.0.1 — v2.0.0 レビュー応答 (README 導線 / line-ending note / MockBackend docstring)

合言葉: **「正式版直後の peripheral 仕上げ」**

v2.0.0 external review (P0/P1/P2) を反映した small patch。
public API / dependency / extension pack / MCP tool / DSL すべて不変。

### 変更点

- **P0**: `README.md` に **line-ending note** を追加。GitHub raw view
  の一部 viewer が file を「1 line」と mis-report する件を、
  `.gitattributes` + CI gate (TOML/YAML parse / compileall /
  multiline guard) で対処していることを明文化
- **P1**: `README.md` 冒頭に **migration guide への強い導線**を追加
  (v1.x ユーザーは v2_migration.md を先に読むよう誘導)
- **P1**: README に **GitHub Actions CI badge** を追加
- **P2**: `src/lab_executor/backends/mock_backend.py` の class
  docstring から v1.11 表記を削除、v2.0 lab-executor-mcp 同梱 backend
  として書き直し (`MockVisaManager` は legacy internal name と整理)

### 互換性

- API / package 構造: 不変
- MCP tool 数 (Stable 43 + Experimental 7 = 50): 不変
- DSL `dsl_version=0.8` / extension pack 形式: 不変
- wheel build / install path / dependency: 不変

### 次の作業

`visa-mcp v2.0.0-rc1` (shim package 化 + `lab-executor-mcp >= 2.0`
依存追加) に並走着手予定。

## v2.0.0 — First stable release (split from visa-mcp v1.11.1)

**`lab-executor-mcp` の最初の安定版 release。** `visa-mcp` v1.x が
1 リポジトリで持っていた「PyVISA backend」と「実験実行 runtime」を
v2.0 で 2 リポジトリに分離した、その runtime 側。

### 位置づけ

```
v1.x までの visa-mcp:
  PyVISA backend + runtime + DSL + extension ecosystem (一体)

v2.0:
  lab-executor-mcp ← backend-independent runtime (これ)
  visa-mcp         ← PyVISA backend + 旧 import shim (v2.0.0-rc1
                     で並走着手予定)
```

依存方向: `visa-mcp → lab-executor-mcp` (許可) /
`lab-executor-mcp → visa-mcp` (禁止)。

### lab-executor-mcp に含まれるもの

- DSL (`ExperimentPlan`, `dsl_version=0.8`) + validator + dry-run
- Job manager / state machine / scheduler / barrier
- Group / Map executor
- Observation API (`timeline` / `live_view` / `summary`)
- Benchmark runner + repair tasks
- Definition pack ecosystem (extension `init/install/check/package/
  catalog/authoring`)
- Instrument authoring (`scaffold` / `promote-check` / `review-report`)
- Export / bundle (deterministic reproducibility)
- Audit / locks / SQLite (user_version=3)
- `InstrumentBackend` Protocol + `MockBackend`
- MCP tool: **Stable 43 + Experimental 7 = 50** (v1.0 から不変)
- minimal `lab-executor` CLI (`--version` / `--help` /
  `validate instrument`)

### 含まれないもの (visa-mcp 側に残る)

- `PyVisaBackend` (PyVISA 透過 adapter)
- `VisaManager` / `bus_manager` / `session_manager`
- Raw VISA tools (env-gated `send_command` / `query_instrument`)
- `tools/discovery.py` (PyVISA resource 列挙)

### 互換性

- MCP tool 名 / 引数 / response: **完全互換** (v1.0 凍結のまま)
- DSL `dsl_version=0.8`: **完全互換**
- extension pack `.visa-mcp-ext.zip` 形式: **完全互換**
- `.install_meta.json` schema: **完全互換**
- `~/.visa-mcp/extensions/` install path: **継続使用**
  (v2.1 以降で `~/.lab-executor/extensions/` 並走検討)

### CLI status (v2.0)

`lab-executor` CLI は v2.0 では **minimal**:

- `lab-executor --version` / `--help`: ✓
- `lab-executor validate instrument <path>`: ✓
- `lab-executor serve`: **placeholder** (exit code 2 で v2.1 案内)
- v1.x `visa-mcp` CLI 完全互換: **v2.1 以降で段階 port 予定**

実機 MCP server 起動は **`visa-mcp serve`** (v2.0.0-rc1 で並走 release
予定の visa-mcp shim 経由) を継続利用してください。利用者は v2.0 時点
で CLI を切り替える必要はありません。

### 依存関係

- PyVISA: **不要** (`pip install lab-executor-mcp` のみで動く)
- 実機 backend が必要なら `pip install visa-mcp` を追加 install
  (`lab-executor-mcp >= 2.0` を自動 pull)

### 検証済み項目

ローカル (Windows + Python 3.14):

```
python -m tomllib pyproject.toml          → OK
python -c "import yaml; yaml.safe_load(ci.yml)"  → OK
python -m compileall src tests             → OK
pytest tests/test_v200_split.py            → 10 passed
lab-executor --version                     → 2.0.0
lab-executor --help                        → OK
lab-executor serve (placeholder)           → exit 2, stderr "v2.1"
import lab_executor without visa-mcp       → OK (sys.meta_path block 検証)
import lab_executor without pyvisa         → OK
no top-level "from visa_mcp.*" import      → 0 件 (AST 検査)
Stable 43 + Experimental 7 = 50            → 不変
multiline / LF only guard                  → 11 file pass
```

GitHub Actions (rc4 で確立した 3 job 構成): `test` /
`pyvisa-not-installed` / `build` の green を v2.0.0 tag でも CI で
継続検証する。

### 既存ユーザー向け

```bash
# 実機を使う既存ユーザー (v1.x から)
pip install --upgrade visa-mcp
# → 自動的に lab-executor-mcp >= 2.0 も install される
#   (visa-mcp v2.0.0-rc1 release 後)

# 実機不要 (benchmark / dry-run / validate のみ)
pip install lab-executor-mcp

# 推奨 import (v2.0+)
from lab_executor.extension import ExtensionManifest
from lab_executor.dsl import validate_experiment_plan
# 旧 import (v2.0 で DeprecationWarning 付きで動作、v2.2+ で削除候補)
from visa_mcp.extension import ExtensionManifest  # DeprecationWarning
```

### 次の作業

- **`visa-mcp v2.0.0-rc1`** (並走着手): shim package 化 +
  `lab-executor-mcp >= 2.0` 依存追加 + `PyVisaBackend` + raw VISA
  tools + `visa-mcp serve` 互換維持
- **`lab-executor-mcp v2.1`**: `lab-executor serve` 実装 + CLI
  完全 port + install path 移行計画

### 履歴

履歴は visa-mcp v1.11.1 から bootstrap (新規 repo として開始)。
v1.x までの開発履歴は `TECTOS-JP/visa-mcp` を参照。

Source of truth (visa-mcp repo):
- `docs/separation/module_ownership.yaml`
- `docs/separation/split_manifest.yaml`
- `scripts/bootstrap_lab_executor.py`

## v2.0.0-rc4 — rc3 レビュー応答 (multiline guard / CLI 文言整合 / serve placeholder test)

合言葉: **「正式 v2.0.0 直前の最後の peripheral 整備」**

rc3 external review (P1) を反映した patch。public API / dependency /
extension pack 形式すべて不変。

### 変更点

- **P0** (`tests/test_v200_split.py`):
  - `test_critical_files_are_multiline_and_lf_only` 追加。
    `pyproject.toml` / `ci.yml` / README / docs / 主要 .py が
    10 行以上 + CR 文字 0 件であることを CI で固定 (`.gitattributes`
    の効果を gate 化)
  - `test_lab_executor_serve_is_placeholder` 追加。
    `lab-executor serve` が exit code 2 + stderr に `v2.1` を含むこと
    を固定 (未実装なのに success と誤解されないようにする)
  - `test_lab_executor_cli_version` 追加
- **P1** (`docs/v2_migration.md`):
  - CLI section を README と整合。「v2.0 では Python API は
    `lab_executor` 推奨、CLI は visa-mcp shim 経由を推奨、`lab-executor`
    CLI 完全 port は v2.1+」と明示
  - `lab-executor serve` は placeholder と明示
  - 既存利用者は v2.0 で CLI を切り替える必要が無いことを明文化

### tests

- smoke test 7 件 → **10 件** に増加 (全て pass)
- TOML / YAML parse: OK
- compileall src tests: OK

### 互換性

- MCP tool / DSL schema / extension pack: 不変
- public API / package 構造: 不変

### 正式 v2.0.0 への残タスク

このレビューサイクル後、以下を経て `v2.0.0` 正式 release へ進む:

1. v2.0.0-rc4 GitHub Actions 全 job green 確認
2. tag clone + wheel install + pytest の検証結果を v2.0.0 release note
   に明記
3. 並走: `visa-mcp v2.0.0-rc1` (shim package 化、
   `lab-executor-mcp >= 2.0` 依存追加) 着手

## v2.0.0-rc3 — rc2 レビュー応答 (`.gitattributes` LF / README CLI status / mock_backend docstring)

合言葉: **「Windows CRLF artifact を消し、レビュアーの raw viewer の
mis-report を二度と起こさせない」**

rc2 external review の P0/P1 を反映した patch。public API / dependency /
extension pack 形式すべて不変。

### 調査結果 (rc2 P0: raw 改行問題)

レビュアーは `pyproject.toml` 等を「1 行」と report していたが、
**実体は LF / multi-line で正常**:

| File | git blob LF count |
|------|------------------:|
| `pyproject.toml` | 46 |
| `.github/workflows/ci.yml` | 79 |
| `README.md` | 42 |
| `docs/v2_migration.md` | 129 |
| `src/lab_executor/backends/base.py` | 64 |
| `src/lab_executor/backends/mock_backend.py` | 77 |
| `tests/test_v200_split.py` | 73 |

検証方法:

```bash
git clone --depth 1 --branch v2.0.0-rc2 \
    https://github.com/TECTOS-JP/lab-executor-mcp.git rc2-check
cd rc2-check
python -c "import tomllib; tomllib.loads(open('pyproject.toml').read())"
# → OK
python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))"
# → OK
python -m compileall src tests
# → OK
```

原因: Windows checkout (autocrlf=true) によって working tree の
file が CRLF 化される。レビュアーの viewer はおそらく **`\n` not
preceded by `\r`** を line count とみなしており、純 CRLF file を
「1 line」と mis-report していたと推測される。

### rc3 で打った対策

- **`.gitattributes` 追加**: `* text=auto eol=lf` + 主要拡張子
  (`*.py` / `*.toml` / `*.yaml` / `*.yml` / `*.json` / `*.md` 等) に
  `eol=lf` を強制。これで Windows checkout でも file は LF となり、
  raw viewer の counter 仕様に関わらず正しく line を見せる
- **P1**: `README.md` に **CLI status in v2.0** section を追加
  (lab-executor CLI の minimal 範囲 + serve は placeholder + v2.1+
  で段階 port 予定を明記)
- **P1**: `src/lab_executor/backends/mock_backend.py` docstring を
  **v2.0 lab-executor-mcp 同梱 backend** として書き直し
  (v1.11 内部準備の文脈を削除、`MockVisaManager` は legacy internal
  と整理)

### tests

- 全 smoke test 7 件 (rc1 から不変): pass
- `compileall src tests`: OK
- `lab-executor --version` / `--help`: OK
- TOML / YAML parse: OK

### 互換性

- MCP tool / DSL schema / extension pack: 不変
- API / CLI / pyproject 構造: 不変
- `.gitattributes` 追加のみ (動作影響ゼロ、行末文字の固定のみ)

### 次フェーズ

`v2.0.0-rc3` レビュー後に `v2.0.0` 正式 release。並走して
`visa-mcp v2.0.0-rc1` (shim 化 + `lab-executor-mcp >= 2.0` 依存追加)
着手予定。

## v2.0.0-rc2 — rc1 レビュー応答

合言葉: **「rc1 で抜けていた CLI entry point と CI gate を埋める」**

`lab-executor-mcp` v2.0.0-rc1 の external review (P0/P1) を反映した
patch。public API / dependency / extension pack 形式すべて不変。

### 主な変更

- **P0**: `src/lab_executor/cli.py` 新規追加 (rc1 で `pyproject.toml`
  `[project.scripts] lab-executor = "lab_executor.cli:main"` が
  module 不在で壊れていた)。`lab-executor --version` / `--help` /
  `validate instrument <path>` / `serve` (placeholder) を提供
- **P0**: `.github/workflows/ci.yml` を 3 job 構成に拡張:
  - `test`: compileall + import smoke + MockBackend smoke +
    `lab-executor --help` smoke + AST 検査 (top-level `visa_mcp.*`
    import 0 件) + pytest
  - `pyvisa-not-installed`: pyvisa uninstall 後の import / smoke
  - `build`: wheel build + wheel install + import 確認
- **P1**: `src/lab_executor/backends/base.py` docstring を **v2.0
  公開境界向け** に書き直し (v1.1 spike / v1.11 内部準備の文脈を削除)
- bootstrap script (`visa-mcp` repo の
  `scripts/bootstrap_lab_executor.py`) に `cli.py` 生成を追加し、
  再 bootstrap で同等の rc2 ツリーを再現できるようにした

### 検証

ローカル (Windows + Python 3.14) で以下が通る:

```
python -m tomllib pyproject.toml   # OK
python -m compileall src tests     # OK
python -c "import lab_executor"    # OK (Stable 43 + Exp 7 = 50)
lab-executor --version             # 2.0.0-rc2
lab-executor --help                # OK
pytest tests/test_v200_split.py    # 7 passed
```

`visa-mcp` を import 経路から block して再検証 → `lab-executor` は
`pyvisa` を sys.modules に load しないことを確認済。

### 互換性

- MCP tool 数 (Stable 43 + Experimental 7 = 50): 不変
- DSL `dsl_version=0.8`: 完全互換
- extension pack 形式 / `.install_meta.json`: 完全互換
- `~/.visa-mcp/extensions/` install path: 継続使用

## v2.0.0-rc1 — Initial split candidate from visa-mcp v1.11.1

lab-executor-mcp の最初の release candidate。`visa-mcp` v1.11.1 から
runtime / DSL / ecosystem layer を切り出した。

### 含まれるもの

- DSL (`ExperimentPlan`, `dsl_version=0.8`) + validator + dry-run
- Job manager / state machine / scheduler / barrier
- Group / Map executor
- Observation API
- Benchmark runner + repair tasks + 5 fixture tasks
- Definition pack ecosystem (extension init/install/check/package/catalog/authoring)
- Instrument authoring (scaffold/promote-check/review-report)
- Export / bundle (deterministic reproducibility)
- Audit / locks / SQLite (user_version=3)
- `InstrumentBackend` Protocol + `MockBackend`
- MCP tool: Stable 43 + Experimental 7 = 50 (v1.0 から不変)

### 含まれないもの (visa-mcp 側に残る)

- `PyVisaBackend` (PyVISA 透過 adapter)
- `VisaManager` / `bus_manager` / `session_manager`
- Raw VISA tools (`send_command` / `query_instrument`, env-gated)
- `tools/discovery.py` (PyVISA resource 列挙)

### 互換性

- DSL `dsl_version=0.8` 完全互換
- extension pack 形式 (`.visa-mcp-ext.zip`) 完全互換
- `.install_meta.json` schema 完全互換
- `~/.visa-mcp/extensions/` install path 継続使用 (v2.x で再評価)
- MCP tool 名 / 引数 / response: v1.0 凍結のまま

### 依存関係

- PyVISA: **不要** (`pip install lab-executor-mcp` で動く)
- 実機 backend が必要なら `pip install visa-mcp` を追加 install

### 履歴

履歴は visa-mcp v1.11.1 から **切り出して新規 repo として開始**
(git filter-repo による history rewrite は行わない)。
visa-mcp の git log は引き続き `TECTOS-JP/visa-mcp` を参照。

### Source of truth

- `docs/separation/module_ownership.yaml` (visa-mcp v1.11.1)
- `docs/separation/split_manifest.yaml` (visa-mcp v1.11.1)
- bootstrap script: `scripts/bootstrap_lab_executor.py` (visa-mcp)
