# Instrument Category Policy (v1.9, experimental)

合言葉: **「同じ機器を 1 つの canonical category 名で表す」**

v1.7 / v1.8 で `instrument scaffold` の category と `metadata.category`
の表記揺れ (例: `dmm` vs `multimeter`) が問題になった。v1.9 で
**canonical category + alias 正規化**を導入し、CLI / scaffold /
registry / overlay / strict validation / catalog すべてで同じ値を
使えるようにする。

## Canonical category 一覧 (v1.9)

| canonical | 説明 | output-capable? |
|-----------|------|-----------------|
| `power_supply` | プログラマブル DC 電源 | **Yes** |
| `smu` | Source-Measure Unit | **Yes** |
| `function_generator` | Function / signal generator | **Yes** |
| `electronic_load` | Electronic load (sink) | **Yes** |
| `temperature_controller` | 温度制御 (PID 等で actual 加熱 / 冷却) | **Yes** |
| `heater` | ヒーター制御 | **Yes** |
| `actuator` | 機械 actuator (motor / valve 等) | **Yes** |
| `dmm` | デジタルマルチメーター | No |
| `temperature_meter` | 温度測定 (制御しない、読み取り専用) | No |
| `oscilloscope` | オシロスコープ | No |
| `logger` | データロガー | No |
| `generic_scpi` | カテゴリ未確定 / 汎用 SCPI | No |

**output-capable** ⇒ `validate instrument --strict` で
`safe_shutdown` / `safety.ratings` 必須化される。

## Alias 表 (v1.9)

`visa_mcp.registry.CATEGORY_ALIASES` で正規化される:

| alias (受け取り側) | canonical |
|-------------------|-----------|
| `multimeter` | `dmm` |
| `digital_multimeter` | `dmm` |
| `psu` | `power_supply` |
| `function_gen` | `function_generator` |
| `fg` | `function_generator` |
| `eload` | `electronic_load` |
| `tc` | `temperature_controller` |

新しい alias を増やす場合は `src/visa_mcp/registry.py` の
`CATEGORY_ALIASES` に追記する。**逆方向 (canonical → alias) は
持たない**: scaffold / template / docs の出力は常に canonical 名を使う。

## どこで `normalize_category()` が呼ばれるか

| 利用箇所 | 振る舞い |
|---------|---------|
| `validate instrument --strict` の output-capable 判定 | 入力 alias を canonical に変換してから `OUTPUT_CAPABLE_CATEGORIES` をチェック |
| `instrument promote-check` | 同上 (strict validate を再利用) |
| `instrument scaffold <category>` (CLI) | **canonical のみ受け付ける** (alias を CLI で許可すると逆方向の混乱を招くため) |
| `extension add-instrument --category <category>` | 同上 |
| `validate extension` registry_entries strict | 同上 |

## scaffold 生成 YAML の `metadata.category`

v1.8.1 で全 template の `metadata.category` を canonical 名に揃え済み:

| template | metadata.category |
|----------|-------------------|
| `power_supply` | `power_supply` |
| `dmm` | `dmm` (v1.8.1 で `multimeter` から修正) |
| `temperature_meter` | `temperature_meter` |
| `generic_scpi` | `generic_scpi` |

## v1.10+ への TODO

- registry INDEX.yaml の lint で alias 検出 → warning
- catalog tags との重複防止 (例: tag に `psu` を入れない、`power-supply`
  のようなハイフン版は別物として認める)
- v2.0 lab-executor-mcp 側へ移行する際、`category_policy.py` として
  独立 module に切り出す候補 ([`separation/notes.md`](separation/notes.md)
  の registry.py 分割案と整合)
- 新しい category (例: `network_analyzer` / `spectrum_analyzer` /
  `lcr_meter`) を追加する場合のレビュープロセスを CONTRIBUTING に追加

## v1.9 で **やらない**こと

- canonical 名の **rename** (`dmm` → `digital_multimeter` 等)
  既存利用者の YAML を壊すため
- alias 廃止 (warning を出すのみ)
- category の階層化 (`measurement.dmm` / `source.power_supply` 等)
  必要性が出てから検討

## 関連 docs

- [`instrument_authoring.md`](instrument_authoring.md)
- [`instrument_promote_check.md`](instrument_promote_check.md)
- [`extension_catalog.md`](extension_catalog.md)
- [`separation/notes.md`](separation/notes.md) — registry.py 分割候補
- [`error_taxonomy.md`](error_taxonomy.md)
