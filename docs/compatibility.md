# 後方互換ポリシー (v1.0 正式)

このドキュメントは v1.0 時点での **正式な後方互換保証** を要約する。
詳細仕様 (Versioning policy / Deprecation policy / 凍結対象一覧) は
[`v1_stability_policy.md`](v1_stability_policy.md) を参照。

## 2 段階の互換ポリシー

レビュー指摘 (v0.7.0/v0.8.0 系) を踏まえ、ツール名すべてを一律凍結ではなく
**stable / experimental** の 2 段階に分ける:

### Stable (v1.x 互換保証対象)

v1.x 内で **名称・引数・response 構造を固定**。破壊的変更は v2.0 まで行わない。
新規 optional 引数 / フィールドの追加は許可。

### Experimental (v1.x 内で変更可、deprecated notice 必須)

新機能のスタブとして許容するが、deprecated 化する際は事前 notice を出す。

---

## Stable candidates (v1.0 時点で固定予定)

### Stable MCP tools

中核ツール (LLM の基本フロー):

- `list_resources` / `identify_instrument` / `identify_all_instruments` /
  `list_identified_instruments` / `bind_definition` /
  `list_available_definitions` / `list_commands` / `get_instrument_info` /
  `list_safety_constraints` / `validate_operation` / `reload_definitions`
- `execute_named_command` / `list_recipes` / `execute_recipe`
- **Job 系**: `start_recipe_job` / `start_wait_job` / `get_job_status` /
  `get_job_result` / `list_jobs` / `cancel_job`
- **Group/Map**: `list_groups` / `list_experiment_units` /
  `start_group_query_job` / `start_map_recipe_job`
- **DSL**: `validate_experiment_plan` / `dry_run_plan` / `start_experiment_job`
- **状態取得**: `describe_instrument` / `get_state` / `get_last_measurement`

### Stable type definitions

- **response envelope**: `{status, data, errors, metadata}` の 4 キー構造
- **Job status enum**: `queued / running / waiting / completed / failed /
  cancelling / cancelled / timeout / interrupted`
- **`error_class` taxonomy**: `docs/error_taxonomy.md` 参照
- **`current_phase` enum** (v0.8.2 新規): `queued / starting / running_step /
  waiting / polling / waiting_for_stable / barrier_wait / stagger_wait /
  monitoring / safe_shutdown / cancelling / completed / failed /
  partial_failure / interrupted / unknown`
- **timeline kind enum** (v0.8.2 新規): `job / step / target / barrier /
  stagger / verify / failure / monitor_sample / safe_shutdown`
- **severity enum**: `info / warning / error / critical`
- **DSL schema `dsl_version=0.8`**: ExperimentPlan の 10 step 種別
  (command / query / wait / wait_until / wait_for_condition /
  wait_for_stable / barrier / sweep / parallel / safe_shutdown)
- **機器 YAML schema**: `metadata` / `commands` / `recipes` / `safety` /
  `state_query` / `safe_shutdown` / `verify` 等のセクション構造
- **SQLite テーブル**: jobs / job_steps / target_runs / job_events /
  measurement_cache / monitor_data / experiment_plans / experiment_templates
  の列構造 (PRAGMA user_version で migration)
- **`job_steps.status` enum** (v0.8.2.1 明記): `started / ok / failed /
  skipped / cancelled / timeout / blocked`
  (現状の MVP 実装は `started / ok / failed` のみだが、Group/Map/barrier 拡張で
   skipped / cancelled / timeout / blocked が増える前提で凍結語彙として確保)
- **`target_runs.status` enum**: `started / ok / failed / skipped /
  cancelled / timeout / blocked`
- **`job_outcome` enum** (v0.8.2.1 新規、Observation API のみ):
  `success / partial_failure / failure / cancelled / interrupted / null(まだ終端でない)`
  Job state machine とは独立した観測値で、`job_status` から導出される。

---

## Experimental (v1.x 内で変更可能)

各ツールの docstring に `(experimental)` を明示する予定:

- **Monitor 高度機能**: `start_monitor` / `stop_monitor` / `get_monitor_data` /
  `prune_monitor_data`
- **Observation** (v0.8.2 新規): `get_experiment_timeline` /
  `get_job_live_view` / `get_job_summary`
- **Template 系**: `save_experiment_template` / `list_experiment_templates` /
  `get_experiment_template`
- **Benchmark runner** (v0.9.0 予定)
- **Job resume** (v0.9.0 予定): `resume_job`
- **Export** (v0.9.1 予定): `get_experiment_results` / `export_experiment_results`
- **Bundle** (v1.0 予定): `export_experiment_bundle` / `import_experiment_bundle`
- **Plugin 関連** (v1.2 予定)

---

## 凍結しないもの (内部実装)

- `src/visa_mcp/` 内のモジュール構造 (リファクタリング自由)
- 内部 SQLite event_type 文字列 (timeline は外部正規化を経る)
- raw payload 構造 (`include_raw=True` で取得時のみ参考)

---

## v0.8.2 時点の deprecated 候補

現在 deprecated 候補は無し。v0.9.x で発生したら本ドキュメントに追記する。

## v0.8.3.1 メモ

- `apply_template_override` を **deepcopy 化** (template 副作用防止)。
- `start_experiment_job_from_template` のレスポンス `data` に **`owner`**
  (実際に Job に反映された owner) を追加。**`override.owner` 指定時は
  関数引数 `owner` より優先**される (従来は override.owner が黙って捨て
  られていた)。動作変更だが experimental スコープのため即時反映。
- `template_source.template_version` は **DSL schema バージョンを流用**
  している現状を docs に明記。v0.9.x で template 改訂番号 (`template_revision`)
  を独立フィールドとして導入する余地を残す。
- `raw_resource_used_with_unit` は v0.8.3 では **warning** だが、v1.0 候補
  検討時に safety_mode=strict で error に昇格させる案を保留。
- `override.parameters` → `expanded_plan.variables` への shallow merge である
  ことを docs に明記。

## v0.8.2.1 メモ

外部レビューに応答した変更 (互換維持):

- `get_experiment_timeline.pagination.next_since` → `next_cursor`
  (`{timestamp, event_id}` 複合 cursor) に変更。`next_since` は **v0.8.2 のみ**
  存在し、v0.8.2.1 で **削除**された (preview API として警告済み)。
- `since` / `until` は ISO8601 datetime としてパースされる。不正値は
  `error_class="validation"` + `details.sub_class="invalid_since_timestamp"`
  (または `invalid_until_timestamp`)。
- `get_job_live_view` / `get_job_summary` が `job_outcome` を追加で返す
  (既存フィールドは不変、純粋追加)。
- `recommended_next_actions[*].action`: `inspect_state` → `inspect_job_result`
  に rename (Observation 出力のみで、experimental スコープ内なので即時変更)。

## 関連ドキュメント

- `docs/error_taxonomy.md`: `error_class` 一覧
- `docs/jobs.md`: Job モデル全体
- `schemas/*.schema.json`: JSON Schema preview (v1.0 で `v1-stable` URI に変更予定)
