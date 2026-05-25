# safe_shutdown_explicit_targets

複数機器を操作した後、特定の機器だけを safe_shutdown する例。

## Intended tool sequence
1. `validate_experiment_plan(plan)`
2. `dry_run_plan(plan)` で `safe_shutdown_targets` に psu001 だけが含まれることを確認
3. `start_experiment_job(plan)`

## Required bindings
- `psu_a` / `psu_b`: 2 台の電源

## Expected behaviour
- compile 時に `safe_shutdown.targets = ["$psu_a"]` が解決され、
  `CompiledPlan.safe_shutdown_targets = ["psu001"]` になる
- 実行時 (v0.8.0.1+) は psu001 にのみ safe_shutdown が走り、psu002 はそのまま

## Common errors
- `safe_shutdown.targets=[]` (空配列) は validation error (`safe_shutdown_targets_empty`)
- `targets` に未知の binding (例: `$unknown`) を指定すると `unknown_binding` error
