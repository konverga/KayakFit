# Development guide

This guide covers day-to-day development of KayakFit from source. Read
[AGENTS.md](AGENTS.md) for repository invariants, [ARCHITECTURE.md](ARCHITECTURE.md)
for runtime design, and [STYLE_GUIDE.md](STYLE_GUIDE.md) before changing core
workout behavior.

## Supported environment

KayakFit targets Python 3.14 and ships on macOS and Windows. The project uses
`uv` for Python installation, dependency resolution, virtual-environment
management, and command execution.

Required tools:

- Git;
- `uv`;
- Bluetooth hardware for device-level manual testing;
- platform build tools only when producing a packaged application.

Runtime dependencies and development tools are declared in `pyproject.toml`.
Exact versions are pinned in `uv.lock`. `requirements.txt` is a generated pip
fallback and must not be edited manually.

## Initial setup

From the repository root:

```bash
uv sync --locked
uv run python kayakfit_gui.py
```

`uv sync --locked` creates `.venv`, installs the default development group, and
fails if `pyproject.toml` and `uv.lock` disagree. Use plain `uv sync` only when
intentionally changing dependencies and updating the lock.

To use the environment without prefixing every command:

```bash
source .venv/bin/activate
```

On Windows PowerShell, activate with `.venv\Scripts\Activate.ps1`.

## Branch workflow

Keep `main` releasable and make changes on a feature or fix branch:

```bash
git switch main
git pull --ff-only
git switch -c feature/short-description
```

Use readable prefixes such as `feature/`, `fix/`, or `chore/`. Do not merge,
push, rebase shared work, or synchronize with a remote unless explicitly
authorized. Integration into `main` is reviewed through Claude for this
repository, so branch documentation and commit messages must make the intent
recoverable without relying on chat history.

Before committing:

1. Inspect the relevant architecture and decision documents.
2. Make the smallest coherent change.
3. Add regression coverage for changed behavior.
4. Update user-facing and architectural documentation where applicable.
5. Run the checks in [TESTING.md](TESTING.md).
6. Review `git diff`, `git diff --check`, and the staged files.

## Running and debugging

Run the source application with:

```bash
uv run python kayakfit_gui.py
```

Useful runtime boundaries:

- `kayakfit_gui.py` starts the Tk application.
- `workout_worker.py` owns the background recording worker entry point.
- `export_worker.py` owns FIT conversion and optional upload work.
- `app/workout_session.py` coordinates BLE capture and durable recording.
- `app/finalized_workout.py` is the shared FIT/summary workout domain.
- `gui/worker_manager.py` marshals worker events onto the GUI thread.

Logs are written beneath the user's `KayakFit/logs` directory. Packaged
windowed builds do not have a console, so the rotating log is the primary
diagnostic source.

User data is stored outside the repository under `~/KayakFit` on macOS and
`C:\Users\<user>\KayakFit` on Windows. Tests must not depend on or modify that
real directory; use temporary paths and patch home/config boundaries.

## Dependency changes

Add or update dependencies through `pyproject.toml`, then regenerate committed
derived files:

```bash
uv lock
uv sync --locked
uv export --format requirements-txt --no-hashes --all-groups -o requirements.txt
```

Review both `uv.lock` and `requirements.txt`. CI verifies that the lock matches
the project definition and that the generated requirements fallback matches the
lock.

Runtime packages belong in `[project].dependencies`. Ruff, mypy, pytest, and
type stubs belong in the `dev` dependency group. PyInstaller-only dependencies
belong in the `build` group so they are not installed or bundled unnecessarily.

## Working on workout processing

Preserve these ownership boundaries:

```text
BLE packets -> CSV observation log -> CsvReader -> FinalizedWorkout
                                                -> FIT
                                                -> summary
```

- CSV capture retains valid stationary and repeated observations.
- The required ergometer `active_paddling` signal owns activity membership;
  heart rate never extends the activity.
- `finalize_workout()` owns records, timer runs, laps, active distance, and
  totals used by FIT and the summary.
- Canonical samples represent `[session_elapsed - sample_duration,
  session_elapsed]`; a zero-duration observation owns no activity time.
- Planned samples may be split across exact performed-step boundaries.
- Summary charts show raw source points and must not redefine statistics.

Changes to this path require invariant tests and decoded-FIT parity coverage,
not only unit tests of a helper.

## GUI and concurrency rules

Tk and CustomTkinter widgets may only be mutated on the GUI thread. BLE,
filesystem, FIT, OAuth, and HTTP work belongs in workers. Workers report
structured events through `WorkerManager`; a thread must never call a widget
directly.

The GUI is composed from mixins. Shared attributes are declared for type
checking in `gui/_mixin_base.py`, but that file does not create runtime state.
Initialize shared state in `gui/main_gui.py` and avoid widening the implicit
mixin surface when an explicit controller or service boundary is practical.

## Documentation responsibilities

- End-user behavior: `README.md` and [FEATURES.md](FEATURES.md).
- Runtime/data flow: [ARCHITECTURE.md](ARCHITECTURE.md).
- Design choice or invariant: [DECISIONS.md](DECISIONS.md).
- Planned or incomplete work: [ROADMAP.md](ROADMAP.md).
- Code conventions: [STYLE_GUIDE.md](STYLE_GUIDE.md).
- Build and release operation: [DEPLOYMENT.md](DEPLOYMENT.md).

Keep documentation factual and based on the current code. Do not mark a feature
implemented merely because a function or button exists; verify its complete
execution path and failure behavior.
