# template_override (v0.8.3, experimental)

`save_experiment_template` で保存した template を
`start_experiment_job_from_template(name, override=...)` で部分上書き
実行する例。LLM が template + 差分だけで Plan を再利用できる。

## 流れ

1. `save_experiment_template(name="voltage_sweep_basic", plan=<template.json>)`
2. `start_experiment_job_from_template(name="voltage_sweep_basic",
   override=<override.json>, dry_run=true, include_expanded_plan=true)` で
   override 適用後の Plan を確認
3. dry_run=false で実行

## 許可される override キー

- `name`        新しい Job 名
- `unit`        実験対象 unit を差し替え (役割は同じ前提)
- `bindings`    role → alias の dict (key 単位の override)
- `parameters`  Plan.variables への shallow merge ({voltage} など)
- `owner`       Job owner (Plan 本体には影響しない)

## 拒否される (`error_class=validation`,
`sub_class=template_override_invalid`)

- `steps` 直接上書き
- `dsl_version` 変更
- `variables` 全体置換 (`parameters` 経由のみ可)
- `description` 直接上書き

steps を変更したいなら、`get_experiment_template` で template を取り出し、
編集してから `start_experiment_job(plan=...)` を使うこと。

## expanded_plan 例 (`include_expanded_plan=true`)

```json
{
  "dsl_version": "0.8",
  "name": "run_from_template_001",
  "unit": "unit002",
  "bindings": { "dmm": "dmm_backup_001" },
  "variables": { "voltage": 3.5, "wait_s": 1.0 },
  "steps": [ ... template と同じ ... ]
}
```

## Job metadata に残る `template_source`

```json
{
  "template_name": "voltage_sweep_basic",
  "template_version": "0.8",
  "override_json": { ... },
  "override_keys": ["name", "unit", "bindings.dmm",
                    "parameters.voltage", "parameters.wait_s"]
}
```

これは `experiment_plans.compiled_summary.template_source` と
`jobs.parameters_json.template_source` の両方に保存される。

## 名称対応 (v0.8.3.1 明記)

| Override 側 | Plan 内 | 説明 |
|------------|---------|------|
| `override.parameters` | `expanded_plan.variables` | sweep 変数 / `{voltage}` 等の置換用変数。`parameters` は外部入力名 (template の引数感)、内部では `variables` に shallow merge される。 |
| `override.owner` | (Plan には入らず) `jobs.owner` | Plan 本体には埋め込まれない。Job metadata 側にのみ反映。レスポンス `data.owner` で確認可能。 |
| `override.unit` | `expanded_plan.unit` | そのまま反映。 |
| `override.bindings` | `expanded_plan.bindings` | key 単位で template の bindings を上書き。 |
| `override.name` | `expanded_plan.name` | Job 名 (template 名ではない)。 |

## `template_version` について (v0.8.3.1 注記)

`template_source.template_version` は現状 template の `dsl_version` を流用している。
v0.9.x で template 自体の改訂番号 (`template_revision: 3` 等) を導入する場合、
別概念として分離する余地があるため、外部参照する際は v0.8.x の間は
"DSL schema バージョン" と読み替えるのが安全。

## `$role` 推奨ルール (v0.8.3.1 明記)

Plan 内の `instrument` フィールドは以下のように解釈される:

- `"$psu"` → `effective_bindings["psu"]` (**unit / explicit binding を使う場合は推奨**)
- `"psu"` → `_system.yaml.instruments` の alias または resource 名 (fallback)
- `"USB0::..."` → raw VISA resource (unit 指定時は warning)

LLM 向けに DSL を書くときは、**unit / bindings を使う場合は `$role` prefix を必ず付ける**
ことを推奨。`$` なしだと alias / resource fallback パスに乗るため、unit role 解決が
スキップされる可能性がある。

