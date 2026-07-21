# Testing guide

KayakFit uses pytest for behavioral tests, Ruff for lint/import validation, and
strict mypy for static type checking. All three are merge gates.

## Required validation

From the repository root:

```bash
uv sync --locked
uv run ruff check .
uv run mypy . --strict
uv run pytest
git diff --check
```

The suite is safe to run in one pytest process and uses the locked runtime
dependencies rather than replacing packages in `sys.modules`. Do not rely on a
fixed test count; add or update coverage with each behavior change.

## Choosing tests during development

Run focused modules while iterating, then the full required validation before
handoff.

| Area | Primary modules |
|---|---|
| BLE parsing | `tests/test_ble_parsing.py` |
| FIT timer-event ordering | `tests/test_fit_events.py` |
| CSV parsing and corruption recovery | `tests/test_read_csv.py` |
| Canonical capture timing and reconnects | `tests/test_recording_timeline.py` |
| Durable CSV writes and filename collisions | `tests/test_write_csv.py` |
| Activity detection and autopause | `tests/test_segmentation.py` |
| Planned program parsing/execution | `tests/test_program.py` |
| Planned-step laps and sample attribution | `tests/test_program_laps.py` |
| Shared metric aggregation | `tests/test_stats.py` |
| Summary timing, series, and HR zones | `tests/test_summary.py`, `tests/test_hr_zones.py` |
| Instantaneous live-card metric policy | `tests/test_metrics_format.py` |
| Encoded FIT/summary parity | `tests/test_pipeline.py` |
| Recovery marker behavior | `tests/test_recovery.py` |
| Worker outcomes, atomic state and strict configuration | `tests/test_reliability.py` |
| Strava/OAuth/upload outcomes | `tests/test_strava.py` |

Example focused run:

```bash
uv run pytest -q tests/test_program_laps.py
```

## Test design rules

Tests should be deterministic, independent, and based on public behavior or a
deliberate subsystem boundary.

- Use `tempfile` or pytest temporary paths for files.
- Patch home directories, clocks, credentials, devices, and network calls.
- Do not read or mutate the user's real `~/KayakFit` data.
- Prefer fixed epoch timestamps and explicit sample durations.
- Use plain asserts with failure values that explain the violated invariant.
- Use real `fit_tool` encoding/decoding for end-to-end FIT parity.
- Use lightweight stubs only for focused ordering/unit tests that do not claim
  encoded-file correctness.
- Test error and recovery paths, not only the successful path.

Important workout invariants include:

- HR-only prefixes and tails do not extend activity.
- Initial/repeated zero-duration observations and reconnect gaps do not inflate active time.
- Poll command duration does not accumulate into the one-second polling period,
  and a late command never produces catch-up bursts.
- Raw multi-second windows are capped by available canonical elapsed time, so
  represented windows never overlap and uncovered time remains explicit.
- Every emitted FIT record belongs to an active ergometer window.
- Planned lap timer time does not exceed lap elapsed time.
- Planned step attribution conserves active time and active distance when the
  performed timeline covers the session.
- FIT session/lap totals, averages, and maxima match `WorkoutSummary` after
  decoding.
- Every session/lap average is at most its corresponding maximum, and the
  session speed maximum is at least every lap speed maximum.
- FIT speed records, averages, peaks, and summary chart points use active
  instantaneous sensor values.
- Session active time equals summed lap active time, and session active distance
  equals summed lap active distance.
- Split formatting preserves finalized centimetre and millisecond values rather
  than redistributing independently rounded units.
- Inactive distance changes do not inflate FIT/session/chart totals.
- Every distance-bearing active FIT record has a synthetic position derived
  from its finalized cumulative distance; inactive distance cannot advance the
  compatibility route.
- Every active run has a metric-free GPS anchor at its START, and its decoded
  first-to-last GPS span equals its finalized timer time.
- Smoothing cannot change displayed averages or maxima.
- Delayed program samples preserve every crossed transition and exact boundary.
- A persistence failure cannot increment the saved-row count or report normal
  completion.
- Failed FIT replacement preserves the last valid FIT.
- Pending Strava acceptance remains distinct from confirmed activity creation.

## Adding a test module

Name it `tests/test_<area>.py`. Pytest discovers test functions named `test_*`.
Tests may follow the repository's lightweight annotation convention, but mypy
still checks them for real type errors.

The `tests.*` mypy override automatically covers new test modules. It relaxes
annotation presence only; substantive type errors remain failures.

## Manual desktop smoke test

Automated tests do not exercise real Bluetooth permissions, physical devices,
Tk rendering, keychain backends, sleep prevention, or OS packaging. Before a
release, test on both supported platforms where possible.

Suggested smoke flow:

1. Launch from a clean user configuration.
2. Complete or revisit device setup and scan for peripherals.
3. Start a free workout and verify brief low-cadence gaps stay in one lap while
   five continuous inactive seconds trigger the paused state.
4. Stop paddling while HR continues; confirm no HR tail extends the result.
5. Run a short planned workout containing warm-up, work, rest, and cooldown.
6. Verify live step cues, exact step changes, summary splits, and graphs.
7. Save FIT and compare its session/lap fields with the summary.
8. Exercise manual and automatic Strava paths with a test activity if
   credentials are available.
9. Restart after an interrupted recording and verify recovery.
10. Confirm logs contain actionable errors and no credentials.

For packaged builds, repeat startup, Bluetooth access, summary opening, FIT
creation, and credential access from the actual `.app` or `.exe`, not only from
source.

## Restricted or offline environments

If `uv` cannot write its default cache, point it at a writable temporary path:

```bash
UV_CACHE_DIR=/tmp/kayakfit-uv-cache uv run --offline pytest -q tests/test_summary.py
```

Use `--offline` only when the environment is already synchronized. Distinguish
missing dependencies or platform tooling from product failures, and record any
check that could not be run. Do not modify project files or the checked-in lock
merely to work around a sandbox or unavailable network.

## Continuous integration

`.github/workflows/ci.yml` runs on pull requests into `main` and pushes to
`main`:

- Linux: lock consistency, Ruff, strict mypy, and generated-requirements drift.
- macOS and Windows: the complete suite in one pytest process.

A local pass is necessary but not sufficient for merge; platform CI must also
be green.
