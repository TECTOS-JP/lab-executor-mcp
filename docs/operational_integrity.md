# Operational integrity (audit + locks, v0.9.3 experimental)

合言葉:「実験を実行できるだけでなく、誰が・いつ・何を・どの resource に
対して行い、なぜ失敗 / 拒否されたかを後から追えるようにする」

## auditテーブルの位置づけ

| テーブル | 役割 | 例 |
|----------|------|---|
| `job_events` | Job 内部の進行記録 | step_started / step_completed / barrier / polling_progress |
| `audit` | **外部から見た操作 / 安全拒否 / 運用記録** (新規 v0.9.3) | tool_called / lock_blocked / safety_blocked / export_created / server_started |

**両者を同じイベントで重複させない**。`step_started` は audit に不要、
`safety_blocked` は job_events だけでなく audit にも残す。

## 記録される event_type (v0.9.3 MVP)

P0 (実装済):

- `server_started` (server 起動時、stale lock 解放数を metadata に記録)
- `job_started` / `job_failed` (`start_experiment_job`)
- `job_cancelled` (`cancel_job`)
- `resume_started` (`resume_job` で新 Job 作成成功時)

P1 (v0.9.3.x 以降):

- `tool_called` / `tool_completed` / `tool_failed` (全 MCP tool 一律 hook)
- `safety_blocked`
- `resource_lock_acquired` / `_blocked` / `_released`
- `export_created`
- `validate_plan_failed` / `dry_run_warning`

## redaction (sensitive keys + 大量データ)

`summarize_for_audit(payload)` が以下を自動適用:

| 入力 | 変換 |
|------|------|
| `len > 200` の文字列 | `{"_truncated": True, "len": N, "head": "..."}` |
| `len > 5` の list | `{"_truncated_list": True, "len": N, "head": [...]}` |
| key に `token` / `api_key` / `password` / `secret` / `authorization` / `credentials` を含む | `[REDACTED]` |
| 深さ 6 超 | `"<deep>"` |

raw SCPI 応答全文 / 大量測定値 / file 内容 / credentials などは保存されない。

## `query_audit` (MCP tool)

```text
query_audit(
  job_id="", resource="", owner="",
  event_type="", severity="",
  since="", until="",
  limit=200, cursor=None,
  include_details=False,
)
```

- limit 上限 5000
- cursor は **`{timestamp, audit_id}` 複合** で同一 timestamp 取りこぼし対策
  (timeline cursor と同設計、v0.8.2.1 のレビュー指摘準拠)
- `include_details=true` で `request_summary` / `response_summary` /
  `metadata` も同梱 (default false で応答軽量)

返却:

```json
{
  "data": {
    "events": [{ "audit_id": "aud_...", "timestamp": "...",
                 "event_type": "job_started", "severity": "info",
                 "owner": "agent_a", "job_id": "...", ... }],
    "pagination": {
      "limit": 200, "returned": N, "has_more": false,
      "next_cursor": {"timestamp": "...", "audit_id": "aud_..."} | null
    },
    "include_details": false
  }
}
```

## locks テーブル

```sql
CREATE TABLE locks (
  resource TEXT PRIMARY KEY,
  owner TEXT NOT NULL,
  job_id TEXT,
  client_id TEXT,
  acquired_at TEXT NOT NULL,
  lease_until TEXT,      -- NULL 可 (lease 無し)
  lock_reason TEXT,
  metadata_json TEXT
);
```

- **lease_until** で stale 検出 (lease 過ぎは `stale=True`)
- server 起動時に自動で stale lock を解放 (`server_started` event の
  `metadata.stale_locks_released` に件数を記録)
- AuditStore に `acquire_lock` / `release_lock` / `list_locks` /
  `release_stale_locks` の helper を実装

v0.9.3 MVP では既存 `ResourceScheduler` (in-memory) と並行存在。永続 lock
への完全統合は v1.0 で検討。

## `list_locks` (MCP tool)

```text
list_locks(resource="", owner="", include_stale=True)
```

各 lock の `stale` フィールドで lease 切れを判定。AI エージェントは
blocked response の `blocked_by` 情報と組み合わせて `cancel_job` /
`wait_and_retry` を判断する。

## owner / client_id / job_id

| 識別子 | 範囲 |
|--------|------|
| `owner` | ユーザー / エージェントが指定する論理所有者 (free string) |
| `client_id` | MCP 接続元 / セッションを識別する ID (v0.9.3 では未取得、reserved) |
| `job_id` | Job 単位の実行 ID |

`audit` テーブルの各 row にこれら 3 つを記録 (空可)。複数エージェント運用時
の追跡に必須。

## 新規 `error_class`

| クラス | 意味 | 既存 `blocked` との関係 |
|--------|------|----------------------|
| `lock_conflict` | resource lock が他 owner に保持されている | `blocked` の詳細種別。v0.9.3 では reserved (実際の MCP response には引き続き `blocked` が出る) |
| `lock_stale` | 自 lock の lease が切れていた | 再取得が必要 |
| `audit_query_failed` | query_audit の内部 error | 通常は `internal` |

v1.0 で `error_class=blocked` + `details.reason=lock_conflict` に統一するか、
独立 class とするかを決定する。

## retention

v0.9.3 では **自動削除なし**。将来検討:

```yaml
audit:
  retention_days: 30     # 未実装
  max_rows: 100000       # 未実装
```

`query_audit` を運用者が定期呼び出しして必要分だけエクスポートし、SQLite を
`VACUUM` する運用を推奨 (v0.9.x 期間)。

## Lock source of truth (v0.9.3.1 明記)

v0.9.3 時点で、resource lock の **source of truth は二重に存在**:

| Lock | 役割 | 利用箇所 |
|------|------|---------|
| `ResourceScheduler` (in-memory) | Job 実行中の resource 排他 / queue / 即時 blocked 判定 | 既存 Job 起動経路 (`start_recipe_job` / `start_experiment_job` 等) |
| `locks` テーブル (SQLite) | 監査記録・長期的可視化・stale lock 検出 | `query_audit` / `list_locks` / 起動時 `release_stale_locks` |

**v1.0 までに決める方針** (open question として記録):

- どちらが lock 取得の primary path か
- blocked response が両方を参照するか / 片方に統一するか
- ResourceScheduler の lock 状態を `locks` テーブルへ自動 mirror するか
- stale lock 自動解放と Job status の連動 (interrupted Job の lock は
  自動 stale 扱いとするか)

v0.9.3.1 では並行存在を許容し、`list_locks` は SQLite 側のみを返す。
ResourceScheduler の lock 競合は引き続き従来通り `blocked` で返る。

## Stale lock の定義と解除条件 (v0.9.3.1 明記)

**Stale 判定**:

- `lease_until IS NOT NULL` かつ `lease_until < now()`

**自動解放のタイミング**:

- サーバ起動時 (`AuditStore` init 直後) に `release_stale_locks()` が
  `lease_until < now()` の全 lock を DELETE
- 解放件数は `server_started` event の `metadata.stale_locks_released` に記録
- 個別の `lock_released` event は v0.9.3 では発火しない (v1.0 候補)

**Job status との連動** (v1.0 候補):

- 現状: lease 切れのみで判定
- 将来: Job が interrupted / failed / cancelled 終端した時点で対応 lock を
  自動 stale 化 + release_stale_locks 経路に乗せる

**手動上書き**: stale な lock がある resource に対しては、
`acquire_lock(resource, owner=new_owner)` が成功 (既存 stale を DELETE +
新規 INSERT)。`list_locks(include_stale=False)` で表示から除外できる。

## 監査対象 tool の範囲 (v0.9.3.1 明記)

| Tool / イベント | v0.9.3 で audit 記録 |
|----------------|--------------------|
| `start_experiment_job` (成功 / 失敗) | ○ (`job_started` / `job_failed`) |
| `cancel_job` | ○ (`job_cancelled`) |
| `resume_job` | ○ (`resume_started`) |
| サーバ起動 / stale lock 解放 | ○ (`server_started`) |
| `start_recipe_job` / `start_group_query_job` / `start_map_recipe_job` | △ (v1.0 候補) |
| `start_experiment_job_from_template` | △ (v1.0 候補、template_source は metadata に既に含まれる) |
| `export_experiment_results` | △ (v1.0 候補、`export_created` event 追加予定) |
| `validate_experiment_plan` / `dry_run_plan` の重大エラー | △ (v1.0 候補) |
| `safety_blocked` | △ (v1.0 候補) |
| `resource_lock_blocked` / `_acquired` / `_released` | △ (v1.0 候補、ResourceScheduler 統合と同時) |
| `unsafe_send_command` / `unsafe_query_instrument` | △ (v1.0 候補、有効化されている場合は必須) |

v0.9.3.1 では above の △ を将来候補としてドキュメント化のみ。実装は
v1.0 安定化フェーズで追加する。

## AuditStore 初期化失敗時 (no-op) の visibility

`JobManager.__init__` 内で `AuditStore` の初期化に失敗した場合:

1. `logger.warning` で **stderr** に警告を出力
   ("AuditStore 初期化失敗: audit / locks 機能が無効化されます")
2. `JobManager._audit = None` + `_audit_init_error = True` を保持
3. `query_audit` / `list_locks` は v0.9.3.1 から
   `error_class=internal` + `details.sub_class=audit_unavailable` を返す
4. 内部 hook (`record_event` 等) は no-op (機能継続)

これにより、AI エージェントは「audit が動いていない」状態を
レスポンスから判別できる。

## `include_details=true` の payload schema (v0.9.3.1 明記)

`query_audit(..., include_details=true)` で返る各 event は以下を含む:

```json
{
  "audit_id": "aud_...",
  "timestamp": "2026-05-22T10:00:00+09:00",
  "event_type": "job_started",
  "severity": "info",
  "owner": "agent_a",
  "client_id": null,
  "tool_name": "start_experiment_job",
  "job_id": "job_...",
  "resource": "psu001",
  "target_id": null,
  "status": "running",
  "error_class": null,
  "message": null,
  "request_summary": {
    "plan_name": "voltage_sweep",
    "queue_policy": "queue",
    "override_safety": false,
    "api_key": "[REDACTED]"
  },
  "response_summary": {
    "rows": {"_truncated_list": true, "len": 120, "head": [...]}
  },
  "metadata": {
    "template_source": {"template_name": "...", "override_keys": [...]}
  }
}
```

redaction の代表的出力形:

```json
{ "_truncated": true, "len": 512, "head": "..." }
{ "_truncated_list": true, "len": 120, "head": [...] }
"[REDACTED]"
"<deep>"
```

LLM が `include_details=true` で audit を読む場合、これらの marker を
そのまま解釈する。

## `blocked` vs `lock_conflict` v1.0 方針 (推奨)

レビュー指摘 (P1) を踏まえ、**v1.0 で以下に統一する方針**を採用予定:

```json
{
  "error_class": "blocked",
  "details": {
    "reason": "lock_conflict",
    "blocked_by": {
      "owner": "agent_b",
      "job_id": "job_456",
      "lease_until": "..."
    }
  }
}
```

理由: AI エージェントにとって「実行不能だが待てば回復可能」の大分類は
`blocked` が扱いやすい。`lock_conflict` は詳細 reason として残す。

`lock_stale` も同様に `error_class=blocked` + `details.reason=lock_stale`
に統一予定。v0.9.x 期間は両方が `error_class` として表示される可能性あり
(experimental スコープ)。

## Audit retention 方針 (v0.9.3.1 明記)

v0.9.3 / v0.9.3.1 / v1.0 では **自動削除なし**。

- DB 肥大化時は **手動削除** または **DB 再作成** (`~/.visa-mcp/jobs.sqlite`
  をバックアップしてから削除 → 起動時に schema 再作成)
- 大量データを export してから DELETE する場合:
  `query_audit` で必要分を `since` / `until` で取得 → 自前で保存 →
  `DELETE FROM audit WHERE timestamp < ?` (SQLite CLI で実行) → `VACUUM`
- v1.x で `retention_days` / `max_rows` 設定 + `purge_audit` CLI / MCP
  tool を検討

## 関連 docs

- [`docs/jobs.md`](jobs.md) — Job model
- [`docs/error_taxonomy.md`](error_taxonomy.md) — `error_class` 一覧
- [`docs/compatibility.md`](compatibility.md) — audit / locks は v1.x 内
  **experimental** スコープ
