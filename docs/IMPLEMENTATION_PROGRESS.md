# Strict v1 implementation progress

## Purpose

This document records the reasoning and branch-level changes needed for review
and eventual integration through Claude. The branch intentionally supports one
workout format only. It contains no reader aliases, schema migration, inferred
activity, or planned-to-free fallback behavior.

## Source-of-truth decisions

The FIT activity is the authoritative processed result. `FinalizedWorkout` is
the single in-memory model used to construct both FIT messages and the summary.
Every active record must have:

- an explicit ergometer `active_paddling` value;
- an explicit positive `sample_duration`;
- a timestamp that closes the represented sample window;
- active-only distance attribution.

Heart-rate arrival, stale speed, cadence, and distance plateaus cannot extend
the workout. This removes the one-second HR tail and makes duration equal to the
sum of ergometer-active sample windows.

Planned workouts use performed step boundaries only after activity selection.
Each planned lap may show elapsed rest with zero active time, but the complete
lap set must conserve session active time and distance. Malformed, partial, or
missing planned timelines are errors rather than free workouts.

## Implemented changes

1. `app/finalized_workout.py` owns record durations, represented windows,
   active distance, timer runs, lap groups, and conservation checks.
2. `app/summary.py` and `app/export_fit.py` consume the same finalized durations
   for averages, HR zones, records, laps, and session fields.
3. `app/read_csv.py` requires the exact v1 writer header and rejects missing,
   extra, or reordered fields. Row-level crash truncation remains recoverable.
4. `app/workout_metadata.py` requires version 1, workout mode, and every
   processing input. History labels invalid files visibly.
5. Planned-step sidecars are versioned, validated, atomically replaced, and
   refreshed while a step is open so interruption recovery has a current end.
6. Power estimation receives pull length explicitly. Live tiles, planned target
   feedback, summaries, and exports cannot alter one another through global
   state.
7. The standard BLE heart-rate implementation is
   `app/heart_rate_monitor_bluetooth.py::HeartRateMonitor`; bundle references and
   permission text are vendor-neutral.
8. GitHub workflows now use locked dependency installs, job timeouts,
   read-only default permissions, release-only write permission, pre-build
   quality validation, tag ancestry validation, and seven-day build artifacts.
9. Reliability phases 1–4 add typed worker terminal outcomes, shared durable
   atomic JSON control files, strict complete-packet admission, malformed-stream
   health signaling, and centralized configuration validation with invalid-file
   quarantine.

## Removed behavior

- distance/speed inference when the activity signal is absent;
- stationary-tail trimming as a substitute for the activity flag;
- optional or unknown CSV fields;
- the `smooth` data-mode alias;
- current-config substitution for missing workout metadata;
- malformed-step filtering and planned-to-free fallback;
- process-global power calibration;
- Garmin-specific HR module and class names.

## Verification strategy

Tests now construct v1 records with explicit activity and sample duration.
Regression coverage includes inactive HR prefixes/tails, stale non-zero speed,
timestamp gaps, sample windows longer than one second, inactive distance
changes, malformed schemas and steps, planned conservation, decoded FIT to
summary parity, typed worker outcomes, atomic-write failures, strict config
validation, and malformed BLE packets. The completed validation has 131
passing tests across 16 isolated modules, plus clean Ruff, strict mypy,
workflow YAML, and whitespace checks. The required command sequence is documented in
[TESTING.md](TESTING.md).

## Review notes

This is deliberately a breaking first-version cleanup. Review should not add
aliases, migration readers, permissive metadata defaults, or inferred movement.
If the format changes before release, update the v1 writer, reader, fixtures,
and documentation together.
