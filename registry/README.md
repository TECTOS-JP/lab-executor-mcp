# visa-mcp instrument definition registry (v0.9.2, experimental)

外部利用・再利用・検証のための機器定義レジストリ skeleton。
v0.9.2 では「**少数の代表定義 + mock 定義**」を載せ、`support_level` で品質を
表現する。v1.0 で初期公開を目指す。

## 構造

```
registry/
├── README.md
├── INDEX.yaml          # 機器定義の一覧 (id / vendor / model / category /
│                       # support_level / path)
└── instruments/
    ├── power_supplies/
    ├── dmms/
    ├── thermometers/
    └── mock/
```

## `support_level`

| level | 条件 |
|-------|------|
| `verified`     | 実機で identify / 主要 command / state_query / verify / safe_shutdown を確認済み |
| `tested`       | mock または実機で基本 command を確認済み |
| `experimental` | マニュアル・仕様書から作成、限定的に動作確認 |
| `draft`        | 未検証 (Plan 生成時に注意推奨、AI エージェントは support_level=draft を強い警告で扱うことを推奨) |

## Registry 掲載条件 (v0.9.2)

最小条件:

- `instrument.schema.json` の schema validation を通る
- `metadata.manufacturer` / `model` / `category` / `support_level` あり
- write command がある機器は `safety` 制約と `safe_shutdown` を持つ
- AI エージェントが利用しやすいよう `state_query` を最低 1 つ定義

`support_level` 別の追加条件:

- `verified`: 実機で identify / command / state_query / verify / safe_shutdown 確認済み
- `tested`: mock または実機で主要 command の往復確認済み
- `experimental`: basic command + safety constraints 定義済み
- `draft`: schema validation のみ

## CLI 検証

```bash
visa-mcp validate registry registry/INDEX.yaml --json
visa-mcp validate instrument registry/instruments/mock/mock_psu.yaml
```

`--json` 付きで CI 向け machine-readable 出力。

## 追加方法

1. `instruments/<category>/<vendor>_<model>.yaml` を作成
2. metadata に `support_level` を明示
3. `INDEX.yaml` に entry を追加 (id / vendor / model / category /
   support_level / path)
4. `visa-mcp validate registry registry/INDEX.yaml` で confirm
5. `visa-mcp validate instrument <yaml>` で lint warning を確認
