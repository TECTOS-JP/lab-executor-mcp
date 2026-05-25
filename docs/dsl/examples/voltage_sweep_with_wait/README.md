# voltage_sweep_with_wait

電圧を `[1.0, 2.0, 3.0]` V で sweep し、各点で 1 秒待ってから DMM で測定。

## Intended tool sequence
1. `validate_experiment_plan(plan)`
2. `dry_run_plan(plan)` で 3 × 3 = 9 個の rendered_steps を確認
3. `start_experiment_job(plan)`

## Required bindings
- `psu`: 電源
- `dmm`: 電圧計

## Expected behaviour
- sweep が compile 時に展開され、IR Plan は 9 step (3 値 × 3 body)
- `summary.step_count_expanded = 9`

## Failure handling
- `set_voltage` が range 違反なら `parameter_invalid`
- `dmm` 未識別なら `not_identified`
