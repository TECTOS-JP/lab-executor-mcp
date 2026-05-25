# partial_failure_group_measurement

3 つの DMM を parallel で同時に測定する。一部が failed しても他は記録される。

## Intended tool sequence
1. `validate_experiment_plan(plan)`
2. `dry_run_plan(plan)` で 3 branch が共有 resource を持たないことを確認
3. `start_experiment_job(plan)`
4. `get_job_result(job_id)` で `result.results` に target_id ごとの結果を取得

## Required bindings
- `dmm_a` / `dmm_b` / `dmm_c`: 3 台の電圧計 (異なる resource)

## Expected behaviour
- 3 branch が GroupExecutor で並列実行 (concurrency=3)
- 各 branch が独立 resource を使うため真の並列
- 1 branch が失敗しても `failure_policy.mode=continue` (デフォルト) で他は継続
- 結果は `status=partial_failure` または `ok`、`summary.success` で成功数を確認

## Failure handling
- LLM は `results` を見て失敗 target_id だけ retry する判断ができる
