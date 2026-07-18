# Cross-instrument routing implementation plan (SP-8)

This document translates `cross_instrument_routing_spec.html` into the concrete
implementation sequence for lab-executor-mcp. It intentionally reuses the
existing `SessionFacade.get_session` / polling resolver pattern.

## Invariants

- Missing or unknown targets fail closed. They never fall back to the primary
  session.
- Commands, parameter ranges, safety checks, history, and response parsing use
  the resolved target session and its definition as one indivisible unit.
- Recipes without `instrument` retain their current behavior.
- MCP tool count remains 50; legacy expression evaluators remain unchanged.
- Safe shutdown is attempted independently for every resource written by the
  sequence.

## SP-8.1: immediate misrouting prevention

1. Export `SessionResolver` from `session.py` and provide a private
   primary-only resolver in `seq_runtime.py`.
2. Resolve `CommandStep.instrument` at the start of `process_command_step`.
3. Return `InstrumentNotAvailable` for an unavailable non-primary target and
   `NoDefinitionFound` for a resolved target without a definition.
4. Execute the command with the resolved target session and include its
   resource in the result.
5. Add regressions proving that top-level and nested/call cross-instrument
   commands perform no VISA I/O without an explicit resolver, while omitted or
   primary instruments remain compatible.
6. Run the complete suite and commit this stage independently.

## SP-8.2: actual routing

1. Thread `SessionResolver` through `execute_recipe`, `execute_plan`, nested
   executors, the recipe Job path, and the existing recipe MCP tool.
2. Extend plan conversion with target-definition lookup so deferred ranges and
   SP-7 role capabilities are checked against the bound target definition.
   Unknown targets and missing definitions fail during validation where a
   resolver is available; runtime validation remains the final gate.
3. Record successful writes by resolved resource. Safe shutdown iterates every
   written resource independently and aggregates all results without stopping
   after one failure. Apply this to synchronous and recipe Job execution.
4. Ensure command results/events and dry-run rows expose the effective target
   resource.
5. Remove the temporary `CrossInstrumentCallUnsupported` compile/runtime gates.
6. Add routing, target-definition, range, capability, shutdown isolation,
   timeline/dry-run, and closed-loop E2E tests.
7. Update `CHANGELOG.md` under `Unreleased`, run the full suite and invariant
   checks, then commit SP-8.2 separately.

## Validation checklist

- `python -m pytest` after SP-8.1 and again after SP-8.2.
- `tests/test_separation_boundary.py` confirms 50 MCP tools.
- `git diff main -- src/lab_executor/utils/expression.py
  src/lab_executor/utils/condition.py` is empty.
- `git diff --check` is clean and `git ls-files --eol` reports no index CRLF.
- Grep the body of `process_command_step` to verify no command execution,
  definition, history, or reporting path uses the unresolved primary session.
