# lab-executor-mcp

Backend-independent **experiment execution runtime** for AI agents.
Split from `visa-mcp` at v2.0 (see `docs/v2_migration.md`).

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

## v2.0 split

- v1.x までは `visa-mcp` 1 リポジトリで提供されていた
- v2.0 で **backend (visa-mcp) と runtime (lab-executor-mcp) を分離**
- MCP tool / DSL schema / extension pack 形式は完全互換
- 旧 `from visa_mcp.extension import ...` は v2.0 で
  DeprecationWarning 付きで動作

詳細: `docs/v2_migration.md`

## License

MIT
