# Job モデル (v0.5.0 / v0.5.1)

長時間 Recipe をバックグラウンドで実行・追跡・キャンセルできる非同期 Job 基盤。
LLM のツール呼び出しをブロックせず、複数の実験を並行管理できる。

v0.5.1 で **polling wait** (条件待機 / 安定待機 / 絶対時刻待機) と
専用 MCP ツール `start_wait_job` を追加。詳細は本ドキュメント末尾。

## 概要

```
LLM → start_recipe_job ── 即座に job_id 返却 ────┐
                                                  │ バックグラウンドで recipe を実行
                                                  ▼
                                          [queued → running → waiting → completed]
                                                                       → failed
                                                                       → cancelled
                                                                       → timeout
                                                                       → interrupted
LLM → get_job_status / get_job_result ─── 状態と結果を取得
LLM → cancel_job (mode=safe_shutdown) ─── 安全に停止
```

## Job 状態機械

| 状態 | 意味 |
|------|------|
| `queued` | 登録直後 or 同 resource の他 Job が running のため順番待ち中 |
| `running` | step を順次実行中 (resource を占有中) |
| `waiting` | wait step 等で待機中 (resource を占有中) |
| `completed` | 全 step 成功で完了 |
| `failed` | エラー (機器 / 安全制約 / 検証 / 内部 / blocked) で停止 |
| `cancelling` | cancel 要求受付済み、停止処理中 |
| `cancelled` | キャンセル完了 |
| `timeout` | `job_timeout_s` 経過で停止 |
| `interrupted` | サーバ再起動により中断 |

`queued` は **「投入直後」と「同 resource の前 Job 待ち」** の両方を表す。
`get_job_status` の data に `queue.queue_position` と `queue.blocking_job_id` が含まれていれば
後者 (queue 待ち)。なければ前者または scheduler 内部状態。

### 許可される遷移

```
queued      → running / failed / cancelling / cancelled / interrupted
running     → waiting / completed / failed / cancelling / timeout / interrupted
waiting     → running / completed / failed / cancelling / timeout / interrupted
cancelling  → cancelled / failed / interrupted
completed / failed / cancelled / timeout / interrupted  → (終端、遷移なし)
```

## タイムアウト

`start_recipe_job(..., job_timeout_s=N)` で全体実行制限を指定。
- 未指定 / `0.0` → デフォルト 24 時間
- 経過すると Job は自動で `timeout` 状態に遷移
- 各 step の境界 + wait の 200ms スライス毎に check

## キャンセル

`cancel_job(job_id, cancel_mode)` の `cancel_mode`：

| モード | 動作 |
|-------|------|
| `immediate` | `asyncio.Task.cancel()` で即時中断 (`CancelledError`) |
| `after_current_step` | 現在の step 完了後 or wait 中断で停止 |
| `safe_shutdown` | 機器の安全停止 (`set_output OFF`, `set_voltage 0`) を試みてから停止 |

WaitStep は 200ms スライス毎に cancel チェックするので、長時間 wait 中もすぐに反応する。

## 再起動セマンティクス

サーバ起動時、SQLite 上で `queued` / `running` / `waiting` / `cancelling` だった Job はすべて `interrupted` に遷移する (v0.5.0.2+)。
`queued` も再開しない理由は、再起動後の実機状態と queue 状態を保証できないため。

**自動再開は v0.5.0 では非目標**。LLM は `get_job_result` で過去ジョブの履歴と中断状態を確認できる。
v0.9.0 以降で step idempotent + checkpoint による本格的な resume を計画。

## 標準レスポンス形式

すべての Job 系ツールは `response_envelope` 形式で返す：

```json
{
  "status": "ok" | "error" | "partial_failure" | "running",
  "data": { ... },
  "errors": [
    {
      "error_class": "timeout" | "safety" | "validation" | "not_found" | "hardware" | "internal",
      "message": "...",
      "recoverable": true,
      "recommended_next_actions": [
        {
          "action": "retry",
          "tool": "start_recipe_job",
          "args": { ... },
          "reason": "..."
        }
      ]
    }
  ],
  "metadata": {
    "timestamp": "2026-...",
    "elapsed_s": 0.123,
    "job_id": "job_..."
  }
}
```

## `recommended_next_actions` (LLM 向け次手提示)

終端状態が `failed` / `timeout` / `cancelled` / `interrupted` の場合、
`get_job_result` のレスポンスに次手候補が構造化されて含まれる。

例: `timeout` で終わった Job：

```json
{
  "status": "error",
  "errors": [
    {
      "error_class": "timeout",
      "recommended_next_actions": [
        {
          "action": "retry",
          "tool": "start_recipe_job",
          "args": {
            "resource_name": "...",
            "recipe_name": "...",
            "parameters": {...},
            "job_timeout_s": "<より大きな値>"
          },
          "reason": "より長い job_timeout_s で再実行する"
        },
        {
          "action": "inspect_state",
          "tool": "get_job_result",
          "reason": "どこで時間切れになったか steps_executed で確認"
        },
        {
          "action": "safe_shutdown",
          "reason": "機器が中途半端な状態の可能性。次の操作前に出力 OFF を確認"
        }
      ]
    }
  ]
}
```

エラークラスごとの推奨：

| error_class | 主な action |
|------------|------------|
| `timeout` | retry (job_timeout_s 拡張) / inspect_state / safe_shutdown |
| `safety` | review_safety_constraints / retry_with_override |
| `validation` | fix_parameters |
| `not_found` | list_recipes / list_resources |
| `hardware` / `protocol` / `internal` | retry / inspect_state |

interrupted (再起動)：

| action | 説明 |
|--------|------|
| `inspect_state` | last_completed_step を確認 |
| `safe_shutdown` | 機器の状態が不明なので安全停止 |
| `resume_from_step` | v0.9.0+ で実装予定 |

## 永続化

`~/.visa-mcp/state.sqlite` (環境変数 `VISA_MCP_STATE_DB` で変更可) に jobs テーブルを保持。
SQLite WAL モードでスレッドセーフ。

スキーマ (v0.5.0 最小版)：

```sql
CREATE TABLE jobs (
    job_id              TEXT PRIMARY KEY,
    owner               TEXT,
    resource_name       TEXT,
    recipe              TEXT,
    parameters_json     TEXT,
    status              TEXT,
    current_step_index  INTEGER,
    error_class         TEXT,
    last_step_summary   TEXT,
    result_json         TEXT,
    created_at          TEXT,
    updated_at          TEXT
);
```

v0.7.0 で完全永続化 (`job_steps` / `measurement_cache` / `locks` / `monitor_data`) を追加予定。

## MCP ツール (5 個)

### `start_recipe_job`

Recipe を Job として登録、即座に job_id を返す。

```
start_recipe_job(
    resource_name: str,
    recipe_name: str,
    parameters: dict = {},
    owner: str = "",
    override_safety: bool = False,
    override_reason: str = "",
    job_timeout_s: float = 0.0,   # 0 → デフォルト 24h
)
```

### `get_job_status`

短いステータスのみ取得 (頻繁なポーリング向け)。

```
get_job_status(job_id: str)
```

返却 `data`: `status / current_step_index / last_step_summary / error_class / created_at / updated_at / is_terminal`

### `get_job_result`

完了 Job の完全な結果 (`steps_executed`) を取得。実行中なら `status: "running"` を返す。
終端エラー時は `recommended_next_actions` を含む。

```
get_job_result(job_id: str)
```

### `list_jobs`

新しい順で Job 一覧を返す。

```
list_jobs(
    status_filter: list = None,   # ["running", "completed"] 等
    owner: str = "",
    limit: int = 50,
)
```

### `cancel_job`

```
cancel_job(
    job_id: str,
    cancel_mode: str = "after_current_step",   # immediate / after_current_step / safe_shutdown
    timeout_s: float = 30.0,
)
```

## 環境変数

| 変数 | 既定値 | 用途 |
|------|--------|------|
| `VISA_MCP_STATE_DB` | `~/.visa-mcp/state.sqlite` | Job メタデータの SQLite パス |
| `VISA_MCP_SAFETY_MODE` | `strict` | 安全モード (Job 経由でも適用) |
| `VISA_MCP_AUDIT_LOG` | `~/.visa-mcp/audit.log` | 監査ログ |

## 設計上の注意

- Job の `running` / `waiting` 中の状態は SQLite に同期されているが、コルーチンが落ちると interrupted 扱いになる
- **同一 resource への並列 Job は `ResourceScheduler` により Job 単位で直列化される** (v0.5.0.2+)
  - 後続 Job は `queued` 状態で順番待ちになる (`queue_policy="queue"` デフォルト)
  - `queue_policy="reject_if_busy"` を指定すれば busy 時に即 `failed` を返す
  - running / waiting 中は **Job 終了まで** resource を占有する (wait 中の interleave を防ぐため)
  - `VisaManager` の低レベル lock は VISA 通信 1 回ごとの排他として依然有効
- 複数 Job のキャンセル順序: queued Job の `cancel_job` は scheduler queue から取り除いて
  即 cancelled に。running 中の Job は通常の cancel_mode (immediate / after_current_step /
  safe_shutdown) に従う
- Job 内の各 step は既存の安全制約 (`safety`) 検証を通る (Job だからといって緩くならない)
- 監査ログ (`~/.visa-mcp/audit.log`) も従来通り記録される

---

## v0.5.1: Polling wait

長時間にわたって測定値を監視し、条件を満たすまで / 値が安定するまで待つステップ。
Recipe 内の step、または `start_wait_job` MCP ツール経由で単発 Job として起動できる。

### Step 型

| step type | 用途 | 必須キー |
|-----------|------|---------|
| `wait_until` | 絶対時刻 / 相対秒数まで待つ | `timestamp` または `seconds_from_now` |
| `wait_for_condition` | `condition_expr` が True になるまで polling | `instrument`, `command`, `condition_expr` |
| `wait_for_stable` | `max - min <= tolerance` (window 内) で安定 | `instrument`, `command`, `tolerance`, `window_s` |

共通オプション (polling 系):

```
interval_s: 1.0          # poll 間隔
timeout_s: 60.0          # 条件全体の制限
command_timeout_s: null  # 1 query の VISA timeout (null=command 定義値)
value_path: null         # parsed response の数値フィールド名
retry_on_error: 1        # 1 polling 失敗時の即時 retry 回数
max_consecutive_errors: 3 # 連続失敗許容数 (超過で step failed)
```

### Recipe 例

```yaml
recipes:
  voltage_then_stable_temp:
    steps:
      - { command: "set_voltage", args: { voltage: 5 } }
      - wait_for_stable:
          instrument: "TEMP::INSTR"
          command: "measure_temperature"
          tolerance: 0.2
          window_s: 60
          interval_s: 5
          timeout_s: 1800
          value_path: "temperature"
      - { command: "measure_current", result_as: "i" }
```

このとき Job は **PSU + TEMP::INSTR の両方の resource** を占有する。
他の Job が同じ resource を取ろうとすると queued になる。

### condition_expr の文法

許可: 変数 `value`、数値リテラル、比較 (`< <= > >= == !=`)、論理 (`and / or / not`)、
算術、`abs(...)`。禁止: 属性アクセス、関数呼び出し全般 (`abs` を除く)、import、indexing、
文字列操作、代入、内包表記、lambda。

```
value > 80
abs(value - 25) < 0.2
value > 10 and value < 20
```

### wait_for_stable の安定判定

```
最新サンプル時刻から window_s 内に min_samples (デフォルト 3) 以上のサンプルがあり、
max(window samples) - min(window samples) <= tolerance
```

method は v0.5.1 では `"range"` のみ。`tolerance` は片側ではなく **幅** (`max - min`) として
比較される (`±0.2` ではなく `0.2`)。

### MCP ツール: `start_wait_job`

```
start_wait_job(
    wait_type: "seconds" | "until" | "condition" | "stable_value",
    params: {...},
    owner: str = "",
    job_timeout_s: float = 0,
    queue_policy: "queue" | "reject_if_busy" = "queue",
)
```

- `seconds` / `until`: resource を取らない (即起動)
- `condition` / `stable_value`: `params.instrument` を resource lock 対象に含む

### `get_job_status` の polling 進捗

polling 中の Job は `data.polling` フィールドを含む:

```json
{
  "data": {
    "status": "waiting",
    "polling": {
      "step_type": "wait_for_stable",
      "instrument": "TEMP::INSTR",
      "elapsed_s": 42.1,
      "timeout_remaining_s": 1757.9,
      "sample_count": 8,
      "last_value": 25.31,
      "current_delta": 0.18,
      "tolerance": 0.2,
      "stable": false,
      "next_poll_in_s": 2.4
    }
  }
}
```

### `polling_safe` フラグ (機器定義側)

```yaml
commands:
  measure_temperature:
    scpi: "MEAS:TEMP?"
    type: "query"
    polling_safe: true   # 副作用無く繰り返し呼んで良い query
```

`polling_safe: false` の command を polling step に使うと結果 dict に
`polling_safe_warning` 文字列が付与される (実行はブロックしない、警告のみ)。

### 設計上の注意 (v0.5.1)

- polling sleep は `POLL_SLEEP_SLICE_S = 0.2s` 単位でスライス → cancel / timeout に即応
- 開始直後 (`t=0`) に 1 回目の measurement を取る (エージェントが即進捗を知れる)
- 多重 resource lock は **canonical sorted 順** で取得 (deadlock 回避)
- 単位変換は **しない** (`MEAS:TEMP?` が "25.3" を返したらそのまま 25.3 として評価)
- timeout は 3 階層 (`command_timeout_s < timeout_s < job_timeout_s`)、混同しない
