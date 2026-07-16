# Coding and documentation style

The executable configuration in `pyproject.toml` is authoritative when this
guide and tooling disagree. Ruff and strict mypy must pass with zero errors.

## Required checks

Run from the repository root:

```bash
uv run ruff check .
uv run mypy . --strict
uv run pytest
```

The complete suite runs in one pytest process.

## Python conventions

- Python target: 3.14 or newer as declared in `pyproject.toml`.
- Maximum line length: 100 characters.
- Import groups: standard library, third-party, then local `app`/`gui`, with a
  blank line between groups and alphabetical ordering within each group.
- Public class names use `CapWords`; modules, functions, methods, parameters,
  and variables use `snake_case`; constants use `UPPER_SNAKE_CASE`.
- Internal helpers use a leading underscore.
- Worker entry modules follow `<role>_worker.py` with
  `run_<role>_worker()`.
- Prefer modern built-in generic syntax: `list[str]`, `dict[str, Any]`, and
  `tuple[int, int]`.

## Types

All new or changed application functions and methods require parameter and
return annotations. Strict mypy means:

- no untyped or incompletely typed definitions;
- no bare `dict`, `list`, `Callable`, or `tuple`;
- no implicit `Any` returns from typed functions;
- no subclassing an untyped third-party base without a narrow documented
  suppression;
- no unused `type: ignore` comments.

Use `Any` explicitly when an external library or genuinely heterogeneous event
requires it; an honest `Any` is preferable to a false specific type. Do not let
that become the default for domain objects—new workout, lap, step, result, and
event models should favor dataclasses, enums, protocols, or typed dictionaries.

Preserve numeric boundaries deliberately:

- Protocol/FIT-facing code may use `Decimal` or encoded integers.
- GUI/domain calculations normally use `float` and `int`.
- Conversion and rounding should happen at an explicit boundary, not
  incidentally during formatting.
- Validate finite values and FIT field ranges before encoding.

## Units and naming

Physical quantities include their unit in names whenever ambiguity is possible:

| Suffix | Meaning | Example |
|---|---|---|
| `_m` | metres | `distance_m` |
| `_s` | seconds | `duration_s` |
| `_ms` | milliseconds | `start_time_ms` |
| `_mps` | metres per second | `avg_speed_mps` |
| `_spm` | strokes per minute | `cadence_spm` |
| `_bpm` | beats per minute | `heart_rate_bpm` |
| `_n` | newtons | `pull_force_n` |
| `_w` | watts | `power_w` |

Distinguish wall/epoch time from monotonic elapsed time in both names and
comments. Use `time.monotonic()` for deadlines, freshness, and durations; use
epoch time only for timestamps that must be persisted or exchanged.

## Docstrings and comments

- Use Google-style `Args:`, `Returns:`, and `Raises:` sections for public,
  non-trivial functions and methods.
- Module docstrings are prose and need not use Google sections.
- Do not add ceremonial docstrings to trivial closures and event callbacks.
- Explain why a non-obvious invariant exists, not what a plainly readable line
  does.
- Keep comments synchronized with behavior. In particular, do not claim a
  collection is immutable when it contains mutable lists/dictionaries, or call
  ergometer elapsed time “wall clock” unless it is actually driven by a
  monotonic session clock.
- Link substantial behavioral rationale to `DECISIONS.md` rather than duplicating
  a long history in source comments.

## Domain design rules

- Keep capture, persistence, finalization, presentation, and external upload as
  distinct stages.
- One business rule should have one implementation. FIT, summary, chart legend,
  live preview, and completion card must not independently redefine the same
  aggregate.
- Treat samples as represented intervals where `sample_duration` exists, not as
  timeless points.
- Make active-time and distance ownership explicit at pause and planned-step
  boundaries.
- Do not infer activity from heart rate.
- Only generate derived FIT fields when their source and compatibility purpose
  are explicit. The synthetic Hazewinkel positions must use finalized active
  distance, and each run's GPS anchor must use its exact timer START and prior
  cumulative distance. Do not generate unsupported calories, altitude, or
  arbitrary GPS.
- Historical processing must use recorded metadata.
- Prefer immutable typed outputs from pure processing functions.
- Avoid process-global mutable configuration in reusable calculations.

## GUI and concurrency

- Tk and CustomTkinter widgets are touched only on the GUI thread.
- Workers communicate through scheduled callbacks/events.
- Long filesystem, BLE, FIT, OAuth, or HTTP work belongs outside the GUI thread.
- Every worker operation has a typed terminal outcome; completion is not
  automatically success.
- Destructive actions are disabled while a worker owns the same artifact.
- Use monotonic deadlines and cancellable waits for worker operations.
- Catch widget-teardown exceptions only at the thread/UI boundary, not around
  unrelated business logic.

The existing GUI mixins are supported, but new behavior should avoid widening
their implicit shared state. Prefer explicit controller/service dependencies
when extracting or substantially changing a feature.

## Filesystem and persistence

- Use UTF-8 and explicit CSV delimiters/newline handling.
- Stream workout data incrementally; never keep the only copy in memory.
- Use sibling temporary files plus `os.replace()` for replaceable artifacts.
- Flush and fsync durable state at meaningful recovery boundaries.
- Never report success after swallowing a write error.
- Reserve output filenames without truncating an existing workout.
- When deleting a workout, define and remove its complete owned artifact set.
- Keep user secrets in the OS vault where available and restrict fallback file
  permissions to the owner.

## Input validation and errors

- Validate external values at their point of use.
- Reject NaN/infinity and out-of-domain numeric values.
- Skip a malformed local record only when the remaining result is still
  truthful; log the exact location and reason.
- Preserve a known fallback only when it is documented and semantically safe.
- Use typed exceptions/results at subsystem boundaries.
- Show actionable user-facing failures for recording, metadata, conversion, and
  upload stages.
- Avoid `except Exception` around an entire operation unless it is a top-level
  containment boundary that converts the error into a structured failure.

## Logging

Use the shared `Logger` wrapper:

```python
self.logger = Logger.get_logger(name=__name__)
```

Plain modules may keep a module-level logger. Pure/testable modules can accept
an optional logger to avoid importing GUI or logging infrastructure.

Logging levels:

- `debug`: packet/detail information useful during diagnosis;
- `info`: lifecycle transitions and user-requested operations;
- `warning`: recoverable degradation, skipped local input, stale data;
- `error`: failed operations or loss of expected capability;
- `critical`: fatal startup/process failures.

Do not log access tokens, refresh tokens, client secrets, authorization codes,
or full credential-bearing URLs.

## Tests

- Tests are ordinary pytest-discovered `test_*` functions with plain asserts.
- Add every new test module to the explicit mypy override list in
  `pyproject.toml`.
- Keep tests deterministic and independent of user home/config state by using
  temporary paths and patched boundaries.
- Test invariants, not only example outputs. Important workout invariants include:
  - planned lap timer time never exceeds elapsed time;
  - the sum of attributed active time is conserved;
  - inactive HR prefixes/tails do not extend activity;
  - summary headline/splits match decoded FIT fields;
  - failed FIT replacement preserves the old file;
  - persistence failure cannot produce a success result;
  - pending upload is distinct from confirmed activity creation.
- Use real `fit_tool` decoding in pipeline parity tests; stubs are appropriate
  only for focused message-order/unit tests.

## Documentation maintenance

- User-visible behavior: update `README.md` and `FEATURES.md`.
- Runtime/data-flow changes: update `ARCHITECTURE.md`.
- New or reversed design choice: update `DECISIONS.md`.
- Unresolved issue or prioritization change: update `ROADMAP.md`.
- Development/tooling convention: update this guide and `AGENTS.md` if it
  changes assistant behavior.
- Keep filenames and cross-links exact; canonical general docs use uppercase
  names in `docs/`.
