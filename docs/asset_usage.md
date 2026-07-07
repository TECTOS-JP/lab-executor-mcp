# 実験資産 (experiment asset) の使い方 (v0.2 + registry / v2.27.0)

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

## 4. registry — 資産を掲載・一覧する (P3.0 / v2.27.0)

資産レジストリは **ディレクトリ 1 つ + `INDEX.yaml` + `assets/<id>.asset.zip` 群**。
利用者が任意の場所に作る。既存の extension registry (`registry/INDEX.yaml`、
instruments 用) とは**別物**で、そちらには一切触れない。市場流通は外部行為であり、
レジストリ操作も **CLI のみ** (MCP ツール面 50 は不変)。

```text
lab-executor asset registry-init --dir DIR [--name STR] [--visibility team|external]
lab-executor asset publish <zip> --registry DIR [--tags a,b] [--force] [--json]
lab-executor asset catalog --registry DIR [--check] [--json]
```

### registry-init — レジストリを作る

`--visibility` は `team` (既定) か `external`。作成時に決まり、以後の変更は手動
編集。二重 init は拒否される。

### publish — 掲載ゲートを通して掲載する

`publish` は次の**掲載ゲート**を通す。ゲートを迂回する経路は無い。

1. **check pass 必須** — `asset check` の `schema_ok` かつ `checksums_ok` が真で
   あること (改ざん / 破損 zip は拒否)。
2. **external 共有ゲート** — `visibility=external` のレジストリでは、上記の
   「共有ポリシー (2026-07-07 所有者決定)」を機械化する:
   - **`level_verified <= 3`** (L3 上限。L4/L5 の再実行可能物は外部に出さない)。
   - **license が付与されている** (`UNLICENSED` / 空は拒否)。外部共有する資産には
     `asset export --license CC-BY-4.0` 等で明示的に付与する。
3. **`--force` は重複置換のみ** — 同一 `asset_id` の再 publish は既定で拒否。
   `--force` を付けると entry と zip を置換する。**`--force` でゲートはスキップ
   できない** (external の L 上限 / license は `--force` でも効く)。

掲載時、zip 全体の sha256 と資産内 recipe の `requires.commands` (発見性用) を
INDEX に記録する。`level_verified` は publish 時の check 結果であり、作成者の
自己申告 (`level_declared`) ではない。

### catalog — 一覧する

`catalog` は掲載資産を **`level_verified` 降順 → `published_at` 降順**で返す
(品質を第一ソートに。人気指標は持たない — 集中リスク対策の設計原則)。`--check` で
各 zip の sha256 を再計算し、INDEX と不一致なら `integrity: FAILED` を付ける。

### レジストリのディスク形式

```text
<registry_dir>/
├── INDEX.yaml          # registry_version / visibility / name / created_at / assets[]
└── assets/
    └── <asset_id>.asset.zip
```

`INDEX.yaml` の各 asset entry: `id` / `title` / `level_verified` / `license` /
`sha256` / `path` / `tags` / `requires_commands` / `published_at` / `producer`。

### 補足: declare_level の自動宣言 (v2.27.0)

`asset export` で `--declare-level` を省略すると、builder は**梱包物そのもの**を
check 相当で自己判定し、その値を `asset.yaml` の `level_declared` に書く (従来は 0
固定)。このため **export 直後の `asset check` で `level_declared == level_verified`**
になる。明示指定 (`--declare-level N`) は従来どおり優先する。

## 関連 docs

- [`experiment_asset_schema_v0.md`](experiment_asset_schema_v0.md) — レベルの意味論と根拠
- [`bundle_export.md`](bundle_export.md) — 内包する export bundle (bundle_version=1.0)
