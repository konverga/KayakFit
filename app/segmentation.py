"""Shared active-run and autopause detection.

A workout is split into *segments* (continuous moving periods) separated by
*stationary* periods. These exact runs own FIT timer events, but do not
necessarily become individual laps. ``app.finalized_workout`` separately
groups them into planned steps or natural free-workout autopause laps.

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
import logging
from collections.abc import Callable, Mapping, Sequence
from math import isfinite
from pathlib import Path
from typing import Any

from .coerce import to_float
from .workout_paths import WorkoutPaths

logger = logging.getLogger(__name__)

# Default number of consecutive stationary records before a pause is declared.
DEFAULT_STATIONARY_THRESHOLD_RECORDS = 5

# Default time gap (seconds) between consecutive records that is treated as a
# pause boundary on its own. Records are nominally one per second; a gap larger
# than this means recording effectively stopped (the athlete paused and the
# stationary samples were dropped, the device stalled, etc.). Without this,
# a lap's elapsed time can silently span such a gap and balloon to hours.
DEFAULT_MAX_GAP_SECONDS = 5

def detect_segments(
    distance_col: Sequence[float | None] | None,
    speed_col: Sequence[float | None] | None,
    timestamps_ms: Sequence[int],
    height: int,
    max_gap_seconds: float = DEFAULT_MAX_GAP_SECONDS,
    logger: Any | None = None,
    active_col: Sequence[float | None] | None = None,
) -> list[dict[str, int]]:
    """Detect exact active-paddling runs over a complete workout.

    Args:
        distance_col: Per-record cumulative distance (or None if unavailable).
        speed_col: Per-record instantaneous speed (or None if unavailable).
        timestamps_ms: Per-record timestamps in milliseconds.
        height: Number of records.
        active_col: Per-record "actively paddling" flag from the ergometer
            required for every row.
        max_gap_seconds: A time gap larger than this between consecutive records
            forces a segment boundary, even when both records look like movement.
            This catches pauses whose stationary samples were filtered out before
            export. Pass 0 to disable.
        logger: Optional logger for debug/warning parity with the old code.

    Returns:
        A list of ``{start_idx, end_idx, start_time_ms, end_time_ms}`` dicts,
        one per continuous moving/active run.
    """
    max_gap_ms = max(float(max_gap_seconds), 0.0) * 1000.0

    def _warning(msg: str) -> None:
        if logger is not None:
            logger.warning(msg=msg)

    if active_col is None or len(active_col) != height or len(timestamps_ms) != height:
        raise ValueError("Workout is not v1: complete activity timeline is required")
    exact_segments: list[dict[str, int]] = []
    start_idx: int | None = None
    for i, active_value in enumerate(active_col):
        if active_value is None:
            raise ValueError(f"Workout is not v1: invalid activity value at row {i}")
        try:
            active = float(active_value) > 0
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Workout is not v1: invalid activity value at row {i}"
            ) from exc

        gap_before = (
            i > 0
            and max_gap_ms > 0
            and timestamps_ms[i] - timestamps_ms[i - 1] > max_gap_ms
        )
        if start_idx is not None and (not active or gap_before):
            exact_segments.append(
                {
                    "start_idx": start_idx,
                    "end_idx": i - 1,
                    "start_time_ms": timestamps_ms[start_idx],
                    "end_time_ms": timestamps_ms[i - 1],
                }
            )
            start_idx = None
        if active and start_idx is None:
            start_idx = i
    if start_idx is not None:
        exact_segments.append(
            {
                "start_idx": start_idx,
                "end_idx": height - 1,
                "start_time_ms": timestamps_ms[start_idx],
                "end_time_ms": timestamps_ms[-1],
            }
        )
    if not exact_segments:
        _warning("No moving segments detected")
    return exact_segments


def load_program_steps(csv_path: str | Path) -> list[dict[str, Any]] | None:
    """Load the program step timeline saved alongside a workout CSV, if any.

    The workout session writes ``steps.json`` in the workout directory when a
    structured program was run; each entry has ``start_ms``/``end_ms`` (and the
    step's ``type``/``label``). Returns None when there is no (valid) sidecar,
    so callers fall back to pure movement-based segmentation.
    """
    try:
        sidecar = WorkoutPaths.from_csv(csv_path).steps
        if not sidecar.exists():
            return None
        with open(sidecar, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ValueError("unsupported step-sidecar version")
        if not isinstance(data.get("complete"), bool):
            raise ValueError("planned-workout completion state is required")
        steps = data.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ValueError("planned workout requires at least one performed step")
        if any(program_step_bounds(step) is None for step in steps):
            raise ValueError("planned workout contains an invalid performed step")
        return steps
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"Invalid planned-workout timeline {sidecar.name}: {exc}"
        ) from exc


def program_step_bounds(step: Any) -> tuple[float, float] | None:
    """Return finite ordered boundaries for a usable performed step."""
    if not isinstance(step, dict):
        return None
    start_ms, end_ms = step.get("start_ms"), step.get("end_ms")
    if (
        isinstance(start_ms, bool)
        or isinstance(end_ms, bool)
        or not isinstance(start_ms, (int, float))
        or not isinstance(end_ms, (int, float))
    ):
        return None
    start_value, end_value = float(start_ms), float(end_ms)
    if not isfinite(start_value) or not isfinite(end_value) or end_value < start_value:
        return None
    return start_value, end_value


def resolve_segments(
    *,
    timestamps_ms: Sequence[int],
    height: int,
    distance_col: Sequence[Any | None] | None,
    speed_col: Sequence[Any | None] | None = None,
    active_col: Sequence[Any | None] | None = None,
    config: Mapping[str, Any] | None = None,
    logger: Any | None = None,
) -> list[dict[str, Any]]:
    """Resolve exact active-record runs independently of lap grouping.

    ``active_paddling`` owns every v1 recording. Planned steps are reporting
    groups built later by :mod:`app.finalized_workout`.

    Args:
        timestamps_ms: Per-record timestamps in milliseconds.
        height: Number of records.
        distance_col: Per-record cumulative distance (or None if unavailable).
        speed_col: Per-record instantaneous speed (or None if unavailable).
        active_col: Required per-record "actively paddling" flag.
        config: Optional app configuration mapping for autopause tuning.
        logger: Optional logger passed through to the underlying detectors.

    Returns:
        Activity-run dicts with indices and timestamps.
    """
    cfg: Mapping[str, Any] = config if config is not None else {}
    max_gap_seconds = float(cfg.get("autopause_gap_seconds", DEFAULT_MAX_GAP_SECONDS))
    if active_col is None or any(value is None for value in active_col):
        raise ValueError("Workout is not v1: active_paddling is required for every row")
    return detect_segments(
        distance_col=distance_col,
        speed_col=speed_col,
        timestamps_ms=timestamps_ms,
        height=height,
        max_gap_seconds=max_gap_seconds,
        logger=logger,
        active_col=active_col,
    )


class LiveSegmenter:
    """Online free-workout autopause/lap presenter fed one record at a time.

    It debounces stationary records for a stable UI and natural lap grouping.
    The batch finalizer still uses every activity-flag transition for exact FIT
    timer boundaries and metric filtering, then applies the same threshold when
    grouping those runs into free-workout laps.
    """

    def __init__(
        self,
        stationary_threshold: int = DEFAULT_STATIONARY_THRESHOLD_RECORDS,
        max_gap_seconds: float = DEFAULT_MAX_GAP_SECONDS,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        """Initialize the detector.

        Args:
            stationary_threshold: Consecutive stationary records before pausing.
            max_gap_seconds: A gap larger than this between record timestamps is
                treated as a pause on its own (device stall / dropped samples).
                Pass 0 to disable.
            on_event: Optional callback invoked on every state transition with
                the same dict returned by :meth:`update`.
        """
        self.threshold = max(int(stationary_threshold), 1)
        self.max_gap_ms = max(float(max_gap_seconds), 0.0) * 1000.0
        self.on_event = on_event
        self.reset()

    def reset(self) -> None:
        """Clear all state for a fresh workout."""
        self.paused = False
        self.lap = 0
        self._stationary_count = 0
        self._last_distance: float | None = None
        self._last_ts_ms: float | None = None
        self._moving_started = False
        self._segment_start_distance: float | None = None

    def _advance_lap(self) -> None:
        """Count the active segment just closed."""
        self.lap += 1

    def _track_advance(self, distance: float | None) -> None:
        """Retain the current distance for status reporting."""
        self._segment_start_distance = distance

    def update(
        self,
        distance: Any = None,
        speed: Any = None,
        timestamp_ms: Any = None,
        active: Any = None,
    ) -> dict[str, Any]:
        """Feed one record and return the current state.

        ``active`` is the required ergometer "actively paddling" flag. Returns
        a dict ``{"paused", "lap", "transition"}``
        where ``transition`` is one of ``None``, ``"start"``, ``"pause"`` or
        ``"resume"``.
        """
        current_distance = to_float(distance)
        current_active = to_float(active)
        if current_active is None:
            raise ValueError("Live v1 workout requires an activity signal")
        current_ts = to_float(timestamp_ms)
        transition: str | None = None

        # A long gap since the previous record counts as a pause even if this
        # record shows movement, matching the batch detector.
        gapped = (
            self.max_gap_ms > 0
            and current_ts is not None
            and self._last_ts_ms is not None
            and (current_ts - self._last_ts_ms) > self.max_gap_ms
        )

        stationary = current_active <= 0
        # The explicit flag is exact for metrics and FIT timer events, but the
        # live pause/lap UI is a presentation layer. Debounce it just like free
        # workout lap grouping so a one-packet dropout neither flickers the UI
        # nor produces a misleading live lap number.
        required_stationary_records = self.threshold

        if not gapped and stationary:
            self._stationary_count += 1
            if (
                self._stationary_count >= required_stationary_records
                and not self.paused
                and self._moving_started
            ):
                self.paused = True
                transition = "pause"
        elif gapped and self._moving_started and not self.paused:
            # Treat the gap as a pause immediately followed by a resume so the
            # lap counter advances and the lap before the gap is closed.
            self._advance_lap()
            self._segment_start_distance = current_distance
            transition = "resume"
            self._stationary_count = 0
        else:
            if self.paused:
                self.paused = False
                self._advance_lap()
                self._segment_start_distance = current_distance
                transition = "resume"
            elif not self._moving_started:
                self._moving_started = True
                self.lap = 1
                self._segment_start_distance = current_distance
                transition = "start"
            self._stationary_count = 0

        if not self.paused:
            self._track_advance(current_distance)

        self._last_distance = current_distance
        if current_ts is not None:
            self._last_ts_ms = current_ts

        result = {"paused": self.paused, "lap": self.lap, "transition": transition}
        if transition is not None and self.on_event is not None:
            self.on_event(result)
        return result
