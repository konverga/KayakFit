"""Shared workout-metric aggregation.

The single, correct definition of how a slice of workout samples becomes the
averaged/peak numbers shown to the athlete and written into the FIT file.
``app.workout_metrics.compute_finalized_metrics`` supplies both consumers, so
they cannot silently disagree. This follows the same
"one source of truth" discipline ``app.segmentation`` provides for lap
boundaries.

Heart-rate zone rules live here too: :func:`zone_lower_bounds` (the single
definition of the five Z1..Z5 bpm lower bounds, manual or derived from max HR)
and :func:`time_in_hr_zones` (time-in-zone accumulated **only inside moving
segments**). The FIT file only contains records inside moving segments —
pauses sit between STOP_ALL/START timer events — so any platform reading it
(Garmin Connect, Strava) derives zone time from moving time only. The in-app
summary must use the same rule or its zone bar won't add up to the moving time
it displays. Missing-HR moving time remains explicit uncovered time in the UI.

Two rules matter and were previously implemented inconsistently:

1. **Heart rate, cadence and power average/peak over strictly-positive
   readings only.** A ``0`` is a dropped sensor sample (no signal), not a real
   physiological zero; including it drags every average toward zero. (The FIT
   exporter used to skip only ``None``, so a run of 0-bpm dropouts deflated its
   averages relative to the summary screen.)
2. **Average and maximum speed come from the recorded instantaneous speed
   samples.** Average speed is weighted by each active represented interval;
   maximum speed is the largest active sample. Cumulative distance remains an
   independent recorded measurement and is never used to manufacture a speed
   spike.

This module lives apart from ``app.table`` (whose ``column_mean``/``column_max``
are deliberately generic, None-skipping aggregates) and ``app.segmentation``
(which is about laps, not stats): this is domain-specific workout-metric logic.
It returns plain floats/ints; each caller coerces to its own field types
(``Decimal``/``int`` for FIT's typed fields, ``float``/``int`` for the GUI).
"""

from collections.abc import Sequence
from math import isfinite
from typing import Any

from .finalized_workout import ActivitySegment
from .power import DEFAULT_PULL_LENGTH_M, estimate_power

# Fractions of max HR used to derive the five zone lower bounds when explicit
# bpm boundaries are not configured. Index 0 is Z1 (recovery) ... 4 is Z5 (max).
ZONE_FRACTIONS = (0.0, 0.60, 0.70, 0.80, 0.90)


def zone_lower_bounds(
    max_hr: Any = 185, zones: list[int] | None = None
) -> list[int]:
    """Return the five ascending zone lower bounds (bpm), Z1..Z5.

    Uses explicit ``zones`` (a list of five ascending bpm values) when provided
    and valid; otherwise derives them from ``max_hr`` via ``ZONE_FRACTIONS``.
    """
    if zones:
        cleaned: list[int] = []
        try:
            cleaned = [int(z) for z in zones]
        except (TypeError, ValueError):
            cleaned = []
        # Require five strictly ascending values to trust a manual config.
        if len(cleaned) == 5 and all(
            cleaned[i] < cleaned[i + 1] for i in range(4)
        ):
            return cleaned
    try:
        mhr = float(max_hr)
    except (TypeError, ValueError):
        mhr = 185.0
    return [round(frac * mhr) for frac in ZONE_FRACTIONS]


def time_in_hr_zones(
    *,
    heart_rate: Sequence[Any],
    segments: Sequence[ActivitySegment],
    bounds: Sequence[int],
    sample_weights: Sequence[Any],
) -> list[float]:
    """Accumulate time in each HR zone over the moving segments only.

    Each active record contributes its explicit represented sample window to
    the zone of its heart-rate reading.

    Args:
        heart_rate: Per-record heart-rate readings; None/0/unparseable values
            are treated as no signal and credited to no zone.
        segments: Finalized active segments shared with FIT timer events.
        bounds: Ascending zone lower bounds (bpm), e.g. from
            :func:`zone_lower_bounds`.
        sample_weights: Represented seconds per record.

    Returns:
        Seconds per zone, one entry per bound (Z1..Zn).
    """
    zone_seconds = [0.0] * len(bounds)
    last = len(heart_rate) - 1
    for seg in segments:
        for i in seg.record_indices:
            if i < 0 or i > last:
                continue
            hr_raw = heart_rate[i]
            if hr_raw is None:
                continue
            try:
                hr = float(hr_raw)
            except (TypeError, ValueError):
                continue
            if hr <= 0:
                continue
            try:
                dt = float(sample_weights[i])
            except (IndexError, TypeError, ValueError):
                continue
            if dt <= 0:
                continue
            zone = 0
            for z, lower in enumerate(bounds):
                if hr >= lower:
                    zone = z
            zone_seconds[zone] += dt
    return zone_seconds


def _positive_floats(
    values: Sequence[Any] | None, maximum: float | None = None
) -> list[float]:
    """Coerce to float and keep only strictly-positive, non-None readings."""
    out: list[float] = []
    if not values:
        return out
    for v in values:
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f > 0 and (maximum is None or f <= maximum):
            out.append(f)
    return out


def _weighted_positive_mean(
    values: Sequence[Any] | None,
    weights: Sequence[Any],
    maximum: float | None = None,
) -> float | None:
    """Mean of positive readings, weighted by their represented seconds."""
    weighted_sum = 0.0
    total_weight = 0.0
    for value, weight in zip(values or (), weights, strict=False):
        try:
            reading = float(value)
            seconds = float(weight)
        except (TypeError, ValueError):
            continue
        if reading > 0 and seconds > 0 and (maximum is None or reading <= maximum):
            weighted_sum += reading * seconds
            total_weight += seconds
    return weighted_sum / total_weight if total_weight > 0 else None


def positive_power_values(
    power_pull_force: Sequence[Any] | None,
    power_cadence: Sequence[Any] | None,
    pull_length_m: Any = DEFAULT_PULL_LENGTH_M,
) -> list[float]:
    """Per-record power estimates (W), keeping only strictly-positive ones."""
    if power_pull_force is None or power_cadence is None:
        return []
    powers: list[float] = []
    for pf, cd in zip(power_pull_force, power_cadence, strict=False):
        p = estimate_power(pf, cd, pull_length_m)
        if p is not None and 0 < p <= 65535:
            powers.append(float(p))
    return powers


def aggregate_metrics(
    *,
    heart_rate: Sequence[Any] | None = None,
    cadence: Sequence[Any] | None = None,
    power_cadence: Sequence[Any] | None = None,
    power_pull_force: Sequence[Any] | None = None,
    speed: Sequence[Any] | None = None,
    sample_weights: Sequence[Any],
    pull_length_m: Any = DEFAULT_PULL_LENGTH_M,
) -> dict[str, float | None]:
    """Compute averaged/peak workout metrics with the shared rules.

    Args:
        heart_rate: per-record heart-rate readings (any may be missing/None;
            0 readings are treated as no signal).
        cadence: per-record instantaneous cadence readings (same missing/zero
            handling).
        power_cadence: per-record stroke-averaged cadence used only for power.
        power_pull_force: per-record stroke-averaged pull force used only for
            power.
        speed: per-record instantaneous speed readings. Average speed is
            time-weighted; maximum speed is the largest valid reading.
        sample_weights: Represented seconds per sensor sample. HR, cadence,
            power, and speed averages are time-weighted.
        pull_length_m: Effective handle pull length used for power estimation.

    Returns:
        Dict of plain floats (or None) with keys ``avg_heart_rate``,
        ``max_heart_rate``, ``avg_cadence``, ``max_cadence``, ``avg_power``,
        ``max_power``, ``avg_speed``, ``max_speed``. Averages/peaks are over
        strictly-positive readings. Speed zero is retained as a real active
        reading so a slow start is represented honestly. Callers coerce types.
    """
    result: dict[str, float | None] = {
        "avg_heart_rate": None,
        "max_heart_rate": None,
        "avg_cadence": None,
        "max_cadence": None,
        "avg_power": None,
        "max_power": None,
        "avg_speed": None,
        "max_speed": None,
    }

    hr = _positive_floats(heart_rate, 255)
    if hr:
        result["avg_heart_rate"] = _weighted_positive_mean(
            heart_rate, sample_weights, 255
        )
        result["max_heart_rate"] = max(hr)

    cad = _positive_floats(cadence, 255)
    if cad:
        result["avg_cadence"] = _weighted_positive_mean(cadence, sample_weights, 255)
        result["max_cadence"] = max(cad)

    powers = positive_power_values(
        power_pull_force, power_cadence, pull_length_m
    )
    if powers:
        aligned_power = [
            estimate_power(force, rate, pull_length_m)
            for force, rate in zip(
                power_pull_force or (), power_cadence or (), strict=False
            )
        ]
        result["avg_power"] = _weighted_positive_mean(
            aligned_power, sample_weights, 65535
        )
        result["max_power"] = max(powers)

    speed_values: list[float] = []
    if speed is not None:
        for value in speed:
            try:
                reading = float(value)
            except (TypeError, ValueError):
                continue
            if 0 <= reading <= 65.535:
                speed_values.append(reading)
    if speed_values:
        weighted_sum = 0.0
        total_weight = 0.0
        for value, weight in zip(speed or (), sample_weights, strict=False):
            try:
                reading = float(value)
                seconds = float(weight)
            except (TypeError, ValueError):
                continue
            if 0 <= reading <= 65.535 and isfinite(seconds) and seconds > 0:
                weighted_sum += reading * seconds
                total_weight += seconds
        if total_weight > 0:
            result["avg_speed"] = weighted_sum / total_weight
        result["max_speed"] = max(speed_values)

    return result
