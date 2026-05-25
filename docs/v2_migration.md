# v2.0 Migration Guide

> v1.x ユーザーが v2.0 へ移行するための手順書。両 repo
> (`visa-mcp` / `lab-executor-mcp`) で同一内容を配置する。

## 何が変わるか

v1.x までは `visa-mcp` 1 リポジトリで以下すべてを提供していた:

```
PyVISA backend  +  実験実行 runtime  +  DSL  +  extension ecosystem
```

v2.0 で **2 リポジトリに分離**:

```
lab-executor-mcp     ← runtime + DSL + extension + benchmark
visa-mcp             ← PyVISA backend + raw VISA + 旧 import shim
```

依存方向:

```
visa-mcp  →  lab-executor-mcp     (許可)
lab-executor-mcp  →  visa-mcp     (禁止)
```

## 既存ユーザー (実機を使う)

特別な対応は不要。`pip install --upgrade visa-mcp` だけで動く。

```bash
pip install --upgrade visa-mcp
# 自動的に lab-executor-mcp >= 2.0 も install される
```

旧 import path はすべて動作する (`DeprecationWarning` 付き):

```python
from visa_mcp.extension import ExtensionManifest  # DeprecationWarning
from visa_mcp.dsl import validate_experiment_plan  # DeprecationWarning
```

推奨される新 import:

```python
from lab_executor.extension import ExtensionManifest
from lab_executor.dsl import validate_experiment_plan
```

## 新規ユーザー (実機なしで benchmark / dry-run のみ)

```bash
pip install lab-executor-mcp
# PyVISA 不要
```

ただし、実機との通信は `visa-mcp` が必要。

## MCP tool

完全互換。Stable 43 + Experimental 7 = 50 で v1.0 から不変。
tool 名 / 引数 / response envelope すべて同じ。

## extension pack

完全互換。v1.x で作成した `.visa-mcp-ext.zip` は v2.0 でも
そのまま install できる。

```bash
lab-executor extension install my-pack.visa-mcp-ext.zip
# または旧 CLI 経由
visa-mcp extension install my-pack.visa-mcp-ext.zip   # DeprecationWarning
```

## install path

v2.0 では `~/.visa-mcp/extensions/` を継続使用する。v2.1 以降で
`~/.lab-executor/extensions/` への移行計画を提示予定。

## CLI

| v1.x | v2.0 推奨 | v2.0 互換 |
|------|-----------|----------|
| `visa-mcp serve` | `visa-mcp serve` (互換) | ✓ |
| `visa-mcp validate ...` | `lab-executor validate ...` | `visa-mcp validate` も動作 (warning) |
| `visa-mcp extension ...` | `lab-executor extension ...` | 同上 |
| `visa-mcp instrument ...` | `lab-executor instrument ...` | 同上 |

## DSL schema

`dsl_version=0.8` 完全互換。v1.x の plan / template は何も
変更せずに v2.0 lab-executor で実行できる。

## bundle / export

`export_experiment_bundle` の zip 形式は v1.0 から不変。
v1.x で作った bundle は v2.0 で `validate_experiment_bundle` /
`inspect_experiment_bundle` ともに通る。

## トラブルシューティング

### `ImportError: cannot import name X from visa_mcp...`

v2.0 で完全に削除された API は無い。`DeprecationWarning` のみ。
このエラーは extension pack の dependency 不整合の可能性が高い。
`lab-executor extension check` で診断する。

### `pyvisa not found` が出る

`lab-executor-mcp` 単独 install ではこれは正常。実機を使うなら:

```bash
pip install visa-mcp
```

### v2.0 で動作しなくなった

v2.0 では MCP tool / DSL / extension pack 形式を変えていない。
動作差異が出る場合は `TECTOS-JP/lab-executor-mcp` / `TECTOS-JP/visa-mcp`
のいずれかに issue を立ててほしい。

## Roadmap

- v2.0:    分離本番、旧 import は warning 付きで動作
- v2.1:    migration 状況 review、`~/.lab-executor/extensions/` 並走計画
- v2.2+:   旧 import path 削除候補 (実利用状況を見て判断)

詳細: `docs/separation/notes.md` / `docs/raw_visa.md`
