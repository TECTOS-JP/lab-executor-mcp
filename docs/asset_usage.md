# 実験資産 (experiment asset) の使い方 (v0.1 / v2.25.0)

実験資産は、完了 Job を **独立可用性レベル L0〜L5** で格付けした 1 つの zip
アーカイブ。MaiML (JIS K 0200:2024) の独立可用性概念に基づく。仕様の正本は
[`experiment_asset_schema_v0.md`](experiment_asset_schema_v0.md)。

> ⚠ 実験資産は **CLI のみ** (`lab-executor asset ...`)。MCP ツール面 (50 個) は
> 不変。市場流通は外部行為であり、AI エージェント向けツール化は将来判断。

## 1. export — 資産を生成する

```text
lab-executor asset export \
    --job <job_id> \
    --db <state.sqlite> \
    --instruments-dir <装置定義 YAML のディレクトリ> \
    [--out PATH] [--title STR] [--license SPDX-ID] \
    [--analysis PATH] [--declare-level N] [--git-commit HASH] [--json]
```

- `--out` を省略すると `~/.visa-mcp/exports/<asset_id>.asset.zip` に出力。
- `--analysis` に渡した手順書 (README / スクリプト) が `analysis/README.md` として
  同梱される (L3 の要件)。
- `--instruments-dir` から、Job の recipe を持つ装置定義 YAML を探して同梱する。
  特定できない場合はエラーにならず、instrument 無しで続行する (宣言可能レベルが
  下がるだけ)。

### 資産 zip レイアウト

```text
<asset_id>.asset.zip
├── asset.yaml                 # マニフェスト (asset_version=0.1)
├── bundle/                    # export_experiment_bundle の全内容 (bundle_version=1.0)
│   ├── manifest.json / job_record.json / job_summary.json
│   ├── timeline.jsonl / results.jsonl / results.csv
├── recipe/recipe.yaml         # recipe 名 + 解決済みパラメータ + 定義断片 (requires 込み)
├── instrument/<slug>.yaml     # 実行に使った装置定義
└── analysis/README.md         # 解析手順 (--analysis で指定)
```

同梱ファイルはすべて `asset.yaml` の `contents` に sha256 付きで列挙される。

## 2. check — レベルを機械判定する

```text
lab-executor asset check <asset.zip> [--json]
```

出力はレベル表 (L0〜L5 の ok/ng と不足理由) と最終判定 1 行。

- **終了コード**: スキーマ検証失敗 or checksum 破損で `1`、それ以外は `0`。
  レベルが低いこと自体はエラーではない (`0`)。

## 3. レベルの読み方と上げ方

| レベル | 意味 | 主な追加要件 |
|---|---|---|
| **L0** | 生データのみ | results が 1 行以上 |
| **L1** | 測定条件付属 | recipe / parameters / created_at / resource_name |
| **L2** | 装置・校正・環境 | instrument 定義 + timeline + `conditions.calibration/environment` キー (値が `not_recorded` でも可、**キー欠落は不可**) |
| **L3** | 第三者が再解析可能 | checksum 全一致 + raw↔数値の対応 + `analysis/` + スキーマ pass |
| **L4** | 代替装置で追試可能 | recipe に `requires:` + 同梱装置が capability を満たす |
| **L5** | AI が安全確認後に再実行可能 | hazards 明示 + expected_results + `dry_run.ok=true` + 装置定義の strict 検証 pass |

各レベルは **下位を全て満たさないと上位に上がれない** (累積的)。

### L2 に上げる — conditions を明示する

`--` オプションはまだ CLI に無いが、`build_asset(conditions={...})` で
`calibration` / `environment` を渡す。記録が無い場合も `"not_recorded"` を
明示すること (暗黙の欠落は L2 要件を満たさない)。

### L4 に上げる — recipe に requires を書く

装置定義 YAML の該当レシピに capability 要件を宣言する:

```yaml
recipes:
  ramp_voltage:
    requires:
      commands: [set_voltage, query_voltage]
      ranges:
        "set_voltage.voltage": { min: 0, max: 10 }
    steps: [...]
```

`requires` は optional。無い既存レシピの検証結果は一切変わらない。

### L5 に上げる — hazards / expected_results / dry_run

`build_asset(hazards={...}, expected_results=[...])` で危険性宣言と期待結果を
書き込む。`hazards` は `none_declared: true` か、電圧 / 温度 / 化学物質の上限を記入。
`dry_run.ok=true` は M3 の dry-run 実行記録の接続で満たす (v0.1 では builder は
自動記入しない)。加えて同梱装置定義が
`validate_instrument_file(strict=True)` の errors 0 を満たすこと。

## 関連 docs

- [`experiment_asset_schema_v0.md`](experiment_asset_schema_v0.md) — レベルの意味論と根拠
- [`bundle_export.md`](bundle_export.md) — 内包する export bundle (bundle_version=1.0)
