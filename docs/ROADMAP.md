# Roadmap

This file contains only open work. Implemented behavior belongs in
[FEATURES.md](FEATURES.md), processing rules in
[DATA_TRANSFORMATIONS.md](DATA_TRANSFORMATIONS.md), and architectural rationale
in [DECISIONS.md](DECISIONS.md).

Priority meanings:

- **P0 / Critical** — invalid workout output, silent data loss, or a destructive
  race. No known P0 issue is currently open.
- **P1 / Recommended** — material correctness, reliability, or maintainability
  risk.
- **P2 / Optional** — usability, performance, or architectural improvement.

## P1 — Persist pending Strava transactions

- **Location:** `app/strava_uploader.py::upload_file`, recovery and upload UI.
- **Problem:** When Strava accepts an upload but processing outlasts polling or
  the app shuts down, the upload ID is not retained.
- **Next step:** Persist the upload ID, source FIT, external ID, account context,
  and acceptance time. Resume polling after restart without repeating the POST.

## P1 — Make device tests protocol-aware

- **Location:** `gui/device_scanner.py::test_connection`.
- **Problem:** The settings test proves only that a BLE address accepts a
  connection; an incompatible device can pass and fail when a workout starts.
- **Next step:** Verify the standard heart-rate service and characteristic. For
  the ergometer, perform a safe characteristic or capability check that does
  not reset a workout.

## P2 — Improve workout history

- **Location:** `gui/history_window.py`.
- **Problem:** History is limited to 200 entries, sorted by modification time,
  and has no search, filters, pagination, or in-window deletion. Copies can
  reorder entries and older recordings can disappear from the list.
- **Next step:** Sort by recorded metadata time and load entries incrementally
  with search, filtering, and deletion controls.

## P2 — Expand integration and failure-injection tests

- **Location:** `tests/`, CI, and packaged-build workflows.
- **Problem:** GUI interaction, thread races, storage failures, clock changes,
  long-chart performance, physical devices, and external services remain
  lightly exercised.
- **Next step:** Add controller-level dependency injection, persistence-failure
  tests, temporal invariant checks, packaged-app smoke tests, and optional
  hardware/service suites.

## P2 — Align documentation and exposed settings

- **Location:** `README.md`, settings UI, and `ConfigManager.DEFAULTS`.
- **Problem:** Some advanced values exist only in YAML, and ergometer display
  fields can be mistaken for values recorded or exported by KayakFit.
- **Next step:** Expose only safe, understandable controls and clearly separate
  device-display configuration from recorded and exported metrics.
