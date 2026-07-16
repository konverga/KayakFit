# Architecture decisions

This is an ADR-lite record of decisions that shape KayakFit. Each decision
states the current policy and why it exists. Open defects in an implementation
are tracked separately in [ROADMAP.md](ROADMAP.md); a decision being accepted
does not imply that every current code path satisfies it perfectly.

## D001 — CSV is the canonical durable observation log

- **Status:** Accepted
- **Decision:** Stream every valid ergometer response to semicolon CSV and retain
  valid stationary and repeated rows. Do not segment, deduplicate, or discard
  low-speed observations in `CsvReader`.
- **Rationale:** Capture and interpretation have different failure modes. A raw,
  append-oriented timeline can be recovered after a crash and reprocessed when
  segmentation improves. Global payload deduplication previously deleted
  legitimate repeated samples and filtering stationary rows erased pauses.
- **Consequences:** CSV contains inactive periods that do not appear as FIT
  records. Consumers must use finalization rather than raw row count or raw
  first/last timestamps as workout duration.

## D002 — Ergometer activity, not heart rate, owns workout timing

- **Status:** Accepted
- **Decision:** `active_paddling` is required; values greater than zero select
  active records and zero selects inactive records. HR notifications update a
  latest-value cache but never create rows or extend the workout.
- **Rationale:** The ergometer directly signals whether the athlete is paddling.
  HR can begin before paddling and continue through recovery, producing the
  unwanted prefix/tail that motivated the branch.
- **Failure policy:** A missing or invalid activity value makes the workout
  invalid; no other metric substitutes for it.
- **Consequences:** Summary duration, FIT timer events, active averages, and
  chart record domains are anchored to the ergometer signal.

## D003 — FIT and summary share one finalized workout domain

- **Status:** Accepted
- **Decision:** `app.finalized_workout.finalize_workout()` is the sole owner of
  active record indices, exact active runs, presentation/FIT laps, chart breaks,
  total timer time, elapsed time, and distance. `FitExporter` and
  `compute_summary()` consume the same result.
- **Rationale:** Independent FIT, summary, and completion calculations previously
  disagreed on pauses, tails, repeated rows, distance, and averages.
- **Verification:** `tests/test_pipeline.py` decodes a real FIT and compares its
  session/lap fields with `WorkoutSummary`.
- **Verification:** Planned samples are intersected with step intervals and a
  single sample can contribute weighted time, distance, and metrics to adjacent
  laps without duplication in the FIT record stream.

## D004 — Metric aggregation is domain-specific and shared

- **Status:** Accepted
- **Decision:** `app.stats.aggregate_metrics()` defines final workout averages
  and peaks for FIT and summary.
- **Rules:**
  - HR, cadence, and estimated power average only valid positive readings.
  - These averages use represented sample duration when available.
  - Average speed is covered distance divided by active timer time.
  - Maximum speed is the maximum valid positive sample in the recorded
    instant/averaged processing mode.
  - FIT-compatible limits filter values that cannot be encoded truthfully.
- **Rationale:** Generic column means and independently repeated calculations
  created multiple sources of truth.
- **Presentation:** Chart smoothing is rendering-only. Legend aggregates come
  from `WorkoutSummary`, and live field selection follows the recorded
  instant/averaged processing mode.

## D005 — Sensor samples represent preceding time windows

- **Status:** Accepted
- **Decision:** `window_size__s`/`sample_duration` represents the interval ending
  at the row timestamp. The first active record therefore contributes time
  before its timestamp, and FIT START can precede the first emitted record.
- **Rationale:** Treating samples as zero-duration points loses the first second
  and makes duration depend on callback timing rather than the ergometer's
  represented window.
- **Presentation decision:** Summary charts use the first emitted record as
  visual time zero to avoid drawing a false blank tail. Headline timer values
  still include the represented first window.
- **Boundary policy:** Planned laps receive the exact overlap between the sample
  window and each performed step. Time-weighted metrics and distance are split
  by the same overlap fraction.

## D006 — Free and planned workouts share activity selection but not grouping

- **Status:** Accepted
- **Decision:** Activity selection is independent of the plan. After active
  records are selected:
  - free workouts group exact runs into natural laps using autopause thresholds;
  - planned workouts group records by performed warm-up/work/rest/cooldown step.
- **Rationale:** A plan describes user-facing structure, not permission to count
  idle samples. Conversely, movement pauses should not fragment a planned work
  step into meaningless one-second laps.
- **Consequences:** Planned splits show elapsed and active time separately. A
  fully inactive rest remains visible with blank averages. The live free-lap
  indicator is hidden during planned workouts.

## D007 — Per-workout metadata makes historical processing reproducible

- **Status:** Accepted
- **Decision:** Save processing inputs in `metadata.json` and performed planned
  boundaries in `steps.json` inside each workout directory. Export and summary
  use only those recorded settings.
- **Captured inputs:** Pull length, instant/averaged field mode, autopause
  thresholds, HR zone configuration, and other versioned processing inputs.
- **Rationale:** Changing current settings must not retroactively change an old
  workout's power, segmentation, or zones.
- **Failure policy:** Missing, incomplete, malformed, or unsupported metadata is
  rejected as an invalid workout. A planned workout without a complete valid
  performed-step timeline is rejected; it is never reclassified as free.

## D008 — Power is estimated from force and cadence

- **Status:** Accepted
- **Decision:** Use
  `power_W = pull_force_N × pull_length_m × cadence_spm / 60` through
  `app.power.estimate_power()`.
- **Default calibration:** `pull_length_m = 0.623`, constrained to 0.3–1.2 m.
- **Rationale:** The ergometer does not expose a dedicated power sensor field;
  the calibrated model tracks its display closely with stroke-averaged inputs.
- **Consequences:** KayakFit's maximum is a highest one-second estimate, not the
  ergometer display's within-stroke peak. Calibration details live in
  [POWER_MODEL.md](POWER_MODEL.md).
- **Consequences:** Pull length is passed explicitly to live metrics, summary,
  and FIT processing. There is no process-global calibration state.

## D009 — Derived FIT measurements require an explicit source and purpose

- **Status:** Accepted
- **Decision:** Emit real device-derived metrics plus a deterministic synthetic
  GPS track along the 2 km Hazewinkel course. Each position is calculated from
  the same finalized active-only cumulative distance written to its FIT record.
  Emit one metric-free anchor at each active-run START with the cumulative
  distance preceding its first sample window. Do not generate altitude,
  calories, arbitrary coordinates, or other unsupported measurements.
- **Rationale:** Strava recalculates the kayaking overview's moving time from
  record positions and otherwise spans inactive timestamp gaps, even when FIT
  timer and lap summaries are correct. Tying every position to the authoritative
  active distance lets Strava identify pauses without creating an independent
  distance or timing domain.
- **Consequences:** The displayed route is compatibility data, not a claim that
  the indoor ergometer captured real GPS. Inactive odometer changes and paused
  rows cannot advance the route. The GPS span from an anchor to its run's last
  sensor record equals that run's authoritative timer time.

## D010 — Reliability state fails closed

- **Status:** Accepted
- **Decision:** Background operations produce one typed terminal result. CSV
  metadata, performed steps, and recovery state use durable atomic JSON writes.
  A missing required sidecar is partial output, never successful output.
- **Decision:** Only complete v1 ergometer packets reach recording callbacks.
  Configuration is validated once through the shared GUI/worker validator;
  invalid explicit values are not silently repaired.
- **Consequences:** Automatic FIT generation requires a successful recording
  result. Partial CSV data remains recoverable and visible to the user.
- **Consequences:** The ergometer can be configured to show its own energy field,
  but KayakFit does not copy or calculate calories for summary/FIT.

## D011 — FIT replacement is atomic

- **Status:** Accepted
- **Decision:** Build to a sibling `.fit.tmp` and replace the destination only
  after `fit_tool` completes successfully.
- **Rationale:** Re-exporting an old workout must not destroy a valid FIT if the
  new conversion fails midway.
- **Verification:** Regression tests assert preservation of an existing FIT on
  failed export.

## D012 — Thread boundaries are explicit

- **Status:** Accepted
- **Decision:** BLE/session work and export/upload work run in background worker
  threads. Workers send structured events through `WorkerManager`, which
  schedules all Tk mutations on the GUI thread.
- **Rationale:** BLE, filesystem, FIT, OAuth, and HTTP operations can block. Tk
  is not thread-safe.
- **Consequences:** Stop and manual step advance use thread-safe events. Shutdown
  is bounded. Event dictionaries are currently loose and should become typed
  records over time.

## D013 — Strava upload acceptance is not completion

- **Status:** Accepted, transaction persistence incomplete
- **Decision:** Treat a timed-out processing poll as `pending`, not as a
  confirmed activity and not as a rejection. Refresh tokens proactively and
  persist rotated credentials even when upload later fails.
- **Security:** OAuth uses a loopback-only callback, per-flow cryptographic state,
  constant-time state comparison, and OS credential-vault storage when
  available.
- **Known qualification:** Pending upload IDs are not persisted, so the app
  cannot yet resume the transaction after restart.

## D014 — Use a small stdlib Table instead of Polars

- **Status:** Accepted
- **Decision:** `app.table.Table` provides the limited column/row operations the
  pipeline needs.
- **Rationale:** Removing a dataframe dependency reduces packaged application
  size and startup complexity.
- **Consequences:** Add focused operations to `Table` only when they are used by
  the domain; do not recreate a general dataframe API.

## D015 — Defensive parsing has bounded graceful degradation

- **Status:** Accepted
- **Decision:** Malformed independent units are skipped with a precise warning
  where truthful output remains possible; hard failure occurs when too little
  usable input remains.
- **Examples:**
  - one malformed program file does not hide other programs;
  - one malformed CSV row can be skipped, but predominantly corrupt input fails;
  - malformed planned-workout timelines fail closed because silently switching
    lap semantics would produce a different workout;
  - program repeat depth, operations, and expanded steps are capped.
- **Boundary:** Persistence failures and failures that would falsely claim a
  successful workout are not candidates for silent degradation.

## D016 — Strava authorization is one atomic credential-vault record

- **Status:** Accepted
- **Decision:** Store client ID, client secret, access token, refresh token, and
  token expiry together in one OS credential-vault entry. All five fields are
  required before any UI or upload path reports Strava as connected.
- **Rationale:** The credential vault survives deletion of `~/KayakFit` while
  `config.yml` does not. Splitting refresh-critical state across both stores can
  restore tokens without their client ID or expiry and falsely show Connected.
- **Consequences:** A complete connection is restored after the settings folder
  is recreated. Disconnect removes the vault entry. Incomplete pre-release
  entries are deleted rather than migrated.

## Branch history and handoff

The `feature/fit-summary-source-of-truth` branch was created from `main` at
`c744d05`. Its focused commits introduced:

- a shared finalized workout model;
- shared FIT-compatible metric aggregation;
- recorded processing metadata;
- authoritative active-paddling timing and removal of HR-only tails;
- planned-step versus free-workout grouping;
- summary chart segmentation refinements;
- decoded-FIT regression tests and atomic replacement safeguards;
- fail-closed CSV persistence and collision-safe recording paths;
- exact overshoot-preserving planned boundaries and sample-window splits;
- active-only FIT record distance and authoritative chart/live metric policy.

Before merging the branch, Claude should review `main...HEAD`, the current
worktree, this decision record, and the completion evidence plus remaining P1/P2
items in [ROADMAP.md](ROADMAP.md). The critical phases were implemented as one
coherent domain change; review their invariants together rather than treating
FIT, summary, planned timing, and persistence as independent patches.
