# 変更履歴

## v2.0.0-rc1 — Initial split candidate from visa-mcp v1.11.1

lab-executor-mcp の最初の release candidate。`visa-mcp` v1.11.1 から
runtime / DSL / ecosystem layer を切り出した。

### 含まれるもの

- DSL (`ExperimentPlan`, `dsl_version=0.8`) + validator + dry-run
- Job manager / state machine / scheduler / barrier
- Group / Map executor
- Observation API
- Benchmark runner + repair tasks + 5 fixture tasks
- Definition pack ecosystem (extension init/install/check/package/catalog/authoring)
- Instrument authoring (scaffold/promote-check/review-report)
- Export / bundle (deterministic reproducibility)
- Audit / locks / SQLite (user_version=3)
- `InstrumentBackend` Protocol + `MockBackend`
- MCP tool: Stable 43 + Experimental 7 = 50 (v1.0 から不変)

### 含まれないもの (visa-mcp 側に残る)

- `PyVisaBackend` (PyVISA 透過 adapter)
- `VisaManager` / `bus_manager` / `session_manager`
- Raw VISA tools (`send_command` / `query_instrument`, env-gated)
- `tools/discovery.py` (PyVISA resource 列挙)

### 互換性

- DSL `dsl_version=0.8` 完全互換
- extension pack 形式 (`.visa-mcp-ext.zip`) 完全互換
- `.install_meta.json` schema 完全互換
- `~/.visa-mcp/extensions/` install path 継続使用 (v2.x で再評価)
- MCP tool 名 / 引数 / response: v1.0 凍結のまま

### 依存関係

- PyVISA: **不要** (`pip install lab-executor-mcp` で動く)
- 実機 backend が必要なら `pip install visa-mcp` を追加 install

### 履歴

履歴は visa-mcp v1.11.1 から **切り出して新規 repo として開始**
(git filter-repo による history rewrite は行わない)。
visa-mcp の git log は引き続き `TECTOS-JP/visa-mcp` を参照。

### Source of truth

- `docs/separation/module_ownership.yaml` (visa-mcp v1.11.1)
- `docs/separation/split_manifest.yaml` (visa-mcp v1.11.1)
- bootstrap script: `scripts/bootstrap_lab_executor.py` (visa-mcp)
