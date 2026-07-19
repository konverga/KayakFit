# Roadmap and audit findings

## Current execution status — 2026-07-18

The strict first-version critical phase is implemented on the feature branch:

- v1 CSV, metadata, activity, sample-duration, and planned-step contracts are
  explicit and reject incompatible inputs;
- summary and FIT share one finalized record/timer/distance model;
- planned laps enforce timer and distance conservation;
- power calibration is explicit rather than process-global;
- the generalized BLE heart-rate implementation no longer uses Garmin-specific
  module or class names;
- release-audit fixes enforce FIT duration units and the required Activity
  message, conserve the terminal planned-step tail, preserve recorded resume
  distance jumps, and delete read-only recordings correctly on Windows;
- CI and release builds use locked installs, timeouts, least privilege,
  pre-build validation, and bounded artifact retention.

Remaining roadmap items below are follow-up improvements unless individually
marked completed. Detailed implementation reasoning and verification live in
[IMPLEMENTATION_PROGRESS.md](IMPLEMENTATION_PROGRESS.md).

This is the prioritized implementation backlog derived from the July 2026
whole-codebase review. Completed critical items remain annotated here during the
feature-branch handoff so reviewers can connect findings to implementation and
regression evidence. New work should keep the status and log current.

Priority meanings:

- **P0 / Critical** — invalid workout output, silent data loss, or destructive race.
- **P1 / Recommended** — material correctness, reliability, or maintainability risk.
- **P2 / Optional** — usability, performance, or architectural improvement.

## Implementation tracking

Critical phases 1–3 started on `feature/fit-summary-source-of-truth` on
2026-07-13. Keep this table current while implementation is in progress.

| Phase | Scope | Status | Verification |
|---|---|---|---|
| 1 | CSV failure propagation, export/discard ownership, unique filenames | Completed | Full suite, Ruff and strict mypy pass |
| 2 | Planned sample-window attribution and overshoot-preserving boundaries | Completed | Full suite, Ruff and strict mypy pass |
| 3 | Active distance and authoritative chart/live metric policy | Completed | Full suite, Ruff and strict mypy pass |
| 4 | FIT conformance and terminal/Windows release hardening | Completed | 156 tests, Ruff and strict mypy pass |
| 5 | FIT/summary metric coherence and peak-speed invariants | Completed | 181 tests, historical FIT audit, Ruff and strict mypy pass |
| 6 | Device-elapsed timeline and recorded-point reproducibility | Completed | 195 runnable tests, supplied-workout decoded FIT parity, compile and whitespace checks |
| 7 | Canonical capture timeline and removal of unreleased compatibility paths | Completed | Canonical reset/gap tests, supplied-workout parity, 203 runnable tests |

Implementation log:

- 2026-07-18 — Replaced finalization-time reconstruction with capture-time
  `session_elapsed__s` and `sample_duration__s`. Counter stalls/resets now
  rebase once against monotonic receipt time, uncovered intervals explicitly
  break active runs, planned sidecars use only canonical elapsed bounds, and
  unused autopause compatibility settings and epoch step fields were deleted.

- 2026-07-18 — Initially made device `elapsed_time__s` the record/session clock,
  made every exact activity run a free lap, persisted elapsed planned-step
  bounds, and removed threshold/gap overrides. Summary, FIT, and graph speed
  share time-weighted active `speed_instant` values while raw odometer
  deltas independently own distance. The supplied workout decodes to 62 s
  elapsed, 39 s active, 117 m, three matching laps, and 10.22 km/h maximum.

- 2026-07-18 — A temporary distance-derived speed policy was superseded after
  the supplied recording proved its odometer distance/active-time ratios exceed
  its recorded maximum. Finalized processing follows the reproducible
  instantaneous-speed policy in D017.

- 2026-07-17 — Phase 5 briefly made instantaneous speed authoritative for FIT
  records and peaks. A temporary distance-derived policy followed, then the
  recorded-point audit restored instantaneous speed for chart/FIT averages and
  peaks. The lasting work shared one finalized metric result between FIT and
  summary, retained rows with malformed unused CSV fields, and conserved
  displayed split totals.

- 2026-07-13 — Started critical phases from the roadmap; baseline branch was
  clean at `cbc50a7`.
- 2026-07-13 — Phase 1 made CSV writes fail closed, retained partial recovery
  files with explicit UI state, prevented export/discard races, deleted owned
  artifacts in canonical-last order, and reserved collision-safe filenames.
- 2026-07-13 — Phase 2 introduced exact time/distance boundary interpolation,
  preserved every transition crossed by delayed packets, counted the first
  represented window, and split sample time/metrics/distance at planned-step
  boundaries.
- 2026-07-13 — Phase 3 rebased emitted FIT record distance onto active-only
  deltas, made FIT/session/lap/chart totals share that domain, supplied chart
  legends from authoritative aggregates, and centralized live field selection.
- 2026-07-13 — Validation completed: all 118 tests passed in 14 isolated pytest
  processes; `ruff check .`, `mypy . --strict` (64 source files), and
  `git diff --check` passed.
- 2026-07-13 — Replaced repeated planned-phase fills in every summary metric
  panel with one compact time-axis strip and shared boundary guides. Short
  phases stay visible without label collisions; distance mode remains
  deliberately unsegmented. Validation passed with 125 tests in 15 isolated
  processes, Ruff, strict mypy (65 source files), and `git diff --check`.
- 2026-07-13 — Stabilized workout-mode badge layout with a fixed title grid,
  revealed the history window only after its first screenful was laid out, and
  moved older-row construction into bounded batches. Validation remained at
  125 tests in 15 isolated processes with Ruff, strict mypy, and diff checks.
- 2026-07-15 — The release audit corrected session/lap duration encoding, added
  the required FIT Activity summary, made a plan's terminal step own the full
  captured activity tail, capped resume jumps without valid speed, and made
  discard retry read-only files on Windows. Validation passed with 156 tests,
  Ruff, strict mypy, lock consistency, and diff checks. The 2026-07-18
  recorded-point contract later removed that resume cap.

## Critical fixes

### P0 — Attribute sensor windows across planned-step boundaries

**Status: Completed 2026-07-13.** Covered by planned-window conservation and
decoded-FIT regression tests.

- **Location:** `app/finalized_workout.py::finalize_workout`
- **Root cause:** A record's entire `sample_duration` is assigned to whichever
  planned step contains the record timestamp. The duration actually represents
  the interval preceding that timestamp and can cross a step boundary.
- **Impact:** A FIT lap can contain `total_timer_time > total_elapsed_time`.
  Active time, HR, cadence, power, zones, and averages can be credited to the
  wrong step. FIT and summary agree, but agree on an invalid result.
- **Recommendation:** Represent each sample as
  `[timestamp - sample_duration, timestamp]`, intersect it with performed step
  intervals, and use overlap duration as the step-specific weight. Enforce
  `0 <= timer_time <= elapsed_time` per lap and conservation of session active
  time.

### P0 — Propagate CSV persistence failures

**Status: Completed 2026-07-13.** A typed terminal error stops recording,
retains durable rows for recovery, and suppresses automatic export/upload.

- **Location:** `app/write_csv.py::CsvWriter.write_record`,
  `app/workout_session.py::_on_ergometer_data`
- **Root cause:** The writer catches all exceptions and returns no status; the
  session increments its point count anyway.
- **Impact:** Disk-full, permission, device-removal, or other I/O failures can
  silently lose the workout while the UI reports that samples were saved.
- **Recommendation:** Give the writer an explicit healthy/failed state and
  propagate a typed persistence failure. Stop safely, retain recovery state,
  report the last durable row, and block false success/export messaging.

### P0 — Make discard and export mutually exclusive

**Status: Completed 2026-07-13.** Worker ownership disables discard and all
derived artifacts are removed before the canonical CSV.

- **Location:** `gui/recording_lifecycle.py::_discard_workout`,
  `gui/export_upload.py::_set_export_buttons`
- **Root cause:** Starting an export disables save/upload/history but leaves the
  discard action active. Discard could leave generated files behind.
- **Impact:** Export can read a workout while the user deletes it, and “delete
  permanently” may leave a generated FIT activity on disk.
- **Recommendation:** Disable destructive actions while a worker owns the
  workout, coordinate completion/cancellation, and delete every owned artifact
  after confirmation.

### P0 — Keep chart statistics authoritative

**Status: Completed 2026-07-13; raw charts adopted 2026-07-19.** Legends and
average lines consume `WorkoutSummary` aggregates, and charts now show raw
recorded points.

- **Location:** `gui/summary_window.py::_ChartSeries`
- **Root cause:** The chart legend previously calculated an unweighted mean and
  maximum from rendered points. It bypassed `WorkoutSummary` and
  `aggregate_metrics()`.
- **Impact:** Display processing lowered peaks; irregular sample windows changed
  displayed averages; speed no longer uses distance divided by active time.
  The summary window can therefore contradict its own headline and the FIT.
- **Resolution:** Pass authoritative aggregate values into chart series and show
  raw source points rather than deriving reported statistics from display data.

### P0 — Preserve planned-workout overshoot and exact boundaries

**Status: Completed 2026-07-13.** The runner returns every interpolated
transition crossed by a sample and the session persists each boundary.

- **Location:** `app/program_runner.py::ProgramRunner.update`,
  `app/workout_session.py::_record_step_boundary`
- **Root cause:** At most one step advances per sample, the next step baseline
  is reset to the current sample, and the sample arrival timestamp is recorded
  as the boundary.
- **Impact:** Delayed packets or large distance jumps make later steps too long
  and shift cues, summary bands, step laps, and auto-stop timing.
- **Recommendation:** Carry overshoot through a bounded transition loop and
  return every crossed transition. Calculate exact time boundaries and
  interpolate distance boundaries between adjacent samples.

## Recommended improvements

### P1 — Define active distance consistently

**Status: Completed 2026-07-18.** Active deltas drive FIT records, session/lap
distance totals, and the chart distance axis. Speed stays an independent
recorded channel as specified by D017.

- **Location:** `app.active_distance.ActiveDistanceAccumulator`,
  `app.finalized_workout.py::finalize_workout`
- **Root cause:** The distance numerator can include cumulative advances between
  active records while the denominator contains only active sample time.
- **Impact:** Coasting or firmware behavior during inactive packets can inflate
  average speed and planned-step distance.
- **Recommendation:** Attribute distance deltas over the same sample intervals
  and step overlaps used for active time. Document whether coasting is activity
  distance or elapsed-only distance.

### P1 — Use structured worker results

**Status: Completed 2026-07-14.** Recording and export workers now return one
typed terminal outcome. The GUI distinguishes success, cancellation, partial
durability, failure, and pending upload instead of treating thread exit as
success.

- **Location:** `workout_worker.py`, `export_worker.py`,
  `gui/worker_manager.py`
- **Root cause:** Several failure paths only log and return; the manager can
  subsequently print a normal “Finished” line and emit the ordinary completion
  event.
- **Impact:** Configuration, BLE, recording, conversion, and upload failures are
  difficult for the UI and user to distinguish from success.
- **Recommendation:** Return or emit a typed result with outcome, failed stage,
  user-facing message, durable artifacts, and retryability.

### P1 — Make metadata and recovery failures durable and visible

**Status: Completed 2026-07-14.** Metadata, performed steps, and recovery state
use shared atomic JSON replacement with file and directory sync. Failures stop
automatic export and produce a visible partial terminal outcome.

- **Location:** `app/workout_session.py::_write_program_steps`,
  `app/workout_metadata.py`, `app/recovery.py`
- **Root cause:** Sidecar failures are log-only and the active-workout marker is
  neither atomic nor fsynced.
- **Impact:** A planned workout can later be interpreted as a free workout, or
  an interrupted workout may not be offered for recovery.
- **Recommendation:** Use the same atomic replace/fsync policy for recovery
  state and surface a “saved with incomplete metadata” outcome.

### P1 — Centralize configuration validation

**Status: Completed 2026-07-14.** GUI loading/saving and both workers use one
range-checked validator. Invalid explicit values are rejected; malformed files
are preserved under a diagnostic name and surfaced in the GUI.

- **Location:** `gui/config_window.py::_submit`,
  `gui/config_manager.py::ConfigManager`
- **Root cause:** Validation is split between settings, worker startup, numeric
  coercion, and individual consumers. Some `int()` calls are unguarded, weights
  lack range checks, and hand-edited numeric settings are not consistently
  bounded. Corrupt YAML silently becomes defaults.
- **Impact:** Tk callback exceptions, nonsensical timeouts, inconsistent error
  timing, and accidental overwriting of a damaged config.
- **Recommendation:** Introduce one typed, validated configuration model used by
  the GUI and workers. Preserve/rename corrupt input and display a warning.

### P1 — Use a monotonic clock for HR freshness

**Status: Completed 2026-07-14.** HR freshness now compares monotonic receipt
and packet-processing times; epoch time is used only for recorded timestamps.

- **Location:** `app/workout_session.py::_on_heart_rate_data`,
  `_on_ergometer_data`
- **Root cause:** HR receipt time uses the system epoch clock while ergometer
  timestamps advance from a monotonic anchor.
- **Impact:** A system clock adjustment can make HR prematurely stale or keep a
  frozen reading fresh.
- **Recommendation:** Store a monotonic HR-reception time for freshness and use
  epoch timestamps only as recorded output.

### P1 — Base inactivity stop on the activity signal

**Status: Completed 2026-07-14.** Inactivity now resets from the required
`active_paddling` signal and is immune to cumulative-distance resets.

- **Location:** `app/workout_session.py::_on_ergometer_data`
- **Root cause:** Movement is detected by exceeding the maximum cumulative
  distance seen in the session.
- **Impact:** If distance resets after reconnect, genuine paddling may remain
  below the old maximum and the session can auto-stop.
- **Recommendation:** Use `active_paddling` exclusively for inactivity state.

### P1 — Persist pending Strava transactions

- **Location:** `app/strava_uploader.py::upload_file`, recovery/UI handling
- **Root cause:** The upload ID is discarded when Strava processing times out or
  application shutdown interrupts polling.
- **Impact:** Polling cannot resume after restart and recovery can initiate a new
  upload. Stable `external_id` helps Strava reject duplicates but is not a
  complete transaction record.
- **Recommendation:** Persist upload ID, source FIT, external ID, token/account
  context, and acceptance time. Resume GET polling without repeating POST.

### P1 — Make device tests protocol-aware

- **Location:** `gui/device_scanner.py::test_connection`
- **Root cause:** The settings test only connects and disconnects from a BLE
  address.
- **Impact:** A wrong or incompatible peripheral can pass and fail only when a
  workout begins.
- **Recommendation:** Verify the standard HR service/characteristic and perform
  a safe ergometer characteristic/capability check without resetting a workout.

### P1 — Tighten planned-workout schema validation

- **Location:** `app/program.py::_make_step`, `_flatten`, `list_programs`
- **Root cause:** Unknown duration kinds become open steps, booleans and
  fractional values can be coerced to repeat counts, and duplicate display names
  overwrite earlier programs in the GUI mapping.
- **Impact:** A typo silently changes workout behavior or hides a program.
- **Recommendation:** Validate against an explicit schema, reject unsupported
  duration kinds and non-integral repeats, and identify programs by file ID while
  treating names as labels.

### P1 — Make live and final metric policies explicit

**Status: Completed 2026-07-18.** Main-window cards and planned targets use
averaged device fields consistently. Summary, graph, and FIT processing use
instantaneous sensor points except for calibrated averaged-input power (D018).

- **Location:** `gui/metrics_format.py::compute_power`,
  `gui/recording_lifecycle.py::_raw_metric`
- **Root cause:** A global source mode could not represent the different source
  requirements of peak-preserving speed and calibrated power.
- **Impact:** A card could display one channel while its secondary statistics
  accumulated another.
- **Resolution:** Each live card and target uses averaged fields; each card's
  secondary statistics use its displayed field. Finalized consumers use instant
  points for sensor metrics and averaged cadence/force only for power.

### P1 — Reserve unique recording filenames atomically

**Status: Completed 2026-07-13.** Exclusive creation plus a bounded collision
suffix prevents truncation.

- **Location:** `app/workout_session.py::_setup_csv_writer`
- **Root cause:** Filenames have one-second resolution and the writer uses
  truncating mode.
- **Impact:** Two starts in the same second can overwrite an existing workout.
- **Recommendation:** Reserve a unique path with exclusive creation, adding
  subsecond precision or a collision counter.

### P1 — Validate complete ergometer records before saving

**Status: Completed 2026-07-14.** The parser requires the exact v1 field count,
activity value, positive sample duration, and valid elapsed time/distance before
dispatch. Sustained malformed packets emit a device-health event and feed the
existing data-stall policy rather than becoming CSV rows.

- **Location:** `app/kayakfirst_ergometer_bluetooth.py` packet parser
- **Root cause:** A line with only a small subset of fields can still produce a
  partial dictionary and count as a data point.
- **Impact:** Malformed packets create rows with missing load-bearing metrics and
  misleading point counts.
- **Recommendation:** Define a minimum protocol schema and reject incomplete
  workout packets while retaining forward-compatible extra fields.

## Optional enhancements

### P2 — Compute summaries outside the Tk thread and downsample charts

**Status: Completed 2026-07-14.** Summary loading and computation run on a
background thread behind a loading window. Canvas series use cached,
pixel-width min/max downsampling that retains gaps and lap boundaries, and
resize redraws are debounced. Full-resolution data still owns statistics,
hover values, and FIT output.

- **Location:** `gui/summary_window.py::open_summary_window`, `_MultiChart`
- **Root cause:** CSV reading, processing, and Canvas point creation formerly
  happened synchronously on the GUI thread at full resolution.
- **Impact:** Long or external workout files can freeze the UI and draw far more
  points than the display has pixels.
- **Recommendation:** Use a worker/progress state and display-only min/max or
  LTTB downsampling. Preserve full-resolution statistics and FIT data.

### P2 — Replace loose dictionaries in core domains

**Status: Completed 2026-07-14.** Power calibration is explicit; finalized
segments/laps are frozen typed records; worker/UI events have TypedDict
contracts; finalization is split into pure phases.

- **Location:** `app/finalized_workout.py`, status events,
  `app/power.py`
- **Root cause:** Core domain objects use mutable `dict[str, Any]`; the frozen
  finalized dataclass contains mutable collections.
- **Impact:** Weak invariants, hidden dependencies, and difficult concurrent or
  multi-workout reasoning.
- **Recommendation:** Use immutable typed records/enums for segments and laps.

### P2 — Replace broad GUI mixin state with explicit controllers

**Status: Completed 2026-07-14.** Recording, program, and export lifecycle
state now has dedicated typed controllers. The type-checking mixin surface is
limited to shared widgets and methods instead of owning lifecycle state.

- **Location:** `gui/_mixin_base.py`, `KayakFitGUI` mixins
- **Root cause:** Mixins depend on a large shared surface declared only for type
  checking.
- **Impact:** Runtime ownership is hard to discover and changes can break distant
  modules.
- **Recommendation:** Extract recording, device, program, and export controllers
  with explicit dependencies and small view interfaces.

### P2 — Show missing-HR coverage

**Status: Completed 2026-07-15.** Zone percentages now use total moving time
and the summary displays an explicit “No HR signal” segment.

- **Location:** `gui/summary_window.py::_build_zone_bar`
- **Root cause:** Zone percentages divide by time with valid HR, not total active
  time.
- **Impact:** A workout with substantial HR dropout still displays physiological
  zones summing to 100%.
- **Recommendation:** Add a “No HR signal” segment and show coverage relative to
  moving time.

### P2 — Improve workout history

**Implemented increment 2026-07-13:** History rows now identify effective
planned/free CSV processing with a compact badge and leave imported FIT mode
unclassified. The scale, sorting, search, and pagination work below remains.

- **Location:** `gui/history_window.py`
- **Root cause:** History is limited to 200 files, sorted by modification time,
  and has no search, filter, pagination, or in-window delete.
- **Impact:** Copies reorder workouts and older recordings become inaccessible in
  the list.
- **Recommendation:** Sort by recorded metadata timestamp and load/search
  incrementally.

### P2 — Expand integration and failure-injection tests

**Implemented increment 2026-07-14:** Removed process-global dependency stubs,
made the suite safe in one pytest process, and replaced the manually maintained
mypy test-module list with the `tests.*` package boundary.

- **Location:** `tests/`, CI, packaged build workflow
- **Root cause:** Current tests focus on pure processing and mocked REST/BLE
  boundaries. The former import-time stubs and per-file isolation are removed;
  GUI and hardware boundaries remain lightly exercised.
- **Impact:** Tk interactions, thread races, disk-full behavior, sidecar failure,
  clock changes, long-chart performance, and real consumer compatibility are
  under-tested.
- **Recommendation:** Add controller-level dependency injection, persistence
  failure tests, temporal invariants such as `timer <= elapsed`, packaged-app
  smoke tests, and optional hardware/service integration suites.

### P2 — Align documentation and exposed settings

- **Location:** `README.md`, settings UI, `ConfigManager.DEFAULTS`
- **Root cause:** Some advanced tuning exists only in YAML, appearance is not
  persisted, and device-display energy can be mistaken for exported calories.
- **Impact:** Users can expect controls or activity fields that do not exist.
- **Recommendation:** Clearly distinguish device-display fields from recorded
  fields and expose only safe, understandable tuning controls.
