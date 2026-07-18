# Instructions for AI assistants

This is the repository-level orientation for every AI assistant working on
KayakFit. Read this file before changing code. Then consult the domain document
that matches the task:

- [ARCHITECTURE.md](ARCHITECTURE.md) — runtime design and data flow.
- [DATA_TRANSFORMATIONS.md](DATA_TRANSFORMATIONS.md) — user-facing sensor,
  summary, FIT, and Strava calculation rules.
- [FEATURES.md](FEATURES.md) — implemented user-facing behavior and verification status.
- [ROADMAP.md](ROADMAP.md) — open findings and prioritized work.
- [DECISIONS.md](DECISIONS.md) — architectural rationale and invariants.
- [STYLE_GUIDE.md](STYLE_GUIDE.md) — coding, typing, testing, and logging conventions.
- [DEVELOPMENT.md](DEVELOPMENT.md) — local setup and day-to-day change workflow.
- [TESTING.md](TESTING.md) — automated gates, invariant tests, and manual checks.
- [DEPLOYMENT.md](DEPLOYMENT.md) — packaged builds, releases, and rollback.
- [POWER_MODEL.md](POWER_MODEL.md) — estimated-power calibration.
- `README.md` — end-user setup and operation.
- `CONTRIBUTING.md` — branches, pull requests, CI, and releases.

## Repository purpose

KayakFit is a Python 3.14 CustomTkinter desktop application for macOS and
Windows. It connects to a KayakFirst Bull ergometer and an optional standard BLE
heart-rate monitor, displays live workout data, streams the raw recording to
CSV, creates an indoor-kayaking FIT activity, shows a post-workout summary, and
can upload the FIT file to Strava.

The main dependencies are `bleak`, `customtkinter`, `fit_tool`, `requests`, and
PyInstaller. Dependencies are declared in `pyproject.toml` and pinned by
`uv.lock`. `requirements.txt` is a generated fallback and must not be edited by
hand.

## Branch and integration rules

- Work on a feature or fix branch created from `main`; keep `main` releasable.
- Do not merge, push, or synchronize a branch unless the user explicitly asks.
- Integration into `main` is performed through Claude. Leave a clear branch
  history and update these documents when behavior or architecture changes so
  Claude can review the branch without reconstructing its intent.
- Preserve unrelated user changes in a dirty worktree.
- Do not rewrite history, force-push, reset destructively, or discard user work.
- Before handing off a behavioral change, update `FEATURES.md`, `ROADMAP.md`,
  `DECISIONS.md`, or `ARCHITECTURE.md` as appropriate.

The current feature branch introduced the shared FIT/summary processing model.
Its rationale has been consolidated into [DECISIONS.md](DECISIONS.md), and the
remaining audit findings are in [ROADMAP.md](ROADMAP.md).

## Build, run, and validate

```bash
uv sync
uv run python kayakfit_gui.py
uv run ruff check .
uv run mypy . --strict
```

```bash
uv run pytest
```

After reliability phases 1–4 on 2026-07-14,
all 131 tests, Ruff, and strict mypy passed.

## Environment cautions

- A local `.venv` can contain platform-specific Python symlinks. Do not repair
  or commit it when it fails in a different operating system or sandbox.
- An offline environment may lack `fit_tool`, pytest, Ruff, or mypy. Distinguish
  missing tooling from a product regression and report what could not be run.
- Use a writable temporary uv cache in restricted environments when necessary;
  do not alter project files merely to work around a sandbox cache restriction.
- PyInstaller output directories and `.venv` are generated artifacts and are
  excluded from linting, typing, and version control.

## Critical domain invariants

These rules must survive every refactor:

1. The CSV is the durable observation log. Valid stationary and repeated rows
   are retained; segmentation happens later.
2. `active_paddling` is required and authoritative for whether an ergometer
   sample belongs to the FIT activity. Heart-rate notifications never create
   records or extend the activity.
3. `finalize_workout()` owns the finalized active record set, timer runs, laps,
   and totals consumed by both FIT generation and the summary.
4. Planned workouts group active samples by performed step; they do not add
   inactive samples to averages. Fully inactive planned rests may remain visible
   with blank active statistics.
5. Free workouts use every exact positive-duration activity run for FIT timer
   events, laps, live pause/lap state, and chart breaks. Uncovered canonical
   time breaks a run; no autopause threshold is configurable.
6. Historical processing uses the recording's `metadata.json`, not whatever
   settings happen to be current later.
7. FIT output is written to a sibling temporary file and atomically replaces the
   destination only after a successful build.
8. The synthetic Hazewinkel GPS track is a documented Strava-compatibility
   field calculated only from finalized active distance. Each active run starts
   with one metric-free anchor record at its exact timer START so its GPS span
   covers the complete first sample window. Do not fabricate calories, altitude,
   arbitrary coordinates, or other unsupported measurements.
9. Worker threads never manipulate Tk widgets directly; they send events that
   are marshalled onto the GUI thread.
10. A completed Strava upload and a merely accepted/pending upload are different
    states.

Open violations or weaknesses around these invariants are tracked in
[ROADMAP.md](ROADMAP.md); do not treat the presence of a shared function as
proof that every encoded result is valid.

## Error-handling policy

For user-controlled or externally supplied data—program JSON, workout CSV,
sidecars, BLE packets, and API responses—use this hierarchy:

1. Validate at the point of use, not only at initial parsing.
2. If records are independent, skip only the malformed local unit and log its
   identity and reason.
3. Do not infer missing v1 workout fields or change planned workouts to free.
4. Fail the operation when continuing would create a misleading workout or
   falsely report persistence/export success.
5. Surface failures that affect saved data, workout interpretation, or external
   state to the user; a log entry alone is not sufficient.

Examples already implemented include per-file program rejection, per-row CSV
recovery with a corruption threshold, strict rejection of unusable planned-step
sidecars, and terminal propagation of CSV write failures without false
saved-row or automatic-export success.

## Repository map

```text
kayakfit_gui.py              process entry point and Tk main loop
workout_worker.py            async recording worker entry
export_worker.py             FIT/export/upload worker entry

app/
  ble_device.py              common BLE retry/reconnect lifecycle
  kayakfirst_ergometer_bluetooth.py
                              ergometer protocol, handshake, polling, parsing
  heart_rate_monitor_bluetooth.py
                              standard HR and battery notifications
  workout_session.py         recording orchestration and sidecars
  write_csv.py               streaming semicolon CSV writer
  read_csv.py                defensive CSV reader into Table
  table.py                   lightweight stdlib-only tabular container
  segmentation.py            activity-run and free-lap detection
  finalized_workout.py       shared FIT/summary activity domain
  stats.py                   shared workout aggregates and HR zones
  summary.py                 pure post-workout summary model
  export_fit.py              atomic FIT construction
  program.py                 JSON program model, loading, and validation
  program_runner.py          live planned-step progression
  workout_metadata.py        reproducible processing settings sidecar
  recovery.py                interrupted-workout marker
  power.py                   estimated-power formula
  strava_api.py              direct HTTP API boundary
  strava_auth.py             OAuth loopback flow
  strava_uploader.py         token refresh, upload, and polling

gui/
  controllers.py               typed recording/program/export state owners
  main_gui.py                root window construction and composition
  recording_lifecycle.py     live metrics and workout lifecycle
  program_panel.py           planned-workout UI and targets
  device_status.py           device, HR-zone, pause, and upload status
  export_upload.py           export/upload action coordination
  worker_manager.py          worker threads and GUI event marshalling
  summary_window.py          summary charts, zones, and splits
  history_window.py          workout history and external-file actions
  config_window.py           settings UI
  config_manager.py          config defaults, loading, and persistence

tests/                       isolated plain-assert pytest modules
presets/                     bundled program JSON copied to the user directory
```

`KayakFitGUI` is composed from several mixins. If a method is absent from
`gui/main_gui.py`, inspect the mixin base list and the modules above before
concluding that the behavior is missing or dead.

The project deliberately uses `app.table.Table` instead of Polars to reduce the
packaged application size. Do not reintroduce a dataframe dependency without a
measured need and an explicit architecture decision.
