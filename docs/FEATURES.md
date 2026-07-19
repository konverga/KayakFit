# User-facing features

This document records behavior that is present in the current codebase. It does
not describe aspirational functionality. Verification labels mean:

- **Tested** — covered by automated processing/unit tests.
- **Implemented** — the complete code path exists, but no automated GUI,
  hardware, or live-service test proves the full interaction.
- **Partial** — usable behavior exists with a known correctness or reliability
  limitation tracked in [ROADMAP.md](ROADMAP.md).
- **Not implemented** — a nearby setting or device field may suggest the
  feature, but the application does not provide it.

## Setup and settings

| Feature | Implementation | Status |
|---|---|---|
| First-run device wizard | `gui/setup_wizard.py` | Tested at the persistence boundary; asks for the printed device name, requires an ergometer selection, and saves both selected devices |
| BLE ergometer scan and selection | `gui/device_scanner.py`, `gui/config_window.py` | Tested; configured names match both platform and advertised BLE names |
| Optional standard BLE HRM scan | Heart Rate Service discovery in `gui/device_scanner.py` | Implemented |
| Device connection tests | `gui/device_scanner.test_connection()` | Partial: tests only the BLE link, not the expected service/protocol |
| Per-workout person and boat weights | main toolbar → workout worker → ergometer handshake | Implemented |
| Five-slot ergometer display configuration | `ConfigManager.DISPLAY_MAP`, settings, handshake | Implemented |
| Fixed reproducible metric policy | `CsvReader.read_all()`, `gui.metrics_format` | Tested; main-window sensor cards use averaged fields, while graph/summary/FIT use instantaneous sensor points except for averaged-input power (D017, D018) |
| Automatic/manual HR zones | `app/stats.py`, settings, device-status UI | Tested |
| Logging level and interval | settings and `WorkoutSession` | Implemented |
| Sound cues and target grace period | settings, program panel | Implemented |
| Automatic Strava upload | settings and export worker | Implemented |
| System/light/dark appearance switch | `gui/main_gui.py` | Implemented; choice is persisted |

Advanced reconnect, stale-data, autopause, inactivity-stop, and power-model
values exist in `config.yml` defaults but are not exposed as ordinary settings.
The README's former reference to a configurable handshake mode is not backed by
the UI; the handshake sequence is fixed by the ergometer implementation.

The “Total Expended Energy” display choice configures the ergometer's own
screen. It does not enable calorie calculation in KayakFit.

## Live workout experience

| Feature | Implementation | Status |
|---|---|---|
| Start/stop button and Space shortcut | `gui/recording_lifecycle.py` | Implemented |
| Two-action stop confirmation | recording lifecycle | Implemented |
| Live time, distance, speed, pace, cadence, HR, pull force, and power | `gui/metrics_format.py` | Implemented with one selected-field policy |
| 200/500/1000 m pace selection | `format_pace()` and toolbar selector | Implemented |
| Live average/maximum previews | `RecordingLifecycleMixin._update_stat()` | Partial: previews are not the authoritative final FIT aggregates |
| HR-zone tile, legend, and ranges | `gui/device_status.py` | Tested at pure-function level |
| Ergometer, HRM, Strava, battery, reconnect, and stale-data indicators | device-status and recording-lifecycle mixins | Implemented |
| Continue without unavailable HRM | `WorkoutSession` plus GUI prompt | Implemented |
| Keep the display awake | `app/keep_awake.py` | Implemented |
| Data-stall warning | `WorkoutSession._watchdog()` | Implemented |
| Automatic stop after prolonged inactivity | workout watchdog | Partial: cumulative-distance resets can cause a false stop |
| Maximum six-hour recording guard | `workout_worker.py` | Implemented |

BLE parsing, packet reassembly, and segmentation are tested without hardware.
Actual compatibility with a particular ergometer firmware, BLE adapter, HR
strap, or packaged operating-system build requires physical integration tests.

## Free workouts and autopause

- The ergometer's `active_paddling` signal is required and authoritative.
- Heart-rate notifications update the latest HR value but never create a row or
  extend workout duration.
- Canonical `session_elapsed__s` and `sample_duration__s` define record windows
  and total elapsed time; raw device timing remains diagnostic.
- Every exact active run owns one free lap, chart segment, and FIT timer run.
- Every activity-flag transition updates the live pause/lap state immediately;
  uncovered canonical time also starts a new run, with no configurable threshold.
- Missing or invalid activity fields are rejected rather than inferred from
  other metrics.
- A forgotten free workout is automatically stopped and saved after the
  configured inactivity interval.

Status: **Tested**, including inactive HR prefixes/tails, free-workout laps,
event balance, strict activity validation, chart breaks, and active-only
distance rebasing.

The BLE parser admits only complete v1 packets with valid activity, elapsed,
distance, and represented-duration fields. Repeated malformed input is shown as
a device-health problem and cannot become a CSV row.

## Planned workouts

Programs are JSON files loaded from `~/KayakFit/programs`. Bundled examples are
copied there once so users can edit them.

Implemented behavior:

- Warm-up, work, rest, cooldown, effort, and custom step labels/types.
- Time-based, distance-based, and open/manual steps.
- Nested repeat blocks with depth, operation, and total-step limits.
- Current and next step, remaining time/distance, step progress, and overall
  program progress.
- Right-arrow/manual next-step control.
- Compact pre-workout step/time/distance preview for the selected program.
- Phase-change, ending-soon, and completion sound cues.
- Targets for HR zone, 200/500/1000 m pace, estimated power, and cadence.
- Configurable grace period before the target badge changes state.
- Automatic workout stop when the last step completes.
- Durable `steps.json` timeline with canonical session-elapsed boundaries in the workout
  directory.
- One summary split/FIT lap per recorded step, with separate elapsed and active
  time. An inactive rest remains visible with blank active averages.
- Planned step bands on time-based summary charts.

Status: **Partial**. Parsing, exact overshoot-preserving progression, and
sample-window attribution across performed-step boundaries are tested. Unknown
duration kinds still silently become open steps.

## Recording, recovery, and files

Each workout can create:

```text
<year>/workout_<timestamp>/
  workout.csv                   durable raw observation timeline
  metadata.json                 processing settings recorded with the workout
  steps.json                    performed planned-step boundaries, when applicable
  activity.fit                  processed activity
```

Recordings are organized under `~/KayakFit/workouts/<year>/`.

Implemented behavior:

- Semicolon-delimited CSV, one row per ergometer response.
- Flush after every row and periodic filesystem sync.
- Read-only protection after close where supported.
- Defensive CSV parsing with required/optional schema handling.
- Atomic metadata and planned-step replacement.
- Active-workout marker and recovery prompt after interruption.
- Post-workout card with summary, FIT, upload, and discard actions.

Status: **Implemented**. CSV persistence failures stop recording with an explicit
partial-workout state, unique workout directories are reserved atomically,
discard is serialized with export while removing owned artifacts, and the
recovery marker uses durable atomic replacement.

## FIT generation and summary

The user-facing calculation rules and free/planned worked examples are in
[DATA_TRANSFORMATIONS.md](DATA_TRANSFORMATIONS.md).

FIT output represents an indoor kayaking activity with:

- Real active ergometer sensor records, plus one metric-free GPS compatibility
  anchor at each active-run start.
- Timer START/STOP events for exact active runs.
- Planned-step laps or exact active-run free-workout laps.
- Session and lap distance, elapsed time, timer time, speed, heart rate,
  cadence, and estimated power.
- A deterministic Hazewinkel out-and-back GPS track derived from finalized
  active-only distance, allowing Strava to calculate overview moving time
  correctly across pauses and include every run's first sample window.
- No calories, altitude, or arbitrary coordinates.
- Atomic destination replacement so a failed rebuild preserves an older valid
  FIT.

The summary provides:

- Active distance plus active, elapsed, and pause time.
- Average/maximum speed, HR, and estimated power.
- Average cadence.
- Selectable speed, power, HR, cadence, and pull-force charts.
- Raw chart points and hover readouts, plus a power tooltip explaining its
  averaged cadence/force inputs.
- Time or cumulative-distance x axis, raw-point hover values, and pause/step
  visualization. In time mode, planned phases use one compact strip above the
  metrics; short steps remain visible without repeated or overlapping labels.
- Time in HR zones.
- Free-workout laps or planned-step splits.

The summary headline and split calculations use the same `FinalizedWorkout` and
immutable finalized metric result as FIT generation. Speed and cadence averages
are represented-time-weighted active instantaneous readings; peaks and FIT
records use those same raw channels. The pull-force chart is instantaneous;
power alone is derived from averaged cadence and force (`app.speed_series`,
D017/D018). Charts and hover readouts show raw recorded points.
Every session/lap average is bounded by its maximum, and splits preserve
finalized centimetre/millisecond values rather than redistributing rounded
units. Decoded-FIT regression tests compare encoded fields with
`WorkoutSummary`, including complete elapsed bounds and removal of HR-only
metrics from active aggregates.

Status: **Implemented**, with these explicit limitations:

1. Step labels are shown in the summary but are not encoded as FIT lap names.
2. Pull force has no corresponding FIT session/lap aggregate.

## Workout history

- Lists the newest 200 CSV recordings recursively.
- Opens the workouts folder in Finder, Explorer, or the Linux file manager.
- Allows an external CSV or FIT file to be selected.
- Shows a compact `Plan`, `Free`, `Invalid`, or `FIT` badge beside each filename.
  CSV mode comes from required versioned metadata; imported FIT files are not
  guessed as planned or free. The first screenful is
  mapped transparently for its final scrollable width before it is revealed, so
  badges appear directly in their stable right-aligned position; older rows load
  below it in responsive batches.
- Opens CSV summaries.
- Filters the loaded list by filename or workout type and supports Up/Down,
  Enter, and Escape keyboard navigation.
- Converts CSV files to FIT.
- Uploads CSV or FIT files to Strava.

Status: **Implemented**. History is ordered by modification time rather than the
recorded workout timestamp and has no pagination or delete action.

## Strava

- OAuth authorization-code flow using a loopback HTTP callback on
  `127.0.0.1`, with manual pasted-code fallback.
- Cryptographic OAuth state validation.
- Connect/disconnect controls and connection indicator.
- Access-token refresh with rotated refresh-token persistence.
- Atomic OS credential-vault storage of the complete authorization when
  available; owner-only YAML fallback. Incomplete credentials are disconnected.
- CSV-to-FIT conversion before upload.
- Stable upload `external_id` based on the FIT filename.
- Asynchronous processing polling and pending-state reporting.
- “View on Strava” after a confirmed activity ID.

Status: **Tested with mocked HTTP behavior**, not against the live service.
Pending upload IDs are not persisted, so status polling cannot resume after a
restart without initiating a new upload workflow.

## Diagnostics and resilience

- Rotating application log under `~/KayakFit/logs`.
- Collapsible in-app activity log with a line cap.
- Native crash trace file through `faulthandler`.
- Tk callback exception logging for windowed builds.
- Bounded worker shutdown and stop-aware BLE reconnect.
- Atomic config and FIT replacement.

Status: **Implemented**. Worker success/failure signaling remains too weak in
some paths, as described in the roadmap.

## Explicitly not implemented

- Real GPS capture or user-selectable routes; FIT export uses only the fixed
  Hazewinkel Strava-compatibility track.
- Calorie calculation or FIT calorie totals.
- Cloud account synchronization other than Strava upload.
- Historical trends, personal records, search, or analytics across workouts.
- A built-in graphical workout-plan editor.
- Linux release packaging as a supported product target.
