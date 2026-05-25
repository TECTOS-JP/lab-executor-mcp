# Dependency Graph Report (v1.10, auto-generated)

> `python -m visa_mcp.dev.ownership_check --graph-md docs/separation/dependency_graph.md` で再生成。
> 手で編集しない。

## Statistics

- declared modules: 73
- actual modules: 73
- unclassified: 0
- manifest ghosts: 0
- edges total: 168
- lab→visa top-level violations: 0
- lazy exceptions: 1

## Owner counts

| owner | count |
|-------|-------|
| lab-executor-mcp | 59 |
| shared | 2 |
| split | 8 |
| visa-mcp | 4 |

## ✅ No NEW lab→visa top-level violations

## Known v1.11-to-resolve (10 件)

v1.11 で InstrumentBackend Protocol 経由化により解消する 既知の violation。新規追加禁止 / 削減のみ。

| from | to | resolve at | method |
|------|----|-----------|--------|
| visa_mcp.dsl.compiler | visa_mcp.session_manager | v1.11 | InstrumentBackend Protocol 経由化 |
| visa_mcp.group.executor | visa_mcp.session_manager | v1.11 | InstrumentBackend Protocol 経由化 |
| visa_mcp.group.executor | visa_mcp.visa_manager | v1.11 | InstrumentBackend Protocol 経由化 |
| visa_mcp.job.manager | visa_mcp.session_manager | v1.11 | InstrumentBackend Protocol 経由化 |
| visa_mcp.job.manager | visa_mcp.visa_manager | v1.11 | InstrumentBackend Protocol 経由化 |
| visa_mcp.testing.benchmark_runner | visa_mcp.session_manager | v1.11 | InstrumentBackend Protocol 経由化 |
| visa_mcp.tools.dsl | visa_mcp.session_manager | v1.11 | InstrumentBackend Protocol 経由化 |
| visa_mcp.tools.info | visa_mcp.session_manager | v1.11 | InstrumentBackend Protocol 経由化 |
| visa_mcp.tools.info | visa_mcp.visa_manager | v1.11 | InstrumentBackend Protocol 経由化 |
| visa_mcp.tools.recipes | visa_mcp.session_manager | v1.11 | InstrumentBackend Protocol 経由化 |
