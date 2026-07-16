"""Structured training programs.

A program is an ordered list of steps; steps may be grouped into repeated
blocks. This module defines the data model, flattens a program specification
(with ``repeat`` blocks) into a flat step list, and loads programs from
``~/KayakFit/programs/*.json``. Bundled example programs are copied into that
folder once on first run (``install_examples``) so users can edit, rename, or
delete them like any other plan.

The model is GUI- and BLE-free so it can be unit-tested and reused anywhere.
"""

import json
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .logger import Logger

logger = Logger.get_logger(name=__name__)

# Step completion rules.
TIME = "time"
DISTANCE = "distance"
OPEN = "open"  # advance manually (lap / stop button)

# Step kinds the GUI knows how to colour and sound-cue (mirrors the keys of
# STEP_KIND_COLORS / STEP_KIND_CUES in gui/main_gui.py). Anything else still
# loads but falls back to a generic tile colour / "phase" cue, so we log a
# warning to make an authoring typo (e.g. "Restx") discoverable.
KNOWN_KINDS = frozenset({"warmup", "work", "effort", "rest", "cooldown"})
KNOWN_TARGET_METRICS = frozenset(
    {"hr_zone", "pace_200", "pace_500", "pace_1000", "power", "spm"}
)

# Safety ceiling on a single flattened program. A real interval plan has at
# most a few hundred steps; a few thousand is generous headroom. Enforced
# *during* flattening so a runaway (e.g. deeply nested "repeat") aborts early
# instead of exhausting memory first.
MAX_STEPS = 5000

# Hard cap on the total amount of expansion work, independent of how many
# steps actually get appended. Nested "repeat" blocks expand as 2^N even when
# the innermost block is empty (so MAX_STEPS alone would never trip), which can
# hang the app; this bounds the work regardless.
_MAX_FLATTEN_OPS = 200_000

# Guards against pathologically deep "repeat" nesting turning into a Python
# RecursionError; real plans nest at most a handful of levels.
_MAX_REPEAT_DEPTH = 200


class ProgramError(ValueError):
    """A program spec is structurally invalid or unsafe to load.

    Subclasses ``ValueError`` so existing best-effort ``except (OSError,
    ValueError, ...)`` call sites keep skipping the offending file, while the
    message carries a human-readable reason we can log.
    """


@dataclass
class Step:
    """A single program step."""

    label: str
    kind: str  # warmup | work | rest | cooldown | effort
    duration_kind: str  # TIME | DISTANCE | OPEN
    duration_value: float | None = None  # seconds or metres; None if OPEN
    target: dict[str, Any] | None = None  # {"metric","low","high"} (optional)

    def is_open(self) -> bool:
        """Return True if this step has no fixed time/distance boundary."""
        return self.duration_kind == OPEN or self.duration_value is None


@dataclass
class Program:
    """A named, flattened sequence of steps."""

    name: str
    steps: list[Step] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.steps)


def _fmt_time(seconds: float) -> str:
    total = int(seconds)
    m, s = divmod(total, 60)
    return f"{m}:{s:02d}"


def _default_label(kind: str, duration_kind: str, value: float | None) -> str:
    name = {"warmup": "Warm-up", "work": "Work", "rest": "Rest",
            "cooldown": "Cool-down"}.get(kind, kind.capitalize())
    if duration_kind == TIME and value is not None:
        return f"{name} {_fmt_time(value)}"
    if duration_kind == DISTANCE and value is not None:
        return f"{name} {int(value)} m"
    return name


def _validate_duration_value(raw: Any, field_name: str) -> float:
    """Coerce a ``seconds``/``meters`` value to a sane positive float.

    Raises ``ProgramError`` (never lets an unvalidated value reach label
    generation) for missing, non-numeric, non-finite, or non-positive values —
    e.g. ``"seconds": -30`` (would render as ``-1:30``), ``0`` (a degenerate
    zero-length step), ``"abc"`` or ``[30]`` (previously crashed inside
    ``_fmt_time``'s ``int()`` before the ``float()`` cast could catch them).
    """
    if raw is None:
        raise ProgramError(f"'{field_name}' is required for this step")
    # bool is an int subclass; True/False is never a real duration.
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ProgramError(f"'{field_name}' must be a number, got {raw!r}")
    value = float(raw)
    if value != value or value in (float("inf"), float("-inf")):
        raise ProgramError(f"'{field_name}' must be a finite number, got {raw!r}")
    if value <= 0:
        raise ProgramError(f"'{field_name}' must be positive, got {raw!r}")
    return value


def _warn_unknown_kind(kind: str, source: str) -> None:
    """Log (once per step) when a step ``type`` matches no known kind.

    Compared case-insensitively to mirror the GUI's ``.lower()`` lookup, so a
    mere case difference (``"REST"``) is *not* flagged — only strings that map
    to no known kind at all (a typo like ``"Restx"``), which would silently get
    the generic colour/cue.
    """
    if kind.lower() not in KNOWN_KINDS:
        logger.warning(
            "Program %r: unrecognized step type %r (known: %s) — "
            "it will use the generic tile colour and sound cue.",
            source, kind, ", ".join(sorted(KNOWN_KINDS)),
        )


def _make_step(item: dict[str, Any], source: str = "Program") -> Step:
    duration = item.get("duration", {})
    if duration is None:
        duration = {}
    if not isinstance(duration, dict):
        raise ProgramError(f"'duration' must be an object, not {type(duration).__name__}")
    kind = duration.get("kind", OPEN)
    if kind == TIME:
        value: float | None = _validate_duration_value(duration.get("seconds"), "seconds")
    elif kind == DISTANCE:
        value = _validate_duration_value(duration.get("meters"), "meters")
    else:
        kind = OPEN
        value = None
    step_type = item.get("type", "effort")
    if not isinstance(step_type, str):
        raise ProgramError(f"step 'type' must be text, got {type(step_type).__name__}")
    # Unknown kinds are kept (a typo'd rest step should still be usable) but
    # logged, rather than silently rewritten or rejected.
    _warn_unknown_kind(step_type, source)
    label = item.get("label") or _default_label(step_type, kind, value)
    target = _validate_target(item.get("target"))
    return Step(
        label=str(label),
        kind=step_type,
        duration_kind=kind,
        duration_value=value,
        target=target,
    )


def _validate_target(raw: Any) -> dict[str, Any] | None:
    """Validate an optional live target before GUI code receives it."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ProgramError("step 'target' must be an object")
    metric = raw.get("metric")
    if metric not in KNOWN_TARGET_METRICS:
        raise ProgramError(f"unsupported target metric {metric!r}")
    low = raw.get("low")
    high = raw.get("high")
    if (
        isinstance(low, bool)
        or isinstance(high, bool)
        or not isinstance(low, (int, float))
        or not isinstance(high, (int, float))
    ):
        raise ProgramError("target 'low' and 'high' must be numbers")
    low_f, high_f = float(low), float(high)
    if not all(v == v and v not in (float("inf"), float("-inf")) for v in (low_f, high_f)):
        raise ProgramError("target bounds must be finite")
    if low_f > high_f:
        raise ProgramError("target 'low' must not exceed 'high'")
    if metric == "hr_zone" and (low_f < 1 or high_f > 5):
        raise ProgramError("heart-rate zone targets must be within Z1..Z5")
    return {"metric": metric, "low": low, "high": high}


def _flatten(
    items: Any,
    out: list[Step],
    source: str = "Program",
    budget: list[int] | None = None,
    depth: int = 0,
) -> None:
    """Expand ``repeat`` blocks into ``out``, validating structure as we go.

    ``budget`` is a single-element mutable counter of expansion operations,
    bounding total work (see ``_MAX_FLATTEN_OPS``) so a runaway/exponential
    nested ``repeat`` aborts early instead of hanging or OOM-ing. Structural
    problems (``steps`` not a list, a step that isn't an object, a bad
    ``repeat`` count) raise ``ProgramError`` locally and predictably rather
    than surfacing as whatever incidental ``AttributeError`` the first ``.get``
    happens to throw.
    """
    if budget is None:
        budget = [0]
    if depth > _MAX_REPEAT_DEPTH:
        raise ProgramError(f"'repeat' nesting exceeds {_MAX_REPEAT_DEPTH} levels")
    if not isinstance(items, list):
        raise ProgramError(f"'steps' must be a list, not {type(items).__name__}")
    for item in items:
        budget[0] += 1
        if budget[0] > _MAX_FLATTEN_OPS:
            raise ProgramError(
                "program is too large to expand "
                f"(over {_MAX_FLATTEN_OPS} operations — runaway 'repeat'?)"
            )
        if not isinstance(item, dict):
            raise ProgramError(f"each step must be an object, not {type(item).__name__}")
        if "repeat" in item:
            raw = item["repeat"]
            try:
                count = max(int(raw), 0)
            except (TypeError, ValueError) as e:
                raise ProgramError(
                    f"'repeat' must be a whole number, got {raw!r}"
                ) from e
            nested = item.get("steps", [])
            for _ in range(count):
                _flatten(nested, out, source, budget, depth + 1)
        else:
            if len(out) >= MAX_STEPS:
                raise ProgramError(f"program exceeds the {MAX_STEPS}-step limit")
            out.append(_make_step(item, source))


def load_program(spec: dict[str, Any], source: str | None = None) -> Program:
    """Build a Program from a specification dict (flattening repeat blocks).

    ``source`` (a filename, if known) only sharpens log messages. Raises
    ``ProgramError`` for a structurally invalid or unsafe spec.
    """
    if not isinstance(spec, dict):
        raise ProgramError(f"program spec must be an object, not {type(spec).__name__}")
    name = spec.get("name", "Program")
    src = source or (name if isinstance(name, str) else "Program")
    steps: list[Step] = []
    _flatten(spec.get("steps", []), steps, src, budget=[0], depth=0)
    return Program(name=str(name), steps=steps)


# --------------------------------------------------------------------------- #
# Program directories                                                         #
# --------------------------------------------------------------------------- #
def _presets_dir() -> Path:
    """Folder holding the built-in preset programs (bundled JSON).

    Uses the PyInstaller extraction dir when frozen, otherwise the repo's
    ``presets/`` folder next to the ``app`` package when running from source.
    """
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "presets"
    return Path(__file__).resolve().parent.parent / "presets"


def programs_dir() -> Path:
    """Folder holding the user's programs (examples are installed here too)."""
    return Path.home() / "KayakFit" / "programs"


# Marker written after the one-time example install, so examples the user
# deletes or edits are never re-copied on later launches.
_EXAMPLES_MARKER = ".examples-installed"


def install_examples(dest: Path | None = None) -> int:
    """Copy the bundled example programs into the user's programs folder.

    Runs once (guarded by a marker file in the destination). Existing files
    are never overwritten. Best-effort: returns the number of files copied,
    0 if already installed or on error.
    """
    target = dest if dest is not None else programs_dir()
    marker = target / _EXAMPLES_MARKER
    try:
        if marker.exists():
            return 0
        target.mkdir(parents=True, exist_ok=True)
        copied = 0
        for src in sorted(_presets_dir().glob("*.json")):
            dst = target / src.name
            if not dst.exists():
                shutil.copyfile(src, dst)
                copied += 1
        marker.write_text("Bundled example programs were installed here.\n",
                          encoding="utf-8")
        return copied
    except OSError:
        return 0


def _record_skip(warnings: list[str] | None, path: Path, reason: str) -> None:
    """Log (and optionally collect) a per-file load failure.

    Failures are logged at WARNING; the collected ``warnings`` list lets a
    caller (the GUI) surface a "N plan(s) skipped" summary without re-scanning.
    """
    logger.warning("Program %s skipped: %s", path.name, reason)
    if warnings is not None:
        warnings.append(f"{path.name}: {reason}")


def _load_programs_from(
    directory: Path, warnings: list[str] | None = None
) -> list[Program]:
    """Load every valid ``*.json`` program in a directory (best-effort, sorted).

    A single malformed file never takes down the rest: every failure mode
    (unreadable/undecodable file, invalid JSON, BOM/non-UTF-8, structurally
    malformed spec, negative/non-numeric durations, runaway ``repeat``) is
    caught, logged with a reason, and skipped individually. A file with no
    steps to run is distinguished from an actual failure: it is logged at INFO
    (not WARNING) and simply not added, so "nothing to run" is never conflated
    with "failed to load".
    """
    out: list[Program] = []
    if not directory.exists():
        return out
    seen_names: dict[str, str] = {}
    for path in sorted(directory.glob("*.json")):
        # NOTE: encoding="utf-8" (not utf-8-sig) is deliberate — a UTF-8 BOM
        # (common from Notepad) makes json.load raise, so such a file is
        # skipped *and logged* rather than silently vanishing. We favour a
        # discoverable skip over silently accepting a mis-encoded file.
        try:
            with open(path, encoding="utf-8") as f:
                spec = json.load(f)
        except (OSError, ValueError) as exc:
            # ValueError covers json.JSONDecodeError and UnicodeDecodeError.
            _record_skip(warnings, path, f"could not be read ({exc})")
            continue

        if not isinstance(spec, dict):
            _record_skip(warnings, path, "top-level JSON is not an object")
            continue

        # Distinguish "no steps to run" from "failed to load". An explicit
        # empty (or missing) step list is a syntactically fine but non-runnable
        # program: there is nothing to do, so it is not added to the list, but
        # it is logged at INFO — clearly *not* the same as a parse/validation
        # error. Anything else falsy under "steps" (e.g. 0, "") is malformed.
        steps_spec = spec.get("steps")
        if not steps_spec:
            if "steps" not in spec or steps_spec == []:
                logger.info(
                    "Program %s has no steps; skipping (nothing to run).", path.name
                )
            else:
                _record_skip(warnings, path, "'steps' is empty or not a list")
            continue

        spec.setdefault("name", path.stem)
        try:
            prog = load_program(spec, source=path.name)
        except ProgramError as exc:
            _record_skip(warnings, path, str(exc))
            continue
        except (OSError, ValueError, TypeError, AttributeError, KeyError,
                IndexError, RecursionError, MemoryError) as exc:
            # Defence in depth: any unforeseen malformed-structure error (or a
            # RecursionError/MemoryError from a runaway file) must skip only
            # this one file, never propagate and wipe the whole program list.
            _record_skip(warnings, path, f"failed to load ({type(exc).__name__}: {exc})")
            continue

        # Duplicate program names silently shadow one another in the GUI's
        # name-keyed dict, so warn (naming both files) when one is detected.
        prior = seen_names.get(prog.name)
        if prior is not None:
            logger.warning(
                "Program name %r in %s duplicates %s; one will shadow the "
                "other in the menu.", prog.name, path.name, prior,
            )
            if warnings is not None:
                warnings.append(
                    f"{path.name}: duplicate name {prog.name!r} (also in {prior})"
                )
        seen_names[prog.name] = path.name
        out.append(prog)
    return out


def builtin_programs(warnings: list[str] | None = None) -> list[Program]:
    """Bundled example programs (the install source for ``install_examples``)."""
    return _load_programs_from(_presets_dir(), warnings)


def user_programs(warnings: list[str] | None = None) -> list[Program]:
    """Load programs from ~/KayakFit/programs/*.json (best-effort)."""
    return _load_programs_from(programs_dir(), warnings)


def list_programs(warnings: list[str] | None = None) -> list[Program]:
    """All available programs.

    Everything lives in ``~/KayakFit/programs/`` — the bundled examples are
    copied there once (``install_examples``), alongside user-authored plans.

    Pass a list as ``warnings`` to collect one human-readable string per
    skipped/shadowed file (the same reasons are always logged regardless).
    """
    return user_programs(warnings)
