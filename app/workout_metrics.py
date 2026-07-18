"""Final session and lap metrics shared by the summary and FIT exporter.

The finalized workout owns the activity domain (records, timer windows, laps
and distance).  This module is the corresponding aggregate boundary: it turns
that domain into one immutable set of session/lap statistics which every
consumer uses verbatim.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from .finalized_workout import FinalizedLap, FinalizedWorkout
from .power import DEFAULT_PULL_LENGTH_M, normalize_pull_length
from .speed_series import distance_derived_speed_mps, peak_speed_mps
from .stats import aggregate_metrics
from .table import Table


@dataclass(frozen=True)
class SliceMetrics:
    """Authoritative aggregates for one session or lap."""

    avg_heart_rate: float | None
    max_heart_rate: float | None
    avg_cadence: float | None
    max_cadence: float | None
    avg_power: float | None
    max_power: float | None
    avg_speed: float | None
    max_speed: float | None


@dataclass(frozen=True)
class FinalizedMetrics:
    """One session aggregate and its positionally matching lap aggregates."""

    session: SliceMetrics
    laps: tuple[SliceMetrics, ...]


def _values(table: Table, column: str, indices: tuple[int, ...]) -> list[Any]:
    values = table.get_column(column)
    return [values[index] for index in indices]


def _slice_metrics(
    table: Table,
    indices: tuple[int, ...],
    weights_s: tuple[float, ...],
    distance_m: float,
    duration_s: float,
    pull_length_m: float,
    speed_peak_mps: float | None,
) -> SliceMetrics:
    raw = aggregate_metrics(
        heart_rate=_values(table, "heart_rate", indices),
        cadence=_values(table, "cadence", indices),
        pull_force=_values(table, "pull_force", indices),
        sample_weights=weights_s,
        distance_m=distance_m,
        duration_s=duration_s,
        pull_length_m=pull_length_m,
    )
    avg_speed = raw["avg_speed"]
    max_speed = speed_peak_mps
    # FIT lap distance is encoded to centimetres. Splitting one record at a
    # planned boundary can move that final centimetre to either adjacent lap,
    # making its encoded distance/time average microscopically exceed the raw
    # record peak. Normalize only that representation edge.
    if avg_speed is not None and (max_speed is None or max_speed < avg_speed):
        max_speed = avg_speed
    return SliceMetrics(
        avg_heart_rate=raw["avg_heart_rate"],
        max_heart_rate=raw["max_heart_rate"],
        avg_cadence=raw["avg_cadence"],
        max_cadence=raw["max_cadence"],
        avg_power=raw["avg_power"],
        max_power=raw["max_power"],
        avg_speed=avg_speed,
        max_speed=max_speed,
    )


def _lap_metrics(
    table: Table,
    lap: FinalizedLap,
    pull_length_m: float,
    smoothed_speed: dict[int, float | None],
) -> SliceMetrics:
    return _slice_metrics(
        table,
        tuple(lap.record_indices),
        tuple(lap.record_weights_s),
        lap.distance_m,
        lap.timer_time_ms / 1000.0,
        pull_length_m,
        peak_speed_mps(smoothed_speed, lap.record_indices),
    )


def compute_finalized_metrics(
    table: Table,
    finalized: FinalizedWorkout,
    pull_length_m: Any = DEFAULT_PULL_LENGTH_M,
) -> FinalizedMetrics:
    """Compute the single aggregate result used by summary and FIT output.

    Average speed is distance / active time. Maximum speed is the largest raw
    per-record distance / active-window-duration value. Because lap/session
    distance is built from those same record distances, every positive average
    is a duration-weighted mean of the record speeds. The only normalization is
    at planned-lap centimetre allocation, where encoded distance quantization
    can put a lap average a fraction above its raw record peak.
    """
    length = normalize_pull_length(pull_length_m)
    record_speed = distance_derived_speed_mps(finalized)
    laps = tuple(
        _lap_metrics(table, lap, length, record_speed) for lap in finalized.laps
    )
    moving_indices = tuple(finalized.moving_indices)
    session = _slice_metrics(
        table,
        moving_indices,
        tuple(finalized.record_duration_s[index] for index in moving_indices),
        finalized.total_distance_m,
        finalized.total_timer_time_ms / 1000.0,
        length,
        peak_speed_mps(record_speed, moving_indices),
    )
    lap_speed_peaks = [lap.max_speed for lap in laps if lap.max_speed is not None]
    if lap_speed_peaks and (
        session.max_speed is None or max(lap_speed_peaks) > session.max_speed
    ):
        session = replace(session, max_speed=max(lap_speed_peaks))
    return FinalizedMetrics(session=session, laps=laps)
