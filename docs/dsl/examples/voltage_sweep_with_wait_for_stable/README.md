# voltage_sweep_with_wait_for_stable

電圧を sweep しながら、各点で温度計が安定してから DMM で測定する。

## Intended tool sequence
1. `validate_experiment_plan(plan)`
2. `dry_run_plan(plan)` で polling_safe warning の有無を確認
3. `start_experiment_job(plan)`

## Required bindings
- `psu`: 電源
- `dmm`: 電圧計
- `temp`: 温度計

## Expected warnings
- `temp001.measure_temperature` が `polling_safe: True` を持たない場合、
  `polling_safe_false` warning が出る (実行は可能)

## Expected behaviour
- sweep が 3 値展開
- 各値で wait_for_stable が温度安定 (window 30s 内 max-min <= 0.2℃) まで polling
- `timeout_s=300` を超えると step failed
