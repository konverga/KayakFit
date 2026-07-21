"""Recorded workout speed shared by summary, FIT and the live UI.

Instantaneous ergometer speed is the canonical finalized speed channel.
"""

from __future__ import annotations

from math import isfinite
from typing import Any

from .active_distance import ActiveDistanceAccumulator
from .finalized_workout import FinalizedWorkout
from .table import Table


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
        distance_m: Any,
        duration_s: Any,
        active: Any,
        reported_speed_mps: Any = None,
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
