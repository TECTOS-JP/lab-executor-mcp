# Reproducibility bundle export (v1.0, experimental)

`export_experiment_bundle` は完了 Job の実験記録を **1 つの zip** にまとめる
ためのツール。AI エージェント・人間どちらにとっても、後から
**再検証 / 共有 / 監査 / 記事化** がしやすくなる。

> ⚠ **v1.x では「別環境での完全再現実行」をサポートしない**。
> `import_experiment_bundle` / replay は v1.1+ 候補。bundle は analysis /
> sharing パッケージとして位置づけてください。

## 使い方

```text
export_experiment_bundle(
  job_id: str,
  output_path: str = "",
  include_monitor_data: bool = False,
  include_audit: bool = False,
  overwrite: bool = False,
)
```

返り値 (envelope.data):

```json
{
  "job_id": "...",
  "bundle_version": "1.0",
  "path": "~/.visa-mcp/exports/<job_id>_bundle.zip",
  "size_bytes": 12345,
  "sha256": "...",
  "contents": ["audit.jsonl", "compiled_summary.json", ...],
  "include_monitor_data": false,
  "include_audit": false
}
```

## Bundle layout

```text
<job_id>_bundle.zip
├── manifest.json          (bundle_version=1.0, visa_mcp_version, contents, checksums)
├── plan.json              (DSL plan original)
├── compiled_summary.json  (CompiledPlan.summary)
├── job_record.json        (jobs テーブル 1 行 dict)
├── job_summary.json       (build_run_summary 結果)
├── timeline.jsonl         (job_events 全件 / 1 行 1 イベント)
├── results.jsonl          (測定結果行 / 標準 columns)
├── results.csv            (同上 CSV 版)
├── monitor_data.jsonl     (include_monitor_data=True のみ)
└── audit.jsonl            (include_audit=True のみ)
```

各 file の **SHA-256** が `manifest.json` の `checksums` に記録される。
外側 zip 全体の SHA-256 は MCP response の `data.sha256` に返る。

### `manifest.json` の例

```json
{
  "bundle_version": "1.0",
  "visa_mcp_version": "1.0.1",
  "job_id": "job_abc123",
  "created_at": "2026-05-23T10:00:00+00:00",
  "include_monitor_data": false,
  "include_audit": false,
  "contents": ["compiled_summary.json", "job_record.json",
               "job_summary.json", "manifest.json", "plan.json",
               "results.csv", "results.jsonl", "timeline.jsonl"],
  "checksums": {
    "plan.json": "9f86d081884c7d659...",
    "job_record.json": "..."
  },
  "note": "再検証 / 共有 / 監査 / 記事化用パッケージ。v1.x では別環境での完全再現実行はサポートしない"
}
```

### `results.jsonl` / `results.csv` の列

`get_experiment_results` と同じ標準 columns:

```text
timestamp / target_id / instrument / measurement / value / unit /
step_index / step_path
```

`monitor_data` を含む場合 (`include_monitor_data=True`) は `monitor_data.jsonl`
を別ファイルで提供 (results との混在は避ける)。

## 保存先と path 安全策

`export_experiment_results` と同等:

| 項目 | 仕様 |
|------|------|
| 既定 export dir | `~/.visa-mcp/exports/` |
| `output_path` の許可範囲 | **既定 dir 配下のみ** |
| `..` traversal / 外部絶対パス | `error_class=invalid_export_path` で拒否 |
| 既存ファイル | `overwrite=False` 既定で拒否 (`invalid_export_path`) |
| 既定ファイル名 | `<job_id>_bundle.zip` |

### path traversal 拒否の挙動例

```json
{
  "status": "error",
  "errors": [
    {
      "error_class": "invalid_export_path",
      "message": "output_path は ~/.visa-mcp/exports/ 配下である必要があります",
      "recoverable": true,
      "details": {
        "base_dir": "~/.visa-mcp/exports",
        "rejected_path": "/etc/passwd"
      }
    }
  ]
}
```

## SHA-256 の使い方

- **manifest.checksums[name]**: bundle 内 1 ファイルの sha256
- **response.data.sha256**: bundle zip 全体の sha256

bundle を共有する場合、受け取り側は:

1. zip 全体の sha256 を計算 → response の sha256 と一致確認
2. zip 中の `manifest.json` を読み、各 file を展開して sha256 を計算 →
   manifest と一致確認

これにより「途中で 1 行書き換わった」のような改ざんを検出できる。

## `plan.json` の optional 扱い (v1.1.1 明記)

`plan.json` は **DSL Job (`start_experiment_job` 由来) でのみ** bundle に
含まれる。Recipe job / Group / Map / wait job などの非 DSL Job では生成
されないため、**bundle validation の必須ファイルから除外** されている。

```text
Required (always present):
  manifest.json / job_record.json / timeline.jsonl /
  results.jsonl / results.csv

Optional (DSL job only):
  plan.json / compiled_summary.json / job_summary.json

Optional (include_* flag):
  monitor_data.jsonl (include_monitor_data=true)
  audit.jsonl        (include_audit=true)
```

`validate_experiment_bundle` は plan.json が無くても bundle_valid を判定
できるが、`inspect_experiment_bundle` の `plan` フィールドは plan.json
が無ければ `None`。

## bundle inspection の zip 安全性 (v1.1.1 明記)

`validate_experiment_bundle` / `inspect_experiment_bundle` は以下を保証:

- **ファイルシステムへの展開は行わない** (`zipfile.ZipFile.read()` で
  メモリ内のみ読み取り)
- zip slip 的なファイル展開なし (展開しないため)
- 不正な zip は `error_class=validation` + `details.sub_class=invalid_bundle_format` で拒否

⚠ **未対応 (v1.x 開発予定)**:

- zip bomb / 巨大ファイル読み取り上限 (v1.x 内で `max_file_size_mb` 設定を
  検討、v1.2+ 候補)
- 信頼できない bundle (例: 外部から受領した未署名 zip) を inspect する場合は、
  別途 sandbox / リソース制限環境での実行を推奨

## experimental スコープ (v1.x)

`export_experiment_bundle` は **experimental** ツール
([`docs/v1_stability_policy.md`](v1_stability_policy.md))。

v1.x 内で変更可能な項目:

- bundle layout 内の追加ファイル
- `manifest.json` の追加フィールド
- `bundle_version` のマイナー更新 (1.0 → 1.1 等、後方互換)

v1.x 内で変更しない (互換保証 informal):

- **`bundle_version: "1.0"` の存在保証**
- `manifest.json` / `plan.json` / `job_record.json` / `results.{jsonl,csv}` /
  `timeline.jsonl` の存在
- `manifest.checksums` の SHA-256 表現
- path 安全策 (default dir / traversal 拒否 / overwrite 既定)

## v1.1+ ロードマップ

| 機能 | 目的 |
|------|------|
| `validate_bundle(path)` | bundle 整合性 (checksums / manifest schema) チェック |
| `import_bundle_for_analysis(path)` | bundle を読み込んで `get_experiment_results` 相当の API を提供 (実機ノータッチ) |
| `replay_bundle_with_mock(path)` | mock instruments + benchmark runner で再実行 |

完全再現実行 (実機側で同じ DSL を再実行) は v2.x 候補。

## 関連 docs

- [`docs/result_export.md`](result_export.md) — `get_experiment_results` /
  `export_experiment_results`
- [`docs/v1_stability_policy.md`](v1_stability_policy.md) — bundle が
  experimental スコープにある理由
- [`docs/operational_integrity.md`](operational_integrity.md) —
  `include_audit=True` 時の audit redaction
