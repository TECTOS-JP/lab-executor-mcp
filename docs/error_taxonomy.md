# `error_class` Taxonomy (v0.8.2 草案、v1.0 で凍結予定)

AI エージェントが「次の判断」を決めるために、`error_class` の意味は**安定**して
いる必要がある。v0.8.2 では候補 taxonomy を整理し、v1.0 で互換保証対象とする。

## 分類 (5 カテゴリ)

### 1. validation (入力検証エラー、実機ノータッチ)

| error_class | 発生 | recoverable |
|------------|------|-------------|
| `unknown_command` | 機器定義に存在しない command を指定 | True (修正可) |
| `unknown_instrument` | binding / alias / resource いずれにも該当しない | True |
| `unknown_binding` | DSL `$role` が plan.bindings にない | True |
| `parameter_invalid` | type / range / enum 違反 | True |
| `wrong_command_type` | query を期待した位置に write 等 | True |
| `safety_violation` | strict mode で安全制約違反 | False (override 要承認) |
| `unsupported_step_type` | DSL schema 不明な step type | True |
| `schema_invalid` | Pydantic schema 検証失敗 | True |
| `safe_shutdown_targets_empty` | DSL `safe_shutdown.targets=[]` | True |
| `parallel_placement` | parallel が top-level 末尾以外 | True |
| `parallel_inside_sweep` | sweep.body 内 parallel | True |
| `nested_parallel` | parallel.branches 内に parallel | True |
| `expanded_too_large` | sweep 展開後 step 数が上限超過 | True |
| `not_query_type` | polling/state_query で query 型でない command | True |
| `command_not_found` | (実行時) 機器定義の command が未登録 | True |
| `invalid_since_timestamp` | `get_experiment_timeline(since=...)` の値が ISO8601 でない | True (sub_class) |
| `invalid_until_timestamp` | `get_experiment_timeline(until=...)` の値が ISO8601 でない | True (sub_class) |
| `unknown_unit` | `ExperimentPlan.unit` が `experiment_units` に未登録 (v0.8.3) | True |
| `unit_role_missing` | `$role` が unit / explicit bindings のどちらにも無い (v0.8.3) | True |
| `template_override_invalid` | `start_experiment_job_from_template` の override に許可外キー (v0.8.3) | True (sub_class) |

> Note (v0.8.2.1): `invalid_since_timestamp` / `invalid_until_timestamp` は
> `error_class="validation"` の `details.sub_class` として返される。
> v1.0 で `error_class` Literal に昇格するか、`sub_class` のまま固定するかは
> v0.9.x で検討する。

### 2. execution (実機実行時のエラー)

| error_class | 発生 | recoverable |
|------------|------|-------------|
| `timeout` | VISA timeout / wait_for_* timeout / job_timeout | True (再試行可) |
| `protocol` | VISA プロトコルエラー | True |
| `hardware` | 機器側エラー (詳細不明) | True |
| `verify_mismatch` | verify read-back で値不一致 | True (機器状態調査要) |
| `blocked` | resource が他 Job により busy | True (待機) |
| `cancelled` | cancel_job により停止 | True |
| `interrupted` | サーバ再起動で中断 | True |
| `resume_not_allowed` | `resume_job` の前提条件を満たさない (completed/running/safe_shutdown_failed/dsl_version 非互換 等) (v0.9.0) | False |
| `invalid_export_path` | `export_experiment_results` の output_path が範囲外 / 既存 / 不正 (v0.9.1) | True |
| `export_failed` | export 出力中に I/O 失敗 (v0.9.1) | False |
| `unsupported_export_format` | csv / jsonl 以外の format 指定 (v0.9.1.1: 独立 error_class へ昇格) | True |
| `lock_conflict` | **v1.0 で deprecated** — `error_class=blocked` + `details.reason="lock_conflict"` に統一。`lock_conflict` を独立 error_class として返す経路は無し (audit log の内部 marker としてのみ残る)。 | True |
| `lock_stale` | **v1.0 で deprecated** — `error_class=blocked` + `details.reason="lock_stale"` に統一。 | True |
| `audit_query_failed` | query_audit 内部 error (v0.9.3、通常は `internal` を使用) | False |
| `WaitConditionTimeout` | wait_for_condition timeout | True |
| `WaitStableTimeout` | wait_for_stable timeout | True |
| `PollingErrorExceeded` | polling の連続エラーが max_consecutive_errors 超過 | True |
| `TimezoneRequired` | wait_until で naive timestamp | True |
| `InvalidTimestamp` | wait_until.timestamp parse 失敗 | True |
| `InstrumentNotFound` | polling 対象 instrument が未識別 | True |
| `AsyncStepRequiresJob` | execute_recipe で polling/barrier 検出 | True (start_recipe_job へ) |

### 3. group / map (Group / Map Job 特有)

| error_class | 発生 | recoverable |
|------------|------|-------------|
| `partial_failure` | 一部 target 失敗 (Job 全体は完了) | True |
| `target_failed` | target_runs の status=failed | True |
| `barrier_timeout` | BarrierCoordinator が timeout | True |
| `policy_stop` | failure_policy で stop_on_first_error 等 | True |
| `PollingErrorExceeded` | (Group target 内) polling 連続失敗 | True |

### 4. persistence (永続化エラー)

| error_class | 発生 | recoverable |
|------------|------|-------------|
| `persistence_warning` | critical event の DB 書き込み失敗 | True (実行は継続) |
| `persistence_error` | (将来) 致命的 DB 障害 | False |

### 5. system (内部エラー)

| error_class | 発生 | recoverable |
|------------|------|-------------|
| `internal` | 予期しない例外 | False (要バグ報告) |
| `not_found` | job_id / template / resource が見つからない | True |
| `validation` | 一般的な引数検証エラー | True |
| `configuration_error` | (将来) _system.yaml 不整合 | True |

## recoverable の意味

- `True`: AI エージェントが追加情報取得 / retry / 設定修正で次のアクションを取れる
- `False`: 人間の判断が必要 (override / バグ調査 / 機器調査)

## v1.0 で凍結対象

- 上記すべての `error_class` 名 (大文字小文字含む)
- `recoverable` の判定基準

v1.0 以降は **新規 error_class 追加は OK、既存 error_class の意味変更・rename は NG**。

## Extension 系 error_class (CLI 専用、v1.2–v1.4 で追加)

extension manifest / install / overlay registry / integrity 系で発生する
error_class。**MCP tool ではなく CLI JSON 出力上の error** であり、v1.0
で凍結した MCP error taxonomy とは別グループとして扱う。AI エージェント
には伝わらず、人間 / CI / 自動化スクリプトが読む。

### Manifest validation (v1.2 / v1.2.1)

| error_class | 意味 |
|-------------|------|
| `extension_path_outside_pack` | contents.* path が pack 外参照 (絶対 / `..` traversal) |

### Install (v1.3 / v1.3.1)

| error_class | 意味 |
|-------------|------|
| `extension_duplicate_install` | 同 extension_id が既に install 済み (--force で上書き) |
| `extension_validation_failed` | install 前 validation 失敗 |
| `extension_source_inside_extensions_dir` | install 元 path が `extensions_dir` 配下 |

### Overlay registry (v1.3 / v1.3.1)

| error_class | 意味 |
|-------------|------|
| `overlay_registry_duplicate_id` | builtin / extension 間で id 衝突 |
| `registry_entry_path_outside_pack` | overlay 構築時、entry の path が pack 外 |
| `registry_entry_missing_id` | overlay entry の id が空 |
| `registry_entry_missing_path` | overlay entry の path が空 |

### Integrity (v1.4)

| error_class | integrity 値 |
|-------------|---------------|
| `extension_install_path_missing` | `missing_file` |
| `extension_install_meta_missing` | `invalid` |
| `extension_manifest_missing` | `invalid` |
| `extension_checksum_mismatch` | `modified` |
| `extension_checksum_unreadable` | `modified` |
| `extension_file_missing` | `missing_file` |

### Zip install (v1.6 / v1.6.1)

| error_class | 意味 |
|-------------|------|
| `extension_install_zip_invalid` | zip path が file でない |
| `extension_install_zip_verify_failed` | verify-package 段階で error |
| `extension_install_zip_unsafe` | zip slip 二重 check で reject |
| `extension_install_zip_no_manifest` | zip 内 extension.yaml 不在 |
| `extension_install_zip_no_root_manifest` | zip **root** に extension.yaml なし (v1.6.1) |
| `extension_install_zip_too_many_files` | file 数上限 5000 超 (v1.6.1) |
| `extension_install_zip_too_large` | uncompressed total > 200MB (v1.6.1) |

### Strict validation (v1.4 / v1.4.1)

`validate extension --strict` および `extension check --strict` 専用。
通常 validation の warning を error に格上げする (registry 掲載 / CI /
release 前検査向け)。`strict_` prefix で識別できる。

| error_class | 通常時 |
|-------------|--------|
| `strict_empty_contents` | warning |
| `strict_registry_entries_format` | warning |
| `strict_support_level_draft` | warning |
| `strict_verified_requires_evidence` | (なし) |
| `strict_registry_entry_missing_id` | warning |
| `strict_registry_entry_missing_path` | warning |
| `strict_registry_entry_missing_vendor` | warning |
| `strict_registry_entry_missing_model` | warning |
| `strict_registry_entry_missing_category` | warning |
| `strict_registry_entry_missing_support_level` | warning |
| `strict_registry_entry_invalid_support_level` | warning |
| `strict_registry_entry_path_outside_pack` | (なし) |
| `strict_registry_entry_support_level_mismatch` | warning |
| `strict_extension_extra_file` | warning |
| `strict_missing_catalog_summary` | warning (v1.6) |
| `strict_missing_catalog_license` | warning (v1.6) |

### 関連 warning_class

| warning_class | 意味 |
|---------------|------|
| `extension_extra_file` | metadata 外の file が install path に存在 |
| `extension_missing_manifest` | overlay 構築時、installed pack に extension.yaml 無し |
| `registry_entry_missing_vendor` / `_model` / `_category` / `_support_level` | overlay entry の補足項目欠落 |
| `empty_contents` | manifest の contents.* が全て空 |
| `registry_entries_format` | registry_entries YAML が `instruments` キーを持たない |

## 関連

- `docs/compatibility.md`: v1.0 互換保証対象一覧
- `docs/extension_integrity.md`: integrity 検査と strict mode
- `docs/extension_install.md`: install フロー
- `docs/extension_registry_overlay.md`: overlay registry
- `src/visa_mcp/response_envelope.py`: `make_error` 構造
- `src/visa_mcp/observation.py`: `_is_recoverable` 判定ロジック
