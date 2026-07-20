# P2 monitor stop action notes

## Changes

- Added `on_stop_condition` to `JobManager.start_monitor_job` and the
  `start_monitor` MCP tool. `record_only` remains the default and preserves the
  previous behavior; `safe_shutdown` invokes the existing structured safe
  shutdown sequence after a stop-condition breach.
- Added start-time validation. `safe_shutdown` is refused when the instrument
  has neither a declared `safe_shutdown` sequence nor a category eligible for
  the existing fallback.
- Added fail-closed handling. The full safe-shutdown result is stored in the
  `monitor_stop_condition_met` event. A false `attempted`/`success` value, or an
  exception from the helper, fails the monitor job with `error_class=hardware`.
- `cancel_job` and unknown values are validation errors. A monitor job is
  separate from an experiment job, and there is no defined way to identify
  which experiment job should be cancelled; no relationship was invented.

## Safety and latency

This feature is supervisory monitoring, not a safety instrumented system.
`interval_s` has a 1.0-second floor, and threshold detection can be delayed by
at least a polling interval plus command/transport latency. Protection against
immediate danger belongs in a hardware interlock.

## Uncertainties

None. Start-time validation checks that a shutdown path exists but deliberately
does not execute it; actual command or transport failures remain possible and
are reported through the fail-closed breach path.
