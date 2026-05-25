# Extension Policy (v1.2)

合言葉: **「plugin を実装する前に、何を拡張可能にするのかを固定する」**

## Current status (v1.2)

`visa-mcp` は **executable third-party plugin を v1.2 ではサポートしない**。
拡張は **data-driven (YAML / JSON / Schema-validated)** な
**definition pack** に集約する。

## Supported extension surfaces (v1.2)

| Surface | 形式 | 検証手段 |
|---------|------|---------|
| Instrument definitions | YAML | `visa-mcp validate instrument` + lint |
| Registry entries (`INDEX.yaml`) | YAML | `visa-mcp validate registry` |
| Benchmark tasks (`benchmarks/tasks/*.yaml`) | YAML | `visa-mcp validate benchmark` |
| Repair benchmark tasks (`benchmarks/repair/*/task.yaml`) | YAML | 同上 (`layer: repair`) |
| Experiment templates (`*.json`) | JSON | `visa-mcp validate plan` |
| Mock scenarios (`fixtures/...`) | YAML | benchmark runner で実行 |
| **Definition pack manifest** (`extension.yaml`) | YAML | `visa-mcp validate extension` (v1.2 新規) |

## Not supported in v1.2 (intentionally)

- Python plugin auto-loading
- `entry_points` ベースの plugin discovery
- Backend plugins (PyVISA 以外の実 backend)
- 任意 user Python code 実行 (custom DSL step / evaluator function)
- Remote registry / registry pull CLI
- Third-party MCP tool injection
- Bundle replay / import as active job
- Human intent / approval

## Why "definition" not "plugin"

| Definition (data) | Plugin (code) |
|------------------|---------------|
| Schema validation 可能 | security model 必要 |
| lint 可能 | version compatibility 難 |
| CI 可能 | sandbox / lifecycle 必要 |
| AI エージェントが読める | 任意コード実行リスク |
| 安全性検証しやすい | v1.0 stable core を揺らす |

v1.2 では「拡張はまず data-driven に寄せる」方針を採用。

## Stability

| 拡張対象 | v1.x ステータス |
|---------|---------------|
| YAML / JSON 定義 (instrument / registry / benchmark / template) | **stable** (各 schema 経由) |
| `extension.yaml` (definition pack manifest) | **experimental** (v1.2 新規) |
| Backend abstraction Protocol | **experimental spike** (v1.1〜) |
| Executable plugin | **未対応 (v1.x 内では対応予定なし)** |

## v1.3+ 候補 (informal)

- Definition pack の registry pull CLI
- Replay backend (`docs/replay_backend_concept.md`)
- Plugin entry_points discovery (sandboxed)
- Custom DSL step type 登録 API (signed only)

これらはいずれも **v1.2 では未対応**。

## 関連 docs

- [`definition_packs.md`](definition_packs.md) — `extension.yaml` 仕様
- [`registry_contribution.md`](registry_contribution.md) — 機器定義追加手順
- [`backend_abstraction.md`](backend_abstraction.md) — backend Protocol
- [`replay_backend_concept.md`](replay_backend_concept.md) — replay 設計メモ
- [`v1_stability_policy.md`](v1_stability_policy.md) — v1.x 互換保証
