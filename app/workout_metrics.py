"""Final session and lap metrics shared by the summary and FIT exporter.

The finalized workout owns the activity domain (records, timer windows, laps
and distance).  This module is the corresponding aggregate boundary: it turns
that domain into one immutable set of session/lap statistics which every
consumer uses verbatim.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .finalized_workout import FinalizedLap, FinalizedWorkout
from .power import DEFAULT_PULL_LENGTH_M, normalize_pull_length
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
    pull_length_m: float,
) -> SliceMetrics:
    raw = aggregate_metrics(
        heart_rate=_values(table, "heart_rate", indices),
        cadence=_values(table, "cadence_instant", indices),
        power_cadence=_values(table, "cadence_instant", indices),
        power_pull_force=_values(table, "pull_force_instant", indices),
        speed=_values(table, "speed_instant", indices),
        sample_weights=weights_s,
        pull_length_m=pull_length_m,
    )
    return SliceMetrics(
        avg_heart_rate=raw["avg_heart_rate"],
        max_heart_rate=raw["max_heart_rate"],
        avg_cadence=raw["avg_cadence"],
        max_cadence=raw["max_cadence"],
        avg_power=raw["avg_power"],
        max_power=raw["max_power"],
        avg_speed=raw["avg_speed"],
        max_speed=raw["max_speed"],
    )


def _lap_metrics(
    table: Table,
    lap: FinalizedLap,
    pull_length_m: float,
) -> SliceMetrics:
    return _slice_metrics(
        table,
        tuple(lap.record_indices),
        tuple(lap.record_weights_s),
        pull_length_m,
    )


def compute_finalized_metrics(
    table: Table,
    finalized: FinalizedWorkout,
    pull_length_m: Any = DEFAULT_PULL_LENGTH_M,
) -> FinalizedMetrics:
    """Compute the single aggregate result used by summary and FIT output.

    Average speed is the represented-time-weighted mean of active
    ``speed_instant`` samples, cadence uses ``cadence_instant``, and power is
    estimated from ``cadence_instant`` and ``pull_force_instant``. Distance is
    aggregated independently from the cumulative odometer.
    """
    length = normalize_pull_length(pull_length_m)
    laps = tuple(
        _lap_metrics(table, lap, length) for lap in finalized.laps
    )
    moving_indices = tuple(finalized.moving_indices)
    session = _slice_metrics(
        table,
        moving_indices,
        tuple(finalized.record_duration_s[index] for index in moving_indices),
        length,
    )
    return FinalizedMetrics(session=session, laps=laps)
