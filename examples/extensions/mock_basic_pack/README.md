# mock_basic_pack (v1.2 example definition pack)

`visa-mcp` v1.2 で導入された **definition pack** の最小例。Python code を
含まず、機器定義 YAML 2 件 + benchmark task 1 件をまとめている。

## 構成

```text
extension.yaml
instruments/
  mock_psu.yaml
  mock_dmm.yaml
benchmarks/
  task_001.yaml
```

## 検証

```bash
visa-mcp validate extension examples/extensions/mock_basic_pack/extension.yaml --json
```

## 何が証明されるか

- `executable_code: false` + `type: definition_pack` のみ通る
- 参照ファイル全てが extension.yaml 配下に存在
- 各 instrument / benchmark が schema validation を通る
- `support_level: tested` が宣言されている

詳細仕様は [`docs/definition_packs.md`](../../../docs/definition_packs.md)
を参照。
