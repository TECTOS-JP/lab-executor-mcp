# visa-mcp Quickstart (English, v0.9.2 draft)

`visa-mcp` is an MCP server that lets AI agents (Claude Desktop / Claude Code /
any MCP client) drive **GPIB / USB / Serial / LAN measurement instruments**
through PyVISA, using **structured YAML instrument definitions** and a
**JSON-based experiment DSL** — no arbitrary Python execution, no raw SCPI by
default.

> **Status: experimental / pre-v1.0.** APIs are stable for the listed
> "Stable" tools (see [`docs/compatibility.md`](../compatibility.md));
> everything else may change before v1.0.

## Why MCP for AI experiment automation?

Letting an LLM drive lab instruments raises three concrete risks:

1. **Wrong command / wrong value** — the agent may try to write
   `VOLT 999` to a 30 V supply.
2. **No observability** — long-running experiments become opaque.
3. **No reproducibility** — what was actually executed?

`visa-mcp` addresses these with:

- **YAML instrument definitions** (declared command set, parameter ranges,
  enums, safety constraints, `safe_shutdown`)
- **DSL plans** (validated and `dry_run`-able before execution)
- **Job model** with state machine, cancel, resume, observation
- **3-layer benchmark** (validate / dry_run / mock execution) — LLM-free
  regression testing
- **Self-repair fixtures** — define what counts as a correct fix

## Core workflow

```text
                 ┌──────────────────────────────────────────┐
LLM / agent ───▶ │ validate_experiment_plan(plan)           │
                 │ dry_run_plan(plan)                       │
                 │ start_experiment_job(plan) → job_id      │
                 └──────────────────────────────────────────┘
                              │
                              ▼
                 ┌──────────────────────────────────────────┐
                 │ get_job_live_view(job_id)                │
                 │ get_experiment_timeline(job_id)          │
                 └──────────────────────────────────────────┘
                              │
                              ▼
                 ┌──────────────────────────────────────────┐
                 │ get_job_summary(job_id)                  │
                 │ get_experiment_results(job_id)           │
                 │ export_experiment_results(job_id, csv)   │
                 └──────────────────────────────────────────┘
```

## 60-second install

```bash
git clone https://github.com/TECTOS-JP/visa-mcp.git
cd visa-mcp
pip install -e .
```

Requires Python 3.10+ and a VISA library (NI-VISA / Keysight IO Libraries /
PyVISA-Py).

## Register with Claude Desktop

`%APPDATA%\Claude\claude_desktop_config.json` (Windows) or
`~/Library/Application Support/Claude/claude_desktop_config.json` (macOS):

```json
{
  "mcpServers": {
    "visa-mcp": {
      "command": "python",
      "args": ["-m", "visa_mcp.server"],
      "cwd": "<path-to-visa-mcp>"
    }
  }
}
```

## Try it without real instruments

```bash
# CLI validation
visa-mcp validate registry registry/INDEX.yaml --json
visa-mcp validate instrument registry/instruments/mock/mock_psu.yaml

# Run a benchmark task end-to-end with mocked instruments
python -c "
import asyncio
from visa_mcp.testing.benchmark_runner import run_task_file
print(asyncio.run(run_task_file(
    'benchmarks/tasks/task_002_unit_based_voltage_sweep.yaml',
    'benchmarks', '/tmp/bm',
)).status)
"
```

## Safety notes

- **Default safety mode is `strict`.** Out-of-range parameters and undefined
  commands are rejected.
- **`dry_run_plan` performs zero VISA I/O.** Always dry-run before executing.
- **`override_safety` requires human approval.** AI agents must not call it
  alone; the server `instructions` make this explicit.
- **`safe_shutdown` is best-effort and always logged.**
- **Resume creates a new Job** rather than mutating the original.

## Next steps

- [`docs/jobs.md`](../jobs.md) — Job model, cancel modes, resume
- [`docs/benchmark_repair.md`](../benchmark_repair.md) — Self-repair benchmark
- [`docs/result_export.md`](../result_export.md) — get/export experiment results
- [`docs/compatibility.md`](../compatibility.md) — Stable vs experimental APIs
- [`docs/error_taxonomy.md`](../error_taxonomy.md) — `error_class` for agent
  self-repair
