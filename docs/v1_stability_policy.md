# visa-mcp v1.0 Stability Policy

このドキュメントは v1.0 で **何を凍結し、何をまだ実験中とするか** を正式に
宣言する。AI エージェントと外部ユーザーが「v1.x の期間中に依存してよい面」
を判別できるようにすることが目的。

合言葉: **v1.0 = 評価基盤の安定化** (実用全機能完成ではない)

## 1. Versioning policy

- **v1.x**: stable API は破壊的変更を行わない。新規 optional 引数 /
  フィールドの追加は許可。
- **v2.x**: stable API の破壊的変更が可能。事前に少なくとも 1 つの v1.x
  リリースで deprecated notice を出す。
- **patch** (`v1.0.x`): バグ修正・docs・experimental スコープの追加変更のみ。

## 2. Stable MCP tools (v1.x 互換保証対象)

v1.x 内で **名称・引数・response 構造を固定**。新規 optional 引数の追加は許可。

### Core instrument / resource

| ツール | 役割 |
|--------|------|
| `list_resources` | 接続中の VISA リソースを列挙 |
| `identify_instrument` | `*IDN?` で機器識別 |
| `identify_all_instruments` | 全リソースを一括識別 |
| `list_identified_instruments` | 識別済みセッション一覧 |
| `bind_definition` | `*IDN?` 非対応機器に定義を手動バインド |
| `list_available_definitions` | ロード済みの YAML 定義一覧 |
| `list_commands` | 利用可能 command 一覧 |
| `get_instrument_info` | 機器仕様・安全制約・recipes を一括取得 |
| `list_safety_constraints` | 安全制約のみ抽出 |
| `validate_operation` | 実行せず事前検証 |
| `reload_definitions` | 定義ファイル再読込 |
| `describe_instrument` | 機器能力サマリ |
| `get_state` | 機器の現在状態 |
| `get_last_measurement` | 測定値キャッシュから最新値 |

### Recipe / Job

| ツール | 役割 |
|--------|------|
| `execute_named_command` | 名前付き command を実行 |
| `list_recipes` | 利用可能 recipe 一覧 |
| `execute_recipe` | 複数 command 順次実行 |
| `start_recipe_job` | recipe を Job として登録 |
| `start_wait_job` | 単発 wait job |
| `get_job_status` | Job の現在状態 |
| `get_job_result` | 完了 Job の結果 |
| `list_jobs` | Job 一覧 |
| `cancel_job` | Job キャンセル |

### Group / Map

| ツール | 役割 |
|--------|------|
| `list_groups` | `instrument_groups` 一覧 |
| `list_experiment_units` | `experiment_units` 一覧 |
| `start_group_query_job` | グループ全機器に並列 query |
| `start_map_recipe_job` | 異なる条件で各 unit に recipe 並列実行 |

### DSL (Experiment Plan)

| ツール | 役割 |
|--------|------|
| `validate_experiment_plan` | DSL plan を検証 |
| `dry_run_plan` | 実機 I/O 無しで rendered + safety + verify summary |
| `start_experiment_job` | DSL plan を Job として実行 |
| `save_experiment_template` | 再利用可能 DSL テンプレートを保存 |
| `list_experiment_templates` | 保存済みテンプレート一覧 |
| `get_experiment_template` | 指定 name のテンプレート取得 |

### Observation

| ツール | 役割 |
|--------|------|
| `get_experiment_timeline` | Job 内の時系列イベント (cursor pagination) |
| `get_job_live_view` | 実行中 Job の集約 |
| `get_job_summary` | 完了 Job の構造化要約 |

### Monitor

| ツール | 役割 |
|--------|------|
| `start_monitor` | 機器の定期測定 Monitor Job 開始 |
| `stop_monitor` | Monitor Job 停止 |
| `get_monitor_data` | Monitor 時系列データ |
| `prune_monitor_data` | Monitor データ削除 |

### Results / Export

| ツール | 役割 |
|--------|------|
| `get_experiment_results` | Job 測定結果を JSON で少量確認 |
| `export_experiment_results` | CSV / JSONL ファイル出力 |

### Ingestion

| ツール | 役割 |
|--------|------|
| `extract_pdf_commands` | PDF からコマンド候補抽出 (★) |

> **★ `extract_pdf_commands` 保証範囲** (v1.0.1 明記):
> v1.x 内で **tool 名・引数・response 構造**は固定する。
> ただし **PDF 抽出精度 / メーカー資料ごとの成功率は保証対象外**。
> 抽出結果は YAML 草案として人間レビューを前提とする。

**合計 43 個 (stable v1.x)** ※ 実数は `src/visa_mcp/stability.py` の
`STABLE_TOOLS` を **単一 source of truth** とし、CI で整合性を確認
(`tests/test_v101_review.py`)

## 3. Experimental MCP tools (v1.x 内で変更可能)

以下は v1.x 内でも仕様変更がある可能性あり。各ツールの docstring に
`(experimental)` を明示。

| ツール | 理由 |
|--------|------|
| `start_experiment_job_from_template` | template override 仕様調整中 |
| `resume_job` | 安全性 / checkpoint / 実機状態再確認が絡む |
| `query_audit` | audit table schema の最終形が未確定 |
| `list_locks` | ResourceScheduler との source-of-truth 統合が未完了 |
| `export_experiment_bundle` (v1.0 新規) | bundle 仕様 / sha256 verify 経路が初期 |
| `validate_experiment_bundle` (v1.1 新規) | bundle 整合性 read-only 検証 |
| `inspect_experiment_bundle` (v1.1 新規) | bundle 中身要約 (analysis-only、実行・import なし) |

**合計 7 個 (experimental v1.x)** ※ 同上 (`EXPERIMENTAL_TOOLS`)

## 4. Stable schemas (v1.x 互換保証)

- **response envelope**: `{status, data, errors, metadata}` の 4 キー構造
  - `status` enum: `ok / error / running / partial_failure`
  - `errors[]` の各要素: `{error_class, message, recoverable, ...optional}`
- **Job status enum**: `queued / running / waiting / completed / failed /
  cancelling / cancelled / timeout / interrupted`
- **`error_class` taxonomy**: `docs/error_taxonomy.md` 参照
- **`current_phase` enum**: 16 種 (queued / starting / running_step /
  waiting / polling / waiting_for_stable / barrier_wait / stagger_wait /
  monitoring / safe_shutdown / cancelling / completed / failed /
  partial_failure / interrupted / unknown)
- **timeline kind enum**: 9 種 (job / step / target / barrier / stagger /
  verify / failure / monitor_sample / safe_shutdown)
- **severity enum**: `info / warning / error / critical`
- **`job_outcome` enum**: `success / partial_failure / failure / cancelled
  / interrupted / null`
- **DSL schema `dsl_version=0.8`**: ExperimentPlan の 10 step 種別
- **機器 YAML schema core**: `metadata` / `commands` / `recipes` / `safety`
  / `state_query` / `safe_shutdown` / `verify` セクション構造

## 5. Experimental schemas / fields

- **`metadata.support_level`** (機器定義): `verified` の必須条件は v1.0
  時点では **自己申告**。v1.x 内で `tested_interfaces` 非空 / 主要 command
  網羅 / safe_shutdown 必須等の強化を検討。
- **`template_source` フィールド** (experiment_plans / jobs.parameters):
  形式は将来拡張可能。
- **`audit` / `locks` テーブル**: 全列構造が experimental。
- **bundle manifest** (v1.0 新規): `bundle_version=1.0` だが内部 layout は
  v1.x 内で追加可能。

## 6. Response envelope guarantee

```json
{
  "status": "ok | error | running | partial_failure",
  "data": {},
  "errors": [
    {
      "error_class": "...",
      "message": "...",
      "recoverable": true,
      "details": {},
      "recommended_next_actions": []
    }
  ],
  "metadata": {}
}
```

**top-level `status` を増やさない方針**: `timeout` / `blocked` /
`interrupted` などは `error_class` + `details.reason` に逃がす。

## 7. Error taxonomy guarantee

v1.0 で凍結:

- 既存 `error_class` 名 (大小文字含む) と意味
- `recoverable` の判定基準
- `details.sub_class` / `details.reason` 経由の詳細種別

v1.x で許可:

- 新規 `error_class` の追加
- 既存 `error_class` への新規 detail field 追加

v1.x で禁止:

- 既存 `error_class` の意味変更 / rename / 削除

### `blocked` vs `lock_conflict` の v1.0 確定方針

`lock_conflict` は **独立 error_class ではなく `blocked` の reason**として
扱う:

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

`lock_stale` も `details.reason=lock_stale` に統一。

## 8. Deprecation policy

- Stable API を変更する場合は **最低 1 つの v1.x リリースで deprecated
  warning** を出してから v2.0 で削除
- v1.x 内では原則削除しない
- Experimental API は v1.x 内でも変更可能 (CHANGELOG への明記必須)

## 9. What is NOT guaranteed in v1.x

以下は v1.0 stable に含まれない:

> **`InstrumentBackend` Protocol** (v1.1 spike, `src/visa_mcp/backends/base.py`)
> は public import 可能だが、**stable plugin API ではない**。
> v1.1 では設計検討のための public class として公開しているのみで、
> v1.x 内で Protocol 構造 / メソッド signature の破壊的変更があり得る。
> 外部 plugin を書く場合は v1.2+ 以降の正式化を待つこと。
> 詳細は [`docs/backend_abstraction.md`](backend_abstraction.md) Open questions
> を参照。

> **Definition packs (`extension.yaml`)** (v1.2 新規) は **experimental
> extension surface**。`schemas/extension_manifest.schema.json` 構造は
> v1.x 内で追加可能 (新フィールドは backward-compatible)。
> **Executable plugin (Python code) は v1.2 では未対応**。v1.x 内で対応
> 予定なし。詳細は [`docs/extension_policy.md`](extension_policy.md) /
> [`docs/definition_packs.md`](definition_packs.md)。

> **Definition pack install (`visa-mcp extension install/list/uninstall`)**
> (v1.3 新規) は **experimental operational feature**。lockfile / install
> metadata / overlay registry の構造は v1.x 内で変更可能。
> **リモート install / signature / Python plugin は引き続き未対応**。
> 詳細は [`docs/extension_install.md`](extension_install.md) /
> [`docs/extension_registry_overlay.md`](extension_registry_overlay.md)。

> **Replay backend** は **概念整理のみ** (v1.2 では実装しない)。
> `inspect_experiment_bundle.compatibility.can_be_replayed=false` を
> 引き続き返す。詳細は [`docs/replay_backend_concept.md`](replay_backend_concept.md)。


- benchmark runner internals (`src/visa_mcp/testing/`)
- registry CLI internals (`visa-mcp validate` の出力詳細)
- audit / locks 内部スキーマ
- resume の checkpoint 仕様
- ResourceScheduler と SQLite `locks` の統合方針
- 任意の `(experimental)` 印付き機能
- 機器定義 registry の特定 entry (registry は配布物として独立進化)

## 10. v1.0 → v1.x → v2.0 のおおまかな展望

| バージョン帯 | 主テーマ (計画、変更可) |
|-------------|----------------------|
| **v1.0** | API 凍結 + 評価基盤安定化 + reproducibility bundle MVP |
| **v1.1** | 名称・リポジトリ戦略 / backend abstraction 検討 |
| **v1.2** | Plugin / Extension mechanism |
| **v1.3+** | Human intent / approval 層 |
| **v2.x** | Prometheus / WebSocket / SSE / gateway 等の運用拡張 (必要に応じて) |

## 関連 docs

- [`docs/compatibility.md`](compatibility.md) — 短縮版 (本ドキュメントの
  メタデータ寄り)
- [`docs/error_taxonomy.md`](error_taxonomy.md) — `error_class` 一覧
- [`docs/operational_integrity.md`](operational_integrity.md) — audit /
  locks の experimental スコープ
