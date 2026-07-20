# P1 bulk-acquisition artifacts — runtime notes

## Changed

- Added strict parsing and validation for the `artifact: v1` JSON reference.
- Added `system_config.artifacts.root` and `embed_max_bytes` (32 MiB default).
- Extended bundle construction to inventory every valid reference, embed small
  verified files, and report missing, unreadable, unconfigured, or mismatched
  files without failing export.
- Embedded artifacts are checksummed normally and also appended to
  `manifest.contents` using the existing `ContentEntry` fields.
- Documented `supports_bulk_acquisition` in the capability design memo.

## Notes and uncertainty

- Backend capabilities are explicitly documented as not implemented, so no
  runtime capability API or validation system was invented for this change.
- Existing bundle `manifest.contents` values are file-name strings, while the
  richer `ContentEntry` model belongs to experiment assets. Existing entries
  remain unchanged; embedded artifacts alone add `{path, sha256, kind}` records
  to keep this change additive.
- Malformed JSON or JSON without an `artifact` key remains an ordinary scalar.
  JSON with an `artifact` key is fail-closed: invalid references are rejected
  and never cause filesystem access.

## Verification

- New and closely related tests: `45 passed`.
- Full suite: `2079 passed, 28 skipped`; one unrelated UI Git-commit test
  failed because this sandbox denies writes to temporary `.git` metadata. The
  same failure reproduces alone.
- Ruff passes for every changed Python file. The repository-wide Ruff command
  still reports 278 pre-existing findings in untouched files.
