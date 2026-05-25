# 測定結果 export API (v0.9.1, experimental)

実験 Job の測定結果を **少量確認用 JSON** と **解析用ファイル出力** の 2 ツール
構成で外部に渡す。MCP 応答に大量データを混ぜないため。

## ツール一覧と使い分け

| ツール | 用途 | 返却サイズ |
|--------|------|-----------|
| `get_job_summary` | 実験全体の **要約** (key_results / failures / verify_summary) | 小 |
| `get_job_result` | Job の **最終 result raw** (生 dict) | 中 |
| `get_experiment_results` | **解析用の行形式** データ (限定列で正規化) | 中 (limit 上限 10000) |
| `get_monitor_data` | **monitor 時系列** の詳細 | 大 |
| `export_experiment_results` | **CSV / JSONL ファイル**へ書き出し | 大 |

LLM が「次の判断」に使うときは `get_experiment_results` (limit=100〜)、
pandas / Excel で解析するときは `export_experiment_results`、monitor 時系列
は `get_monitor_data` 推奨。

## `get_experiment_results(job_id, limit=1000, offset=0, include_monitor_data=False)`

### 抽出元

| ソース | 取得する内容 |
|--------|------------|
| `job_steps.result_json` | step 完了時の result (`response_parsed` dict → 各 key を 1 行へ展開、その他は `command` を measurement として 1 行) |
| `job_steps.error_json` | step 失敗時の error (strict mode で verify 失敗時など) |
| `target_runs.result_json` | target ごとの最終 result の scalar 値 (`step_results` / `success` / `summary` は除外) |
| `monitor_data` | `include_monitor_data=true` のときのみ追記 (monitor_id == job_id 慣習、後述) |

### 標準 columns (v1.0 凍結候補)

```text
timestamp
target_id
instrument
measurement
value
unit
step_index
step_path
```

### pagination

`limit` の上限は **10000** にクランプ (`get_monitor_data` と同じ)。
`pagination.has_more=true` のときは offset + limit で続きを取得。

### `monitor_data` 取り扱い (v0.9.1 慣習、v0.9.2+ で再検討予定)

MVP 実装では `monitor_id == job_id` を前提に `count_monitor_data(job_id)` /
`list_monitor_data(job_id)` を呼ぶ。Job が複数 monitor を持つ場合や monitor
の自由命名対応は v1.0 までに以下を検討:

```text
include_monitor_data: false | true | "linked_only" | "all_for_job"
monitor_ids: 明示指定可能
```

## `export_experiment_results(job_id, format, ...)`

### 引数

```text
format: "csv" | "jsonl"
include_monitor_data: bool (default False)
output_path: str (default ~/.visa-mcp/exports/<job_id>_results.<format>)
overwrite: bool (default False)
```

### 出力先制約 (security)

- 既定 export dir: `~/.visa-mcp/exports/`
- `output_path` は **既定 dir 配下のみ許可**。絶対パス / `..` traversal は
  `error_class=invalid_export_path` で拒否
- 既存ファイルは `overwrite=False` (既定) で拒否 (デフォルトパスでも同じ)
- 上書き許可するには `overwrite=True` を明示

### 返却

```json
{
  "data": {
    "job_id": "<job>",
    "format": "csv",
    "path": "~/.visa-mcp/exports/<job>_results.csv",
    "rows": 1200,
    "size_bytes": 56789,
    "sha256": "...",
    "include_monitor_data": false,
    "columns": ["timestamp", "target_id", ...]
  }
}
```

`sha256` は v1.0 `export_experiment_bundle` の整合性確認に使用予定。

## error_class 一覧 (v0.9.1.1)

| error_class | 意味 | recoverable |
|-------------|------|-------------|
| `not_found` | job_id が存在しない | False |
| `invalid_export_path` | output_path が default dir 外 / `..` / 既存 (overwrite=False) | True |
| `export_failed` | 書き込み I/O 失敗 | False |
| `unsupported_export_format` | csv / jsonl 以外指定 | True |

v0.9.1 時点では `unsupported_export_format` を `validation` の `sub_class`
として返していたが、**v0.9.1.1 で独立 error_class に統一** (AI エージェントの
修正可能性を高めるため)。

### `invalid_export_path` の recommended_next_actions (v0.9.1.1)

既存ファイル拒否時は以下を返す:

```json
{
  "error_class": "invalid_export_path",
  "message": "...",
  "recommended_next_actions": [
    {"action": "set_overwrite_true",
     "reason": "前回の export を意図的に上書きする"},
    {"action": "choose_different_output_path",
     "reason": "別 path を指定して前回の export を保持する"}
  ]
}
```

## CLI 利用例 (Python)

```python
import asyncio
from visa_mcp.tools.export import _extract_result_rows  # internal helper

# 解析: pandas で読む
import pandas as pd
df = pd.read_csv("~/.visa-mcp/exports/<job_id>_results.csv")
```
