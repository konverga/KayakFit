"""Shared active-time and active-distance accumulation.

The ergometer publishes cumulative distance for every packet, including idle
packets. This accumulator attributes distance only to active sample windows and
is used by both batch finalization and the live dashboard preview.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any


def _finite_nonnegative(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) and number >= 0 else None


@dataclass(frozen=True)
class ActiveDistanceUpdate:
    """Result of closing one measurable active-distance span."""

    distance_m: float
    duration_s: float
    cumulative_distance_m: float

    @property
    def speed_mps(self) -> float | None:
        return self.distance_m / self.duration_s if self.duration_s > 0 else None


class ActiveDistanceAccumulator:
    """Accumulate active distance/time from cumulative-distance packets."""

    def __init__(self) -> None:
        self._previous_distance_m = 0.0
        self._previous_sample_active = False
        self._seen_sample = False
        self._resume_pending = False
        self._pending_duration_s = 0.0
        self.active_distance_m = 0.0
        self.active_time_s = 0.0

    def update(
        self,
        *,
        distance_m: Any,
        duration_s: Any,
        active: Any,
        reported_speed_mps: Any = None,
    ) -> ActiveDistanceUpdate | None:
        """Fold one packet into the active domain.

        A missing active odometer reading defers its represented duration to the
        next valid reading. An idle packet advances the raw-distance baseline
        without crediting its distance or duration to the activity.
        """
        try:
            is_active = float(active) > 0
        except (TypeError, ValueError):
            is_active = False
        represented_s = _finite_nonnegative(duration_s) or 0.0
        cumulative_m = _finite_nonnegative(distance_m)

        if not is_active:
            self._pending_duration_s = 0.0
            self._resume_pending = False
            if cumulative_m is not None:
                self._previous_distance_m = cumulative_m
            self._previous_sample_active = False
            self._seen_sample = True
            return None

        if self._seen_sample and not self._previous_sample_active:
            self._resume_pending = True
        self._seen_sample = True
        self._previous_sample_active = True
        self.active_time_s += represented_s
        self._pending_duration_s += represented_s

        if cumulative_m is None:
            return None

        delta_m = max(cumulative_m - self._previous_distance_m, 0.0)
        if self._resume_pending and self._pending_duration_s > 0:
            reported_mps = _finite_nonnegative(reported_speed_mps) or 0.0
            delta_m = min(delta_m, reported_mps * self._pending_duration_s)

        span_duration_s = self._pending_duration_s
        self._pending_duration_s = 0.0
        self._resume_pending = False
        self._previous_distance_m = cumulative_m
        self.active_distance_m += delta_m
        return ActiveDistanceUpdate(
            distance_m=delta_m,
            duration_s=span_duration_s,
            cumulative_distance_m=self.active_distance_m,
        )
