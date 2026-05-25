# basic_voltage_set_and_measure

電源を 5V に設定し、その後測定値を取得する最小例。

## Intended tool sequence
1. `validate_experiment_plan(plan)`
2. `dry_run_plan(plan)`
3. `start_experiment_job(plan, owner="agent")`
4. `get_job_result(job_id)`

## Required bindings
- `psu`: 電源の alias (例: psu001)

## Expected behaviour
- `VOLT 5.0` → `MEAS:VOLT?` の 2 行が rendered_scpi に並ぶ
- `step_count_dsl = 2`、`step_count_expanded = 2`
