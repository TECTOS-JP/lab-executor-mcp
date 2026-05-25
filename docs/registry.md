# Registry / Validation CLI 仕様メモ (v0.9.2.1)

## 用語: `vendor` ↔ `manufacturer`

**現状の使い分け** (v0.9.2.1 時点):

| 場所 | フィールド名 | 役割 |
|------|------------|------|
| `registry/INDEX.yaml` の entry | `vendor` | 一覧表示・検索用の短いメーカ名 |
| 機器定義 YAML (`metadata`) | `manufacturer` | 機器定義 schema 上の正式フィールド |

実装上、registry validation は **両者の不一致を強制チェックしない** (片方
だけ手動で揃える)。v1.0 までに片方へ統一するか docs で明示する予定。
当面は両方を併記したまま、registry INDEX の `vendor` は表示用、機器定義
YAML の `metadata.manufacturer` が正式値、と扱う。

## `support_level` の意味と現状

| level | 意味 | v0.9.2.1 の lint 扱い |
|-------|------|---------------------|
| `verified` | 実機で identify / 主要 command / state_query / verify / safe_shutdown 確認済み | **現時点では自己申告**。v1.0 で `tested_interfaces` 非空 / 主要 command 網羅 / safe_shutdown 存在を必須条件として強制する予定 |
| `tested` | mock または実機で基本 command 確認済み | warning なし |
| `experimental` | マニュアル等から作成、限定的確認 | warning なし |
| `draft` | 未検証 | `support_level_draft` warning を出す |

`invalid_support_level`:

- **機器定義単体** (`visa-mcp validate instrument`) では **warning**
- **registry INDEX validation** (`visa-mcp validate registry`) では **error**
  (掲載時の品質ゲート)

## `visa-mcp validate plan` の検証範囲

```bash
visa-mcp validate plan <plan.json>
```

**Pydantic schema validation のみ** (`ExperimentPlan(**data)`)。以下は **行わない**:

- system_config / `experiment_units` の参照解決
- instrument 定義の command 存在チェック
- parameter range / safety 制約
- resource availability
- sweep / parallel 展開

これらは **MCP tool `validate_experiment_plan`** が `validate_and_compile`
経由で行う。CLI は軽量な構文確認用と整理してください。

v1.0 候補:

```bash
visa-mcp validate plan plan.json --system _system.yaml --instruments instruments/
```

(compile-level validation を CLI でも可能にする経路)

## `visa-mcp validate registry` の検証範囲

v0.9.2.1 で強化:

| 検証 | error / warning |
|------|----------------|
| INDEX entry の必須項目 (id/vendor/model/category/path) | error (`registry_entry_missing_field`) |
| INDEX 内の重複 `id` | error (`registry_duplicate_id`) |
| INDEX path の指す機器定義 YAML が存在 | error (`registry_entry_path_not_found`) |
| path が registry/ 配下 | warning (`registry_path_outside_registry`) |
| INDEX entry の `support_level` 語彙 | error (`invalid_support_level`) |
| 各機器定義 YAML の schema | error |
| 各機器定義の lint (missing safe_shutdown / verify 等) | warning |
| INDEX の support_level と YAML の support_level の不一致 | warning |

## 将来 (v1.0 以降) の検討

- `visa-mcp lint instrument` を `validate` から分離 (strict mode 用)
- `validate plan --system` で compile-level 検証
- `support_level=verified` の必須条件強化 (上記参照)
- `vendor` / `manufacturer` の用語統一
- `visa-mcp validate repo-format` (raw 改行 / YAML pretty / Markdown 検査)
