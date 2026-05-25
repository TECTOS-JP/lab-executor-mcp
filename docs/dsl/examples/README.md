# Experiment DSL Examples (v0.8.1)

このディレクトリは AI エージェントが Experiment DSL を学習・参照するための
典型 plan 集です。各 example は以下の構成を持ちます。

```
<example_name>/
  plan.json              # ExperimentPlan の JSON 表現
  expected_dry_run.json  # validate_and_compile の期待結果サマリ
  README.md              # 人間/エージェント向け説明
```

## 共通の使用フロー

各 example は以下のツール列で使う想定:

1. `validate_experiment_plan(plan)` で構文・resource・safety・verify を検証
2. `dry_run_plan(plan)` で送信予定 SCPI と verify 予定を確認 (実機ノータッチ)
3. 問題なければ `start_experiment_job(plan, owner)` で Job 実行
4. `get_job_status(job_id)` で進捗、`get_job_result(job_id)` で完了結果取得

## examples 一覧

| name | 目的 | 含む step type |
|------|------|---------------|
| `basic_voltage_set_and_measure` | 単純な set → query の基本パターン | command / query |
| `voltage_sweep_with_wait` | 電圧 sweep + 各点で固定 wait | sweep / command / query / wait |
| `voltage_sweep_with_wait_for_stable` | 電圧 sweep + 安定待ち | sweep / wait_for_stable / query |
| `partial_failure_group_measurement` | 複数機器 parallel measurement (一部失敗を許容) | parallel / query |
| `safe_shutdown_explicit_targets` | 明示的に targets を指定する安全停止 | command / safe_shutdown |

## 必要な bindings / instrument 定義

各 example は `plan.bindings` で `$psu` / `$dmm` / `$temp` 等を実 alias に
マッピングする。利用前に `_system.yaml` で対応 alias を定義してください。

例:

```yaml
# instruments/_system.yaml
instruments:
  psu001:
    resource: "GPIB0::6::INSTR"
    bus: "GPIB0"
  dmm001:
    resource: "GPIB0::5::INSTR"
    bus: "GPIB0"
```
