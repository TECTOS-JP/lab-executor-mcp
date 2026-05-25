# Concepts (English, v0.9.2 draft)

A short index of the vocabulary used by `visa-mcp`.

## Instrument definition

YAML file describing one instrument's command set. Key sections:

| section | purpose |
|---------|---------|
| `metadata` | manufacturer / model / category / **`support_level`** (verified / tested / experimental / draft) |
| `commands` | named SCPI commands with parameter type + range + enum |
| `safety` | hard limits (`absolute_max`, `recommended_max`, preconditions) |
| `state_query` | how to read current state (used by `get_state`, `wait_for_condition`) |
| `verify` | read-back configuration to confirm a write took effect |
| `safe_shutdown` | sequence to bring the instrument to a safe state |

See `registry/instruments/mock/*.yaml` for minimal references.

## `system_config` / `experiment_units`

`_system.yaml` maps a stable **alias** to a VISA resource, and groups multiple
instruments into an **`experiment_unit`** — a set of roles
(e.g. `{psu, dmm, temp}`) that an experiment plan can reference by `unit:`.

## ExperimentPlan (DSL)

JSON / YAML document the agent generates. Validated by Pydantic
(`dsl_version: "0.8"`, 10 step types). Highlights:

- `unit` + `bindings` (role → resource override)
- `command` / `query` / `wait` / `wait_for_condition` / `wait_for_stable`
- `barrier` / `sweep` / `parallel`
- `safe_shutdown` (final cleanup)

Compile flow:

```text
plan_dict → validate_and_compile() → CompiledPlan {
  valid, errors, warnings,
  main_plan (Plan IR), parallel_groups,
  rendered_steps (dry-run preview),
  unit_resolution {unit_bindings, explicit_bindings, effective_bindings,
                   overridden_roles}
}
```

## Job model

A `Job` has a state machine:

```text
queued → running → waiting → completed
                          → failed
                          → cancelling → cancelled
                          → timeout
                          → interrupted (server restart)
```

`current_phase` (v0.8.2) gives a finer-grained label for observation:
`waiting_for_stable / barrier_wait / polling / monitoring / partial_failure
/ ...`.

`job_outcome` (v0.8.2.1) is independent of `job_status` and may be
`success / partial_failure / failure / cancelled / interrupted`.

## Benchmark task (3-layer)

```yaml
layer: validate | dry_run | execute | repair
```

Each task carries:

- `input`: plan or template_name + override
- `expected.plan_features`, `required_tool_sequence`, `success_criteria`
- `fixtures.system_config`, `instruments`, `mock_scenarios`,
  `random_seed`, `safety_mode`

For `layer: repair`, additional sections:

- `broken_plan` + `expected_failure`
- `repaired_plan` + `expected_repair` (incl. `must_not` for safety-bypass
  detection)

## Observation API

| tool | purpose |
|------|---------|
| `get_job_status` | low-level status / queue info |
| `get_job_live_view` | running snapshot (phase / latest_measurements / active_waits) |
| `get_experiment_timeline` | normalized event timeline (paginated cursor) |
| `get_job_summary` | terminal summary (key_results / failures / verify_summary / recommended_next_actions) |

## Results

| tool | scope |
|------|-------|
| `get_job_result` | raw final result dict |
| `get_experiment_results` | small, paginated, normalized rows |
| `export_experiment_results` | CSV / JSONL file, sha256, sandboxed path |
| `get_monitor_data` | detailed monitor time-series |
| `export_experiment_bundle` (v1.0) | full reproducibility bundle |

## `error_class` taxonomy

A stable vocabulary the agent uses to decide next actions. See
[`docs/error_taxonomy.md`](../error_taxonomy.md). Examples:

- `unknown_command` / `parameter_invalid` / `unit_role_missing` (recoverable)
- `safety_violation` (NOT recoverable without human override)
- `partial_failure` / `verify_mismatch` / `timeout` (recoverable)
- `invalid_export_path` / `unsupported_export_format` (recoverable)
- `resume_not_allowed` (not recoverable for this Job; start new one)
