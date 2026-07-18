"""Shared activity-run detection.

A workout is split into *segments* (continuous moving periods) separated by
*stationary* periods. These exact runs own FIT timer events and free-workout
laps. ``app.finalized_workout`` groups them into performed steps for a planned
workout.

  * ``LiveSegmenter`` is the online form used by the live workout session to
    drive the dashboard's "Paused" indicator and lap counter in real time.
  * ``resolve_segments`` resolves the authoritative activity-record runs.

Planned-step grouping is intentionally handled later by
``app.finalized_workout``. A plan defines reporting groups; it never changes
which ergometer samples are active.

The ergometer's activity flag is the sole authority for timer time. Speed and
distance remain metrics; they never infer whether the athlete is paddling.
"""

import json
from collections.abc import Callable, Mapping, Sequence
from math import isfinite
from pathlib import Path
from typing import Any

from .coerce import to_float
from .workout_paths import WorkoutPaths


def detect_segments(
    *,
    active_col: Sequence[Any] | None,
    record_windows_ms: Mapping[int, tuple[int, int]],
    logger: Any | None = None,
) -> list[dict[str, Any]]:
    """Detect active runs from explicit non-overlapping record windows."""

    def _warning(msg: str) -> None:
        if logger is not None:
            logger.warning(msg=msg)

    height = len(record_windows_ms)
    if active_col is None or len(active_col) != height:
        raise ValueError("Workout is not v1: complete activity timeline is required")
    exact_segments: list[dict[str, Any]] = []
    current_indices: list[int] = []
    previous_endpoint_ms: int | None = None

    def _close_run() -> None:
        if not current_indices:
            return
        first, last = current_indices[0], current_indices[-1]
        exact_segments.append(
            {
                "start_idx": first,
                "end_idx": last,
                "record_indices": tuple(current_indices),
                "start_time_ms": record_windows_ms[first][0],
                "end_time_ms": record_windows_ms[last][1],
            }
        )
        current_indices.clear()

    for i, active_value in enumerate(active_col):
        if isinstance(active_value, bool):
            raise ValueError(f"Workout is not v1: invalid activity value at row {i}")
        try:
            numeric_active = float(active_value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Workout is not v1: invalid activity value at row {i}"
            ) from exc
        if not isfinite(numeric_active) or numeric_active not in (0.0, 1.0):
            raise ValueError(f"Workout is not v1: invalid activity value at row {i}")

        try:
            window_start_ms, window_end_ms = record_windows_ms[i]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Workout is not v1: invalid sample window at row {i}") from exc
        represented_ms = window_end_ms - window_start_ms
        if represented_ms < 0:
            raise ValueError(f"Workout is not v1: invalid sample window at row {i}")
        if represented_ms == 0:
            previous_endpoint_ms = window_end_ms
            continue
        if previous_endpoint_ms is not None and window_start_ms > previous_endpoint_ms:
            _close_run()
        if numeric_active == 0.0:
            _close_run()
        else:
            current_indices.append(i)
        previous_endpoint_ms = window_end_ms
    _close_run()
    if not exact_segments:
        _warning("No moving segments detected")
    return exact_segments


def load_program_steps(csv_path: str | Path) -> list[dict[str, Any]] | None:
    """Load the program step timeline saved alongside a workout CSV, if any.

    The workout session writes ``steps.json`` in the workout directory when a
    structured program was run. Every entry uses canonical session-elapsed
    seconds; epoch and raw-device timing are deliberately unsupported.
    """
    try:
        sidecar = WorkoutPaths.from_csv(csv_path).steps
        if not sidecar.exists():
            return None
        with open(sidecar, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or set(data) != {"version", "complete", "steps"}:
            raise ValueError("invalid step-sidecar schema")
        if data.get("version") != 1:
            raise ValueError("unsupported step-sidecar version")
        if not isinstance(data.get("complete"), bool):
            raise ValueError("planned-workout completion state is required")
        steps = data.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ValueError("planned workout requires at least one performed step")
        previous_end = 0.0
        for ordinal, step in enumerate(steps):
            bounds = program_step_bounds(step)
            if bounds is None or step.get("index") != ordinal:
                raise ValueError("planned workout contains an invalid performed step")
            start, end = bounds
            if start != previous_end:
                raise ValueError("planned workout steps must be consecutive")
            previous_end = end
        return steps
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"Invalid planned-workout timeline {sidecar.name}: {exc}"
        ) from exc


def program_step_bounds(step: Any) -> tuple[float, float] | None:
    """Return strict canonical session-elapsed bounds for one performed step."""
    required = {
        "index", "type", "label", "start_elapsed_s", "end_elapsed_s"
    }
    if not isinstance(step, dict) or set(step) != required:
        return None
    elapsed_start = step.get("start_elapsed_s")
    elapsed_end = step.get("end_elapsed_s")
    if (
        isinstance(step.get("index"), bool)
        or not isinstance(step.get("index"), int)
        or step["index"] < 0
        or not isinstance(step.get("type"), str)
        or not step["type"]
        or not isinstance(step.get("label"), str)
        or not step["label"]
        or isinstance(elapsed_start, bool)
        or isinstance(elapsed_end, bool)
        or not isinstance(elapsed_start, (int, float))
        or not isinstance(elapsed_end, (int, float))
    ):
        return None
    start_value, end_value = float(elapsed_start), float(elapsed_end)
    if (
        not isfinite(start_value)
        or not isfinite(end_value)
        or start_value < 0
        or end_value < start_value
    ):
        return None
    return start_value, end_value


def resolve_segments(
    *,
    record_windows_ms: Mapping[int, tuple[int, int]],
    active_col: Sequence[Any],
    logger: Any | None = None,
) -> list[dict[str, Any]]:
    """Resolve exact active-record runs independently of lap grouping."""
    return detect_segments(
        active_col=active_col,
        record_windows_ms=record_windows_ms,
        logger=logger,
    )


class LiveSegmenter:
    """Online free-workout pause/lap presenter fed one record at a time.

    Every activity-flag transition is reflected immediately, matching finalized
    free-workout laps and FIT timer boundaries.
    """

    def __init__(
        self, on_event: Callable[[dict[str, Any]], None] | None = None
    ) -> None:
        """Initialize the detector.

        Args:
            on_event: Optional callback invoked on every state transition with
                the same dict returned by :meth:`update`.
        """
        self.on_event = on_event
        self.reset()

    def reset(self) -> None:
        """Clear all state for a fresh workout."""
        self.paused = False
        self.lap = 0
        self._moving_started = False

    def _advance_lap(self) -> None:
        """Count the active segment just closed."""
        self.lap += 1

    def update(
        self,
        *,
        active: Any,
        gap_before_s: Any = 0,
    ) -> dict[str, Any]:
        """Feed one record and return the current state.

        ``active`` is the required ergometer "actively paddling" flag. Returns
        a dict ``{"paused", "lap", "transition"}``
        where ``transition`` is one of ``None``, ``"start"``, ``"pause"`` or
        ``"resume"``.
        """
        current_active = to_float(active)
        if isinstance(active, bool) or current_active not in (0.0, 1.0):
            raise ValueError("Live v1 workout requires an activity signal")
        transition: str | None = None

        gap = to_float(gap_before_s)
        if gap is None or gap < 0:
            raise ValueError("Live v1 workout requires a valid canonical gap")
        if gap > 0 and self._moving_started and not self.paused:
            self.paused = True
            transition = "pause"

        stationary = current_active <= 0
        if stationary:
            if not self.paused and self._moving_started:
                self.paused = True
                transition = "pause"
        else:
            if self.paused:
                self.paused = False
                self._advance_lap()
                transition = "resume"
            elif not self._moving_started:
                self._moving_started = True
                self.lap = 1
                transition = "start"

        result = {"paused": self.paused, "lap": self.lap, "transition": transition}
        if transition is not None and self.on_event is not None:
            self.on_event(result)
        return result
