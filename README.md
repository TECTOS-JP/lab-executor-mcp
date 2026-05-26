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

## CLI status in v2.0

`lab-executor` CLI は v2.0 時点で **minimal** な構成:

- `lab-executor --version` / `--help`: available
- `lab-executor validate instrument <path>`: available
- `lab-executor serve`: **placeholder** (v2.1 で MCP server 起動を実装)
- v1.x `visa-mcp` CLI との完全互換 (`extension` / `registry` /
  `instrument scaffold` 等): **v2.1+** で段階的 port 予定

実機 backend を起動する `visa-mcp serve` 互換は visa-mcp 側の CLI で
従来通り提供される (`pip install visa-mcp` 後 `visa-mcp serve`)。

## v2.0 split

- v1.x までは `visa-mcp` 1 リポジトリで提供されていた
- v2.0 で **backend (visa-mcp) と runtime (lab-executor-mcp) を分離**
- MCP tool / DSL schema / extension pack 形式は完全互換
- 旧 `from visa_mcp.extension import ...` は v2.0 で
  DeprecationWarning 付きで動作

詳細: `docs/v2_migration.md`

## License

MIT
