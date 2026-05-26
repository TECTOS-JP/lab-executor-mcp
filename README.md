# lab-executor-mcp

[![CI](https://github.com/TECTOS-JP/lab-executor-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/TECTOS-JP/lab-executor-mcp/actions/workflows/ci.yml)

Backend-independent **experiment execution runtime** for AI agents.
Split from `visa-mcp` at v2.0.

> **⚠ Upgrading from visa-mcp v1.x?**
> **First read [`docs/v2_migration.md`](docs/v2_migration.md).**
> 実機 backend が必要な既存利用者は **`pip install --upgrade visa-mcp`** が
> 互換経路 (v2.0 では visa-mcp v2.0 release を待つ必要あり)。

> **Line-ending note**: GitHub raw view may display some files as
> "collapsed" / "1 line" in certain viewers. The repository uses
> `.gitattributes` to enforce LF, and CI validates **TOML / YAML
> parse**, **`compileall`**, **multiline / LF-only guard** on every
> commit. See `tests/test_v200_split.py`.

## What it provides

- DSL (`ExperimentPlan`, `dsl_version=0.8`) + validator + dry-run
- Job manager / state machine / scheduler / barrier
- Observation API (`timeline` / `live_view` / `summary`)
- Benchmark runner (MockBackend, PyVISA 不要)
- Definition pack ecosystem (`extension init/install/check/package/...`)
- Instrument authoring (`instrument scaffold/promote-check/review-report`)
- Export / bundle (deterministic reproducibility)
- MCP tool surface: Stable 43 + Experimental 7 = 50 (v1.0 から不変)

## Install

```bash
pip install lab-executor-mcp
```

PyVISA は **必須ではない**。実機 backend が必要な場合は
`visa-mcp` を install すると自動的に `lab-executor-mcp` も入る。

```bash
pip install visa-mcp     # PyVISA backend + lab-executor-mcp runtime
```

## How to start the server (v2.1+)

| 用途 | コマンド | PyVISA | backend |
|---|---|---|---|
| Mock / dry-run / benchmark / validation | `lab-executor serve --backend mock` | 不要 | `MockBackend` |
| 実機 (PyVISA 経由) | `visa-mcp serve` | 必要 | `PyVisaBackend` (visa-mcp 同梱) |

### Quick examples

```bash
# 1) Server を compose して tool 一覧を確認する (transport 起動なし)
lab-executor serve --backend mock --dry-run

# 2) 実 transport を起動 (Claude Code / MCP client から接続)
lab-executor serve --backend mock

# 3) Extension pack を検証
lab-executor validate extension my_pack/extension.yaml --strict
lab-executor extension doctor my_pack/

# 4) Package + verify
lab-executor extension package my_pack/ --output my_pack.visa-mcp-ext.zip
lab-executor extension verify-package my_pack.visa-mcp-ext.zip
```

### Exit code policy (v2.1.1)

| Subcommand | exit 0 | exit 1 | exit 2 |
|---|---|---|---|
| `serve` | server 起動 / `--dry-run` 成功 | — | `--backend` 未指定 / 不正 |
| `validate {instrument,extension}` | errors == 0 | errors > 0 | usage error |
| `extension doctor` | `status == "ok"` | warnings / errors あり | usage error |
| `extension package` | package 成功 | 失敗 | usage error |
| `extension verify-package` | checksums OK | mismatch / 不正 | usage error |

`doctor` は warning だけでも exit 1 になります (CI で fail させる
強い gate として設計)。warning を許容したい場合は `doctor` 出力を
`--json` で取り、`errors` のみを判定してください。

### Notes

- `lab-executor serve` は **`--backend mock` が必須** (引数なしは
  exit 2 で `visa-mcp serve` への案内)
- 他 backend (REST / replay / plugin) は v2.2+ で検討
- runtime 内部の `JobManager` は v2.1 時点で `visa=` 引数名を受け取る
  (v1.x からの互換維持)。v2.2+ で `backend=` への rename を検討予定

## CLI status in v2.1

v2.1.0 で活性化された範囲:

- `lab-executor --version` / `--help`
- `lab-executor serve --backend mock` (MockBackend で MCP server 起動)
- `lab-executor serve --backend mock --dry-run` (server を compose
  して tool 一覧を出すだけ)
- `lab-executor validate instrument <path>`
- `lab-executor validate extension <path>`
- `lab-executor extension doctor <pack_dir>`
- `lab-executor extension package <pack_dir>`
- `lab-executor extension verify-package <zip>`

v2.2+ で port 予定:

- `lab-executor extension init / install / catalog`
- `lab-executor instrument scaffold / review-report`
- 他 backend (REST / replay / plugin) の `--backend` choice 追加

実機 backend を起動する経路は引き続き **`visa-mcp serve`** (visa-mcp
v2.0+ を install)。

## v2.0 split

- v1.x までは `visa-mcp` 1 リポジトリで提供されていた
- v2.0 で **backend (visa-mcp) と runtime (lab-executor-mcp) を分離**
- MCP tool / DSL schema / extension pack 形式は完全互換
- 旧 `from visa_mcp.extension import ...` は v2.0 で
  DeprecationWarning 付きで動作

詳細: `docs/v2_migration.md`

## License

MIT
