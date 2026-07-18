# Backend expansion foundation implementation plan (BEF)

## Invariants

- `InstrumentBackend` remains the four-member protocol defined in
  `backends/base.py`; routing prefixes are registration metadata.
- Unknown resources never fall back to another backend.
- A single selected backend follows the existing construction path without a
  `CompositeBackend` wrapper.
- MCP tool count stays 50 and the legacy expression evaluators remain unchanged.

## BEF-1: freeze and verify the contract

1. Publish `docs/backend_contract.md` as the semver-stable integration surface.
2. Add an async conformance assertion that verifies runtime protocol shape,
   signatures, awaited result types, and optional idempotent `close()`.
3. Verify both the bundled `MockBackend` and the conformance kit's ability to
   reject a deliberately broken implementation.
4. Run the complete suite and commit BEF-1 independently.

## BEF-2: discover and compose backends

1. Add `BackendRegistration` and entry-point discovery for
   `lab_executor.backends`, including the bundled mock factory.
2. Add a strict longest-prefix `CompositeBackend`, collision checks,
   fail-closed routing, resource aggregation, and best-effort close propagation.
3. Add declarative CLI and `_system.yaml` selection while preserving the exact
   legacy single-backend path.
4. Test routing, no-I/O failures, collisions, degradation, discovery,
   conformance, server composition, and single-backend identity.
5. Update `CHANGELOG.md`, run the full suite and invariant checks, and commit
   BEF-2 independently.
