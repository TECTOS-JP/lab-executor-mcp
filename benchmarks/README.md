# visa-mcp Benchmark suite (v0.9.0, experimental)

AIエージェントの実験遂行能力を、実機 (および本物の LLM) なしに再現可能に
評価するためのベンチマーク基盤。

## ディレクトリ構成

```
benchmarks/
├── README.md
├── tasks/                 # benchmark task 定義 (YAML)
│   ├── task_001_basic_validate_dry_run.yaml
│   ├── task_002_unit_based_voltage_sweep.yaml
│   ├── task_003_template_override_run.yaml
│   ├── task_004_verify_mismatch.yaml
│   └── task_005_partial_failure_group.yaml
└── fixtures/
    ├── system_config_basic.yaml
    ├── system_config_partial_failure.yaml
    └── instruments/
        ├── mock_psu.yaml
        ├── mock_dmm.yaml
        └── mock_temp.yaml
```

## 3 層 benchmark

1. **Layer 1 (validate)** ── `validate_experiment_plan` 相当
2. **Layer 2 (dry_run)** ── `dry_run_plan` 相当
3. **Layer 3 (execute)** ── `start_experiment_job` (mock 機器で実行)

各 task の `layer` フィールドで対象 layer を指定。

## 実行方法 (Python から)

```python
import asyncio
from pathlib import Path
from visa_mcp.testing.benchmark_runner import run_task_file

result = asyncio.run(run_task_file(
    task_path="benchmarks/tasks/task_001_basic_validate_dry_run.yaml",
    benchmarks_root="benchmarks",
    db_dir="benchmarks/.tmp/db",
    artifacts_dir="benchmarks/.tmp/artifacts",
))
print(result.status, result.scores)
```

## v0.9.0 のスコープ外

- 本物の LLM 呼び出し評価 (v1.0)
- self-repair loop (v0.9.1)
- benchmark を MCP tool 化する (v1.0 で必要に応じて)
- LLM ベンチ CI (v1.0)

## fixture 形式

`fixtures/instruments/*.yaml` は通常の機器定義 YAML と同じ schema。
file stem (例: `mock_psu`) を `_system.yaml.instruments.<alias>` の `alias`
と一致させると benchmark runner が自動 bind する。
