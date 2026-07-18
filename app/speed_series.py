"""Distance-derived workout speed shared by summary, FIT and the live UI.

Canonical record speed is simply active distance attributed to a represented
sample window divided by that window's active duration. Chart smoothing remains
presentation-only; it does not rewrite FIT measurements or define maxima.
"""

from __future__ import annotations

from collections.abc import Sequence

from .active_distance import ActiveDistanceAccumulator
from .finalized_workout import FinalizedWorkout

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


def distance_derived_speed_mps(
    finalized: FinalizedWorkout,
) -> dict[int, float | None]:
    """Canonical per-record speed in m/s, keyed by active record index."""
    result: dict[int, float | None] = {}
    for index in finalized.moving_indices:
        distance_m = finalized.record_distance_m.get(index)
        duration_s = finalized.record_duration_s.get(index, 0.0)
        result[index] = (
            max(distance_m, 0.0) / duration_s
            if distance_m is not None and duration_s > 0
            else None
        )
    return result


def peak_speed_mps(
    speed_by_index: dict[int, float | None],
    indices: Sequence[int],
) -> float | None:
    """Largest raw canonical record speed over ``indices``."""
    present = [
        speed_by_index[i]
        for i in indices
        if speed_by_index.get(i) is not None
    ]
    return max(present) if present else None  # type: ignore[type-var]


class LiveSpeedTracker:
    """Live view of the same active-distance accumulator used by finalization."""

    def __init__(self) -> None:
        self._distance = ActiveDistanceAccumulator()
        self.max_speed_mps: float | None = None

    @property
    def active_distance_m(self) -> float:
        return self._distance.active_distance_m

    @property
    def active_time_s(self) -> float:
        return self._distance.active_time_s

    @property
    def avg_speed_mps(self) -> float | None:
        """Active distance / active time -- the finalized average definition."""
        if self.active_time_s > 0:
            return self.active_distance_m / self.active_time_s
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
        update = self._distance.update(
            distance_m=distance_m,
            duration_s=duration_s,
            active=active,
            reported_speed_mps=reported_speed_mps,
        )
        speed_mps = update.speed_mps if update is not None else None
        if speed_mps is not None and (
            self.max_speed_mps is None or speed_mps > self.max_speed_mps
        ):
            self.max_speed_mps = speed_mps
