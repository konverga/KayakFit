# Contributing to KayakFit

This document describes how the KayakFit repo is set up on GitHub and the
workflow every contributor (including solo maintainers) should follow: making
changes, opening pull requests, and cutting releases.

---

## 1. Overview: what lives where

| File / path | Purpose |
|---|---|
| `.github/workflows/ci.yml` | Runs the lint + type checks (`ruff`/`mypy`, on Linux) and the test suite (macOS + Windows) automatically on every PR and push to `main`. All jobs must pass to merge. |
| `.github/workflows/build-release.yml` | Builds the macOS `.app` (Apple Silicon) and Windows `.exe` and attaches them to a GitHub Release. |
| `.github/CODEOWNERS` | Who is required to review changes. |
| `.github/PULL_REQUEST_TEMPLATE.md` | Checklist that pre-fills every new PR. |
| `.github/ISSUE_TEMPLATE/` | Structured forms for bug reports and feature requests. |
| `KayakFit.spec` | PyInstaller build spec used by both local builds and CI. |
| `pyproject.toml` | uv project config: direct dependencies (`[project]`), the `dev`/`build` dependency-groups, plus ruff/mypy/pytest settings. |
| `uv.lock` | The fully-resolved, pinned dependency graph. Source of truth for exact versions; `uv sync` installs it. Commit it. |
| `requirements.txt` | **Generated** pip fallback, exported from `uv.lock` for environments without uv. Don't hand-edit. |
| `docs/ARCHITECTURE.md` | Runtime design from BLE capture through FIT, summary, and upload. Keep it current when behavior changes. |
| `docs/AGENTS.md` | Repository instructions and invariants for AI assistants. |
| `docs/STYLE_GUIDE.md` | Coding, typing, testing, and documentation conventions. |
| `docs/DEVELOPMENT.md` | Local environment, source workflow, dependencies, and debugging. |
| `docs/TESTING.md` | Required checks, test isolation, coverage map, and smoke testing. |
| `docs/DEPLOYMENT.md` | Local package builds, release publication, verification, and rollback. |
| `docs/ROADMAP.md` | Prioritized open audit findings and planned work. |

If you only remember one rule: **`main` is always releasable.** Nothing gets
built or shipped from a branch — only tagged commits on `main` produce a
release.

---

## 2. Making a change (feature branches)

First-time setup (once per clone): the project uses **uv** for dependency
management and targets **Python 3.14** — the same version CI and the release
build use. One command creates the `.venv`, fetches Python 3.14 if you don't
have it, and installs exactly what `uv.lock` pins (runtime deps + the `dev`
tooling group):

```bash
uv sync
```

Then run commands with `uv run <cmd>` (no manual activation needed), or
`source .venv/bin/activate` once and run them directly. If `uv.lock` doesn't
exist yet, `uv sync` (or `uv lock`) generates it — commit the result so everyone
resolves to the same versions. A `requirements.txt` is exported from the lock as
a pip fallback for environments without uv; regenerate it with
`uv export --format requirements-txt --no-hashes --all-groups -o requirements.txt`.

1. Branch off `main`:

   ```bash
   git checkout main
   git pull
   git checkout -b feature/short-description
   ```

   Branch prefixes are not enforced by tooling, but use something readable:
   `feature/...`, `fix/...`, `chore/...`.

2. Make your change. Run the relevant tests **and** the lint/type checks
   locally before pushing — all three are merge requirements for `main`
   (see section 3 for `ruff`/`mypy` setup):

   ```bash
   uv run ruff check .          # lint + import order — must be clean
   uv run mypy . --strict                # static type checking — must be clean
   uv run pytest tests/test_pipeline.py
   uv run pytest tests/test_read_csv.py
   # ...or the complete suite:
   uv run pytest
   ```

   The suite runs in one pytest process using the locked runtime dependencies.

3. Push the branch and open a pull request into `main`. GitHub will
   pre-fill the PR description from `.github/PULL_REQUEST_TEMPLATE.md` —
   fill in the summary, changes, and testing sections, and check off the
   checklist items honestly (don't tick "tests pass" without running them).

**Opening a PR does not build or release anything.** `build-release.yml`
only runs on a version tag or a published Release — see section 5.

---

## 3. Code and naming conventions

### Linting and type checking (required)

Two checks must pass with **zero errors** on every pull request into `main`,
alongside the test suite: **`ruff`** (lint + import ordering) and **`mypy`**
(static type checking, in **`strict`** mode). Their rules live in
`pyproject.toml` (`[tool.ruff]` / `[tool.mypy]`); `docs/STYLE_GUIDE.md` has the fuller
rationale. Strict means new code needs complete annotations and no bare generics
(`dict[str, Any]`, not `dict`); the unstubbed GUI/BLE/FIT libraries are kept
non-fatal via `ignore_missing_imports`. **CI enforces both** —
the `lint` job in `ci.yml` runs `uv run ruff check .` and `uv run mypy . --strict` on every PR, and a
failure blocks the merge — so run them locally first to avoid a red build.

Both live in the `dev` dependency-group in `pyproject.toml` (along with the
`types-*` stub packages mypy reads and `pytest`), so the first-time `uv sync`
from section 2 already installs them at the exact versions CI uses — no separate
install step. They're build/dev-time only: PyInstaller doesn't bundle them into
the shipped app. Run both from the repo root before pushing:

```bash
uv run ruff check .            # lint + import order
uv run mypy . --strict                  # static type checking (whole repo)
```

`mypy` runs strict without a `--strict` flag: `strict = true` lives in
`[tool.mypy]` in `pyproject.toml`, so `uv run mypy . --strict` (and your editor, and CI)
all pick it up from config. It checks `tests/` too — a `[[tool.mypy.overrides]]`
block relaxes only the annotation-presence rules there (listing the test modules
by name, since without a `tests/__init__.py` mypy sees them as top-level
`test_*` modules and a `test_*` glob isn't a legal pattern).

A PR that reports any `ruff` or `mypy` error is not ready to merge — fix the
findings rather than suppressing them wholesale. Reach for `# noqa: <rule>` or
`# type: ignore[<code>]` only for the deliberate exceptions already documented
in `docs/STYLE_GUIDE.md` (e.g. intentional `Any` use and docstring-presence
judgment calls),
and always pin the specific rule/error code: `warn_unused_ignores` is enabled,
so a suppression that stops being necessary becomes an error in its own right.

Two build-output details worth knowing so a clean checkout stays clean:

- The PyInstaller output dirs (`build-mac/`, `dist-mac/`, `build-win/`,
  `dist-win/`) and `.venv` are excluded from **both** tools — Ruff via
  `extend-exclude` and mypy via its `exclude` regex (which matches `/` and `\`
  so it holds on Windows too). Don't type-check or lint a packaged app.
- `tests/*.py` are exempt from the docstring/annotation rules (Ruff
  `per-file-ignores`), and a matching mypy `tests`/`test_*` override relaxes the
  same *annotation-presence* strictness there. They are **not** otherwise exempt
  from mypy — real type errors in tests (bad generics, attribute typos,
  non-overlapping comparisons) still fail the check.

### Naming conventions

The naming rules the codebase follows:

| Kind | Convention | Examples |
|---|---|---|
| Modules / packages / files | `snake_case`, lowercase | `workout_worker.py`, `export_fit.py`, `app/`, `gui/` |
| Classes | `CapWords`; initialisms title-cased, not shouted | `WorkoutSession`, `CsvReader`, `FitExporter`, `LiveSegmenter` |
| Functions, methods, variables, parameters | `snake_case` | `compute_summary`, `detect_segments`, `max_gap_seconds` |
| Constants / module-level config | `UPPER_SNAKE_CASE` | `DEFAULT_MAX_GAP_SECONDS`, `MPS_TO_KMH` |
| Internal / non-public helpers | leading underscore | `_axis_bounds`, `_lap_summary`, `_MultiChart` |

Two conventions specific to this codebase:

- **Suffix physical quantities with their unit**, matching the CSV column
  schema in `app/field_mapping.py`: `_m` (metres), `_s` (seconds), `_ms`
  (milliseconds), `_mps` (metres/second) — e.g. `distance_m`, `duration_s`,
  `start_ms`, `avg_speed_mps`. A bare quantity name is a smell; name the unit.
- **Worker entry modules are named `<role>_worker.py`** with a matching
  `run_<role>_worker` entry function — `workout_worker.py` /
  `run_workout_worker`, `export_worker.py` / `run_export_worker`.

Also preserve the deliberate `Decimal`/`float` split (FIT-facing code uses
`Decimal`/`int`, GUI-facing code uses `float`/`int`; see `app/stats.py`),
keep imports in three alphabetized groups (stdlib, third-party, local — with
`app` and `gui` as first-party), and hold to the 100-column line length.

---

## 4. Pull requests and code review

Every PR into `main` automatically:

- Triggers **CI** (`ci.yml`), which runs two kinds of job, all of which must
  pass:
  - **`lint`** — `ruff check .` and `mypy . --strict` on Linux (see section 3).
  - **`test`** — the full `tests/test_*.py` suite on macOS and Windows, the two
    platforms the app ships on.
- Requires **review from the code owner(s)** listed in `.github/CODEOWNERS`
  (currently `@konverga` for the whole repo) before it can be merged, once
  branch protection is enabled (see section 7 — this is a one-time repo
  setting, not something enforced by a file).

Guidelines for the PR itself:

- **`ruff check .` and `mypy . --strict` must both pass with zero errors** before the PR
  is merged into `main` (see section 3). CI's `lint` job runs them on every PR,
  so run them locally first — a clean lint/type-check is a merge requirement,
  exactly like green tests.
- Keep PRs scoped to one feature/fix — easier to review, easier to revert.
- Link the related issue with `Closes #123` if one exists.
- If your change affects behavior end users would notice, update `README.md`
  in the same PR. If it changes how the app works internally (BLE
  handshake/polling, packet parsing, FIT export, config handling), update
  `docs/ARCHITECTURE.md` too, so the internals doc doesn't drift.
- If your change touches Bluetooth parsing, FIT export, or config handling,
  add or update a test in `tests/` — there's no formal coverage requirement,
  but these are the modules most likely to break silently.

Once CI is green and the PR is approved, merge it (squash merge is
recommended to keep `main`'s history one commit per change). Deleting the
branch after merge is safe — nothing references it.

---

## 5. Versioning and releasing a new version

### Version numbers

KayakFit uses [semantic versioning](https://semver.org/): every release is
`MAJOR.MINOR.PATCH`. The project **starts at `0.1.0`** and stays in the `0.x`
range during initial development. The current number is `__version__` in
`app/__init__.py`, mirrored by `version` in `pyproject.toml` — **bump both
together** (they're expected to match). The git tag you push at release time
must match them (see below), and the build bakes that tag into the macOS bundle
version and the Windows file/product version.

Bump the number by the *largest* kind of change since the last release:

| Change | While in `0.x` (now) | After `1.0.0` |
|---|---|---|
| Bug fix / internal-only change, no behaviour change users rely on | **PATCH** (`0.1.0 → 0.1.1`) | PATCH (`1.4.2 → 1.4.3`) |
| New feature, **or** a breaking change (incompatible config/CSV/FIT format, removed setting) | **MINOR** (`0.1.0 → 0.2.0`) | MINOR for features (`1.4.0 → 1.5.0`), MAJOR for breaking (`1.4.0 → 2.0.0`) |
| First stable, API/format-committed release | — | promote to `1.0.0` |

While the project is in `0.x`, the public surface (config file, CSV/FIT
layout, presets) is explicitly **not** guaranteed stable — per the semver spec,
`0.y.z` is for initial development and anything may change. A breaking change
therefore bumps MINOR, not MAJOR, until the project is deliberately promoted to
`1.0.0`. Update `__version__` (and the matching `version` in `pyproject.toml`)
in the same PR as the change that warrants the bump, so `main` always reflects
the version it would release as.

### Cutting the release

Releases are **tag-triggered**, not merge-triggered. Merging PRs into `main`
never publishes a build. To ship:

1. Make sure `main` is in the state you want to release (all the PRs you
   want are merged, CI is green) and `__version__` in `app/__init__.py` is the
   number you intend to tag.

2. Confirm the version number using the table above — e.g. `0.1.1` for a bug
   fix, `0.2.0` for a new feature or a breaking format change while in `0.x`.

3. Tag the commit on `main` and push the tag:

   ```bash
   git checkout main
   git pull
   git tag v0.2.0
   git push origin v0.2.0
   ```

   Pushing the tag alone is enough — `build-release.yml` will build both
   platforms and create the GitHub Release automatically. Publishing a release
   through the GitHub UI does not trigger this workflow; edit the generated
   release notes after the tagged build finishes if they need customization.

4. Wait for the **Build & Release** workflow to finish (Actions tab). When
   it completes, the Release page will have two assets attached:
   `KayakFit-macOS-arm64.zip` (Apple Silicon) and `KayakFit-Windows.zip`. Both
   are built with the version number baked in (macOS bundle version and Windows
   file/product version both read from the tag).

5. Sanity-check the release notes and assets, then announce/share as needed.

**Testing a build without releasing:** run the same workflow manually from
Actions → Build & Release → Run workflow. This builds both platforms and
uploads them as workflow artifacts (visible on the run's summary page) but
does **not** create or attach anything to a Release.

Currently there is no code signing configured, so macOS will show a
Gatekeeper warning ("unidentified developer") and Windows will show a
SmartScreen warning on first run of a downloaded build. This is expected
until signing certificates are set up.

---

## 6. Reporting bugs and requesting features

- Bugs: open an issue using the **Bug Report** template — include OS,
  Python version (if running from source), and ergometer/firmware if
  relevant.
- Feature ideas: use the **Feature Request** template.
- General questions or open-ended discussion: use
  [Discussions](https://github.com/konverga/KayakFit/discussions) instead
  of an issue — blank issues are disabled to keep the issue tracker focused
  on actionable work.

---

## 7. One-time repo setup (for admins)

The pieces above assume `main` is protected. If this hasn't been configured
yet, an admin should go to **Settings → Branches → Add branch protection
rule** for `main` and enable:

- Require a pull request before merging (blocks direct pushes to `main`).
- Require status checks to pass before merging — select all three checks that
  `ci.yml` produces: `lint`, `test (macos-14)`, and `test (windows-latest)`.
  They only appear as options after the workflow has run at least once, so open
  one throwaway PR first. (The `lint` check is what actually makes the `ruff` /
  `mypy` requirement in sections 3-4 blocking rather than advisory.)
- Require review from Code Owners.
- Do not allow bypassing the above settings.
- Disable force pushes and branch deletion for `main`.

This is what actually makes CODEOWNERS and CI enforce anything — without it,
they run/exist but nobody is required to act on them.

---

## Quick reference

```
feature branch  --PR-->  main  --tag push (vX.Y.Z)-->  GitHub Release
                          |                                  |
                       ci.yml                        build-release.yml
              (lint: ruff + mypy on Linux;      (builds mac + windows, attaches
               tests on mac + windows;                  to the release)
               every PR/push)
```
