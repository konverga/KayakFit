"""Recorded workout speed shared by summary, FIT and the live UI.

Instantaneous ergometer speed is the canonical speed channel. Chart smoothing
remains presentation-only; it does not rewrite FIT measurements or statistics.
"""

from __future__ import annotations

from collections.abc import Sequence
from math import isfinite

from .active_distance import ActiveDistanceAccumulator
from .finalized_workout import FinalizedWorkout
from .table import Table

# Centered moving-average width used only for chart presentation.
SPEED_SMOOTHING_WINDOW = 5


def smooth(
    values: Sequence[float | None],
    breaks: Sequence[bool] | None = None,
    window: int = SPEED_SMOOTHING_WINDOW,
) -> list[float | None]:
    """Centered moving average that never crosses missing data or a pause.

    ``breaks[i]`` marks an index where a new segment starts (a lap boundary or a
    meaningful pause); the averaging window is not allowed to span such a break,
    so each segment is smoothed independently.
    """
    if window <= 1:
        return list(values)
    half = window // 2
    out: list[float | None] = []
    for i, v in enumerate(values):
        if v is None:
            out.append(None)
            continue
        left = max(0, i - half)
        right = min(len(values), i + half + 1)
        if breaks is not None:
            for j in range(i, left - 1, -1):
                if j < len(breaks) and breaks[j]:
                    left = j
                    break
            for j in range(i + 1, right):
                if j < len(breaks) and breaks[j]:
                    right = j
                    break
        acc: list[float] = []
        for j in range(left, right):
            if values[j] is None:
                if j <= i:  # gap before/at center: restart the window
                    acc = []
                else:  # gap after center: stop extending
                    break
            else:
                acc.append(values[j])  # type: ignore[arg-type]
        out.append(sum(acc) / len(acc) if acc else v)
    return out


def recorded_speed_mps(
    table: Table, finalized: FinalizedWorkout
) -> dict[int, float | None]:
    """Return valid instantaneous speed for every active record index."""
    values = table.get_column("speed_instant")
    result: dict[int, float | None] = {}
    for index in finalized.moving_indices:
        try:
            speed = float(values[index])
        except (IndexError, TypeError, ValueError):
            result[index] = None
            continue
        result[index] = speed if 0 <= speed <= 65.535 else None
    return result


class LiveSpeedTracker:
    """Live view of the same active-distance accumulator used by finalization."""

    def __init__(self) -> None:
        self._distance = ActiveDistanceAccumulator()
        self._speed_weighted_sum = 0.0
        self._speed_weight_s = 0.0
        self.max_speed_mps: float | None = None

    @property
    def active_distance_m(self) -> float:
        return self._distance.active_distance_m

    @property
    def active_time_s(self) -> float:
        return self._distance.active_time_s

    @property
    def avg_speed_mps(self) -> float | None:
        """Device-time-weighted instantaneous speed over active intervals."""
        if self._speed_weight_s > 0:
            return self._speed_weighted_sum / self._speed_weight_s
        return None

    def update(
        self,
        *,
        distance_m: object,
        duration_s: object,
        active: object,
        reported_speed_mps: object = None,
    ) -> None:
        """Fold one live sample into active distance, time, average and maximum."""
        self._distance.update(
            distance_m=distance_m,
            duration_s=duration_s,
            active=active,
        )
        try:
            active_now = float(active) > 0
            duration = float(duration_s)
            speed_mps = float(reported_speed_mps)
        except (TypeError, ValueError):
            return
        if (
            not active_now
            or not isfinite(duration)
            or duration <= 0
            or not 0 <= speed_mps <= 65.535
        ):
            return
        self._speed_weighted_sum += speed_mps * duration
        self._speed_weight_s += duration
        if (
            self.max_speed_mps is None or speed_mps > self.max_speed_mps
        ):
            self.max_speed_mps = speed_mps
