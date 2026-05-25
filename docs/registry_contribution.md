# Registry contribution workflow (v1.2)

外部ユーザーが `visa-mcp` の機器定義 registry へ contribute する手順。
v1.2 では PR ベースの定義追加を主経路とする
(remote registry / pull CLI は未対応)。

## 1. 機器定義 YAML を作成

`registry/instruments/<category>/<vendor>_<model>.yaml`:

```yaml
metadata:
  manufacturer: YourVendor
  model: ModelXYZ
  category: power_supply            # power_supply / dmm / thermometer 等
  description: ...
  manual_ref: "https://..."
  support_level: tested              # 後述
  tested_interfaces: ["USB", "LAN"]  # 動作確認済み interface
  tested_firmware: "1.23"            # optional
  definition_version: "0.1.0"        # 機器定義 YAML 自体の改訂番号

identification:
  manufacturer_match: "YourVendor"
  model_regex: "ModelXYZ"

connection:
  default_timeout_ms: 3000
  read_termination: "\n"
  write_termination: "\n"

commands:
  set_voltage:
    scpi: "VOLT {voltage}"
    type: write
    parameters:
      - name: voltage
        type: float
        range: [0, 30]
    verify:
      readback_command: query_voltage
      arg_key: voltage
      tolerance: 0.05

  query_voltage:
    scpi: "MEAS:VOLT?"
    type: query
    polling_safe: true

  # 出力系機器なら set_output (ON/OFF) も推奨

state_query:
  voltage:
    command: query_voltage
    unit: V

safe_shutdown:
  - command: set_output
    args: { state: "OFF" }
```

## 2. `support_level` を選ぶ

| level | 条件 | contribution に求められる evidence |
|-------|------|--------------------------------|
| `verified` | 実機で identify / 主要 command / state_query / verify / safe_shutdown を確認済み | テスト記録 (`tested_by` / 日付 / interface / firmware) を PR に添付 |
| `tested` | mock または実機で基本 command を確認済み | mock benchmark task を 1 件以上同梱 |
| `experimental` | マニュアル / 仕様書から作成、限定的に動作確認 | (任意) 推測ベースであることを description に明記 |
| `draft` | 未検証 | schema validation 通過のみ。AI エージェント Plan 生成時は注意推奨 |

## 3. ローカル検証

```bash
# 単体 schema + lint
visa-mcp validate instrument registry/instruments/your_path.yaml

# registry 全体整合
visa-mcp validate registry registry/INDEX.yaml
```

## 4. `INDEX.yaml` に entry を追加

```yaml
instruments:
  - id: yourvendor_modelxyz
    vendor: YourVendor
    model: ModelXYZ
    category: power_supply
    support_level: tested
    path: instruments/power_supplies/yourvendor_modelxyz.yaml
```

`id` は **registry 内で一意**。`vendor` と `metadata.manufacturer` の
用語ずれは [`registry.md`](registry.md) 参照。

## 5. PR 提出チェックリスト

```text
- [ ] visa-mcp validate instrument <yaml> がエラーなし
- [ ] visa-mcp validate registry registry/INDEX.yaml がエラーなし
- [ ] metadata.support_level が宣言されている
- [ ] 出力系機器は safe_shutdown が定義されている
- [ ] write command は safety / verify が可能な範囲で定義されている
- [ ] state_query が最低 1 つある
- [ ] tested_interfaces が記入されている (verified / tested 申告時)
- [ ] firmware / version が分かれば documented
- [ ] support_level=verified の場合、testing evidence を PR description に記載
```

## 6. definition pack として配布する場合

複数機器定義を独立リポジトリで配布する場合は、
[`definition_packs.md`](definition_packs.md) の `extension.yaml` 形式を
使うこと。

## v1.3+ candidate (未対応)

- `visa-mcp install extension <url>` 自動取得 CLI
- Remote registry index
- 自動 lint CI for community contributions

これらは definition pack 普及後の判断。

## 関連 docs

- [`registry.md`](registry.md) — registry / `support_level` / `vendor`vs
  `manufacturer` 用語
- [`extension_policy.md`](extension_policy.md) — v1.2 拡張ポリシー
- [`definition_packs.md`](definition_packs.md) — `extension.yaml`
