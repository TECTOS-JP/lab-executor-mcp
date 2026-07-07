# 実験資産 (experiment asset) の使い方 (v0.2 / v2.26.0)

実験資産は、完了 Job を **独立可用性レベル L0〜L5** で格付けした 1 つの zip
アーカイブ。MaiML (JIS K 0200:2024) の独立可用性概念に基づく。仕様の正本は
[`experiment_asset_schema_v0.md`](experiment_asset_schema_v0.md)。

> ⚠ 実験資産は **CLI のみ** (`lab-executor asset ...`)。MCP ツール面 (50 個) は
> 不変。市場流通は外部行為であり、AI エージェント向けツール化は将来判断。

## 共有ポリシー (2026-07-07 所有者決定)

1. **ライセンス**: 既定値は `UNLICENSED` のまま (暗黙のライセンス付与をしない)。
   **外部に共有する資産にだけ** `--license` で明示的に付与する。最初に共有する
   資産は **CC-BY-4.0** を用いる。
2. **L3 上限ルール**: **外部共有は L3 (再解析可能) まで**。L4/L5 資産
   (capability 宣言・再実行可能パッケージ) は自分・チーム内に留める。
   受け手側の AI 安全基盤 (実行前の危険性レビュー・実行範囲制御) が整うまで、
   再実行可能物の外部流通は行わない。
   将来の `asset publish` (P3.0) は、この L 上限チェックを共有ゲートとして
   実装すること。

## 1. export — 資産を生成する

```text
lab-executor asset export \
    --job <job_id> \
    --db <state.sqlite> \
    --instruments-dir <装置定義 YAML のディレクトリ> \
    [--out PATH] [--title STR] [--license SPDX-ID] \
    [--analysis PATH] [--declare-level N] [--git-commit HASH] \
    [--dry-run-now] [--meta META.yaml] [--json]
```

- `--out` を省略すると `~/.visa-mcp/exports/<asset_id>.asset.zip` に出力。
- `--analysis` に渡した手順書 (README / スクリプト) が `analysis/README.md` として
  同梱される (L3 の要件)。
- `--instruments-dir` から、Job の recipe を持つ装置定義 YAML を探して同梱する。
  特定できない場合はエラーにならず、instrument 無しで続行する (宣言可能レベルが
  下がるだけ)。
- `--dry-run-now` (v0.2) — export 時に同梱レシピをコンパイル検証し、結果を
  `asset.yaml` の `dry_run` に記録する (L5 の要件)。詳細は下記「L5 に上げる」。
- `--meta META.yaml` (v0.2) — `conditions` / `hazards` / `expected_results` /
  `sample` を 1 ファイルで一括指定する (下記「meta ファイル」)。

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

### meta ファイル — L2/L5 メタデータを CLI から書く (v0.2)

`--meta META.yaml` は、トップレベルに `conditions` / `hazards` /
`expected_results` / `sample` の任意サブセットを持つ YAML ファイル。

```yaml
conditions:
  calibration: "2026-06 校正証明書 #1234"
  environment: "23±1°C, 45%RH"
hazards:
  none_declared: true
expected_results:
  - {command: measure_voltage, value_min: -0.1, value_max: 0.1}
sample:
  uuid: null
  metadata: {description: "無負荷 (出力OFF基線)"}
```

- 上の 4 つ以外のトップレベルキーがあると **exit 1** でエラーになる
  (誤記で L5 を逃さないよう、黙って無視しない)。
- meta の指定は `build_asset` の個別引数 (`conditions=` 等) より優先される。

### L5 に上げる — hazards / expected_results / dry_run

L5 は次の 4 つをすべて満たす:

1. `hazards` を明示 (`none_declared: true` か、電圧 / 温度 / 化学物質の上限)。
2. `expected_results` が 1 件以上。
3. `dry_run.ok=true`。
4. 同梱装置定義が `validate_instrument_file(strict=True)` の errors 0。

v0.2 では **`--dry-run-now` で 3 を CLI から満たせる**。builder が export 時に
同梱レシピを `recipe_to_plan` でコンパイルし、同梱装置定義を検証して、成功なら
`dry_run.ok=true` (+ `step_count` / `performed_at` / `method`) を書き込む。
コンパイル例外・検証 errors・定義/レシピ不在の場合は `ok=false` + `error` を
記録するが **export 自体は成功する** (L5 に届かないだけ)。UI で過去に dry-run した
内容ではなく「梱包物そのもの」を検証するため、資産内容と検証内容が乖離しない。

`hazards` / `expected_results` は `--meta` (上記) で渡す。

#### CLI だけで L5 資産を作る (end-to-end)

```text
lab-executor asset export \
    --job <job_id> --db <state.sqlite> \
    --instruments-dir <dir> --analysis procedure.md \
    --meta l5_meta.yaml --dry-run-now --out my.asset.zip

lab-executor asset check my.asset.zip
#   => verified level: L5
```

`l5_meta.yaml` は上の meta ファイル例のように `hazards` (`none_declared` か上限) と
`expected_results` を含めること。`conditions.calibration/environment` (L2) も
併せて書けば L2〜L5 を CLI 単独で満たせる。

## 関連 docs

- [`experiment_asset_schema_v0.md`](experiment_asset_schema_v0.md) — レベルの意味論と根拠
- [`bundle_export.md`](bundle_export.md) — 内包する export bundle (bundle_version=1.0)
