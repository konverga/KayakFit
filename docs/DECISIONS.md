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
- **Decision:** `active_paddling` is required; `1` selects active represented
  windows and `0` selects inactive windows. HR notifications update a
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
- **Decision:** `app.workout_metrics.compute_finalized_metrics()` produces one
  session/lap aggregate set consumed verbatim by FIT and summary;
  `app.stats.aggregate_metrics()` defines each slice's arithmetic.
- **Rules:**
  - HR, instantaneous cadence, and instantaneous-input estimated power average
    only valid positive readings.
  - These averages use canonical represented sample duration.
  - Average speed is the represented-time-weighted mean of valid active
    `speed_instant` readings (see D017).
  - Maximum speed is the largest valid active instantaneous-speed reading.
  - Every reported average/maximum pair satisfies `average <= maximum` before
    FIT encoding or UI rounding.
  - FIT speed records use that same unsmoothed recorded series.
  - Integer FIT/summary averages use nearest-integer rounding, not truncation.
  - FIT-compatible limits filter values that cannot be encoded truthfully.
- **Rationale:** Generic column means and independently repeated calculations
  created multiple sources of truth.
- **Presentation:** Legend aggregates come from `WorkoutSummary`; live active
  distance/time and speed use the same accumulator as finalization. Summary
  splits show finalized distance and millisecond timing without redistributing
  rounded display units.

## D005 — Capture persists the canonical processing timeline

- **Status:** Accepted
- **Decision:** Capture writes `session_elapsed__s` and `sample_duration__s` for
  every row. The row represents
  `[session_elapsed - sample_duration, session_elapsed]`; `timestamp` is that
  endpoint on one epoch origin. Raw device elapsed/window/timestamp fields are
  diagnostics and never processing fallbacks.
- **Capture rule:** Advancing raw elapsed moves the canonical endpoint by its
  delta. A stall/reset rebases to monotonic receipt time. Effective duration is
  clipped to avoid overlap, leaving all uncovered time as pause time. A first
  elapsed value of zero is a valid zero-duration observation.
- **Polling rule:** Poll commands follow fixed one-second monotonic deadlines.
  Command-write time is included in the period, and a late command resumes from
  a new deadline instead of causing catch-up bursts. This avoids systematic
  drift while leaving delayed device responses safe.
- **Rationale:** The raw counter can stall or reset after reconnect. Persisting
  the normalized result once makes final processing strict, simple, and fully
  reproducible without recreating capture-time state.
- **Presentation decision:** Summary chart bounds span elapsed zero through the
  final canonical session endpoint, including inactive prefixes and tails. Active
  points are plotted at the start of their represented intervals.
- **Boundary policy:** Planned laps receive the exact overlap between the sample
  window and each performed step. Time-weighted metrics and distance are split
  by the same overlap fraction.
- **Validation:** `ProgramRunner` re-checks the same monotonic, non-overlapping
  invariant in integer milliseconds, matching capture and finalization. The
  canonical fields are millisecond-derived, so comparing float seconds directly
  once rejected an exactly-filled window (`2.251 - 2.0 == 0.2509999999999999`,
  just under a legitimate `0.251` s window) and crashed the live BLE callback.
  Rounding both sides to milliseconds removes that false positive while still
  rejecting a genuine overlap.

## D006 — Free and planned workouts share activity selection but not grouping

- **Status:** Accepted
- **Decision:** Activity selection is independent of the plan. After active
  records are selected:
  - free workouts group exact active runs across gaps shorter than the fixed
    five-second meaningful-pause delay;
  - planned workouts group records by performed warm-up/work/rest/cooldown step.
- **Rationale:** A plan describes user-facing structure, not permission to count
  idle samples. At low cadence the ergometer can briefly clear its activity flag
  between strokes, which must not create one-second free laps. Conversely,
  movement pauses should not fragment a planned work step into meaningless laps.
- **Consequences:** Planned splits show elapsed and active time separately. A
  fully inactive rest remains visible with blank averages. Free-lap elapsed time
  may include short inactive gaps, while active time and averages remain exact.
  The live free-lap indicator is hidden during planned workouts.

## D007 — Per-workout metadata makes historical processing reproducible

- **Status:** Accepted
- **Decision:** Save processing inputs in `metadata.json` and performed planned
  boundaries in `steps.json` inside each workout directory. Export and summary
  use only those recorded settings.
- **Captured inputs:** Pull length and HR zone configuration. Metric channels
  are fixed by the v1 processing contract, not stored as user preferences.
  Activity timing has no user-configurable threshold or gap.
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
- **Default calibration:** `pull_length_m = 0.600`, constrained to 0.3–1.2 m.
- **Rationale:** The ergometer does not expose a dedicated power sensor field.
  Using instantaneous force and cadence keeps every surface tied to the same
  recorded points; 0.600 slightly lowers estimates from the former 0.623
  calibration.
- **Consequences:** KayakFit's maximum is a highest one-second estimate, not the
  ergometer display's within-stroke peak. Calibration details live in
  [POWER_MODEL.md](POWER_MODEL.md).
- **Consequences:** Pull length is passed explicitly to live metrics, summary,
  and FIT processing. There is no process-global calibration state. Historical
  workout metadata remains unchanged.

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

## D017 — Recorded instantaneous speed is the canonical speed channel

- **Status:** Accepted
- **Decision:** Raw chart points, time-weighted lap/session averages, maxima, and
  FIT record speed use valid active `speed_instant` readings. Cumulative
  odometer distance is a separate recorded channel and is never capped or
  rewritten using speed.
- **Rationale:** Odometer changes around pauses can make
  `distance / active time` disagree with the recorded instantaneous-speed
  series. Keeping the two observations independent is reproducible and avoids
  inventing corrections that are absent from the sensor data.
- **Consequences:**
  - `lap distance / lap active time` can differ from reported average speed.
  - A valid time-weighted average cannot exceed its raw maximum; constant speed
    legitimately produces equality rather than a strictly lower average.
  - If the recorded speed channel has no valid value, average and maximum stay
    unavailable instead of falling back to odometer-derived speed.
  - Missing odometer readings still bridge over their pending active intervals,
    independently from speed.

## D018 — Metric channels are fixed by metric

- **Status:** Accepted
- **Decision:** There is no global metric-source setting. Summary and FIT use
  instantaneous protocol points for speed and cadence, and the summary uses
  instantaneous pull force. The main-window cards, their average/max previews,
  and planned target feedback consistently use instantaneous speed, pace,
  cadence, and pull force without an averaged-field fallback. Live and
  finalized power both use instantaneous force and cadence with the recorded
  pull-length calibration. Heart rate and distance each have one source field.
- **Rationale:** Workout review and export retain the recorded instantaneous
  sensor points, and physical testing showed that averaged protocol fields make
  the live cards read lower than the ergometer display. A single instantaneous
  live policy keeps cards, previews, and target feedback internally consistent.
- **Consequences:** Main-window sensor cards and their secondary average/max use
  instantaneous device fields. Summary graphs, aggregates, and FIT records use
  the same instantaneous sensor channels. Changing app settings cannot
  reinterpret a workout's metric channels.

## D019 — Chart-line breaks use the meaningful-pause delay in every workout mode

- **Status:** Accepted
- **Decision:** The summary chart restarts its line only at meaningful-pause
  group boundaries (`app.finalized_workout._chart_breaks` over
  `_group_free_segments`), for planned and free workouts alike. Exact active runs
  less than the fixed five-second delay apart are drawn as one continuous line.
- **Rationale:** Planned charts previously broke the line at every exact activity
  run. At high cadence the ergometer can report a one-second window across a
  two-second elapsed advance, leaving a one-second uncovered gap that closes an
  exact run even though paddling never stopped. Breaking the line at each such
  gap fragmented a continuous work interval into disconnected pieces. Reusing the
  meaningful-pause grouping already applied to free workouts keeps the line
  faithful to the effort.
- **Consequences:** Only chart-line continuity changes. Exact runs, FIT timer
  events, active time, distance, and planned/free laps are unchanged; the
  uncovered second still counts as pause, not active time. Planned step
  boundaries continue to render as chart bands, not line breaks. A real pause of
  five seconds or more still starts a new line.

## D020 — Summary chart lines begin at each run's start boundary

- **Status:** Accepted
- **Decision:** On both the time and distance x-axes, the summary chart starts
  each active run's line at the run's start boundary: the FIT timer START (the
  window start time) and the cumulative active distance preceding the run's
  first sample window. `app.summary` exposes these as `series_anchor_t` and
  `series_anchor_distance`; `gui.summary_window` prepends the anchor at each line
  break using the first record's value.
- **Rationale:** Sensor records are plotted at the end of their sample window, so
  without an anchor each run's line began one window in. In distance mode this
  understated every segment's span versus the lap table and drew a resume
  odometer jump — credited to the first active record after a pause — as empty
  space between segments. Anchoring each run at its start makes the drawn span
  equal the lap distance and active time and mirrors the metric-free FIT
  run-start anchor already emitted for Strava (D009).
- **Consequences:** Presentation only. The anchor is a drawn lead-in, not a
  record: the `series_*` value arrays, hover snapping, statistics, laps, totals,
  and FIT output are unchanged. Where a distance reading is missing the anchor is
  omitted and the line falls back to starting at its first point.
