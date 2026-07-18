"""Shared workout-metric aggregation.

The single, correct definition of how a slice of workout samples becomes the
averaged/peak numbers shown to the athlete and written into the FIT file. Both
consumers -- ``app.summary.compute_summary`` (in-app summary screen) and
``app.export_fit.FitExporter._calculate_stats`` (the FIT session/lap messages
Strava reads) -- go through here so they can never silently disagree, the same
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
2. **Average speed is distance-covered / elapsed-moving-time**, the physically
   correct definition, NOT a mean of the noisy per-record reported speed
   samples (which is skewed by sampling irregularity and ignores elapsed time).
   Peak speed is the largest positive sample supplied by compatibility callers;
   finalized workout metrics derive canonical record speed separately.

This module lives apart from ``app.table`` (whose ``column_mean``/``column_max``
are deliberately generic, None-skipping aggregates) and ``app.segmentation``
(which is about laps, not stats): this is domain-specific workout-metric logic.
It returns plain floats/ints; each caller coerces to its own field types
(``Decimal``/``int`` for FIT's typed fields, ``float``/``int`` for the GUI).
"""

from collections.abc import Mapping, Sequence
from typing import Any

from .power import DEFAULT_PULL_LENGTH_M, estimate_power

# Fractions of max HR used to derive the five zone lower bounds when explicit
# bpm boundaries are not configured. Index 0 is Z1 (recovery) ... 4 is Z5 (max).
ZONE_FRACTIONS = (0.0, 0.60, 0.70, 0.80, 0.90)


def cumulative_segment_distance(
    distance: Sequence[Any], start_idx: int, end_idx: int
) -> float | None:
    """Return FIT-compatible distance covered by an inclusive segment.

    Device distance is cumulative from workout start. A resumed segment owns
    the advance since the record immediately before it; the first segment owns
    the cumulative distance already present on its final record.
    """
    try:
        end = float(distance[end_idx])
        before = float(distance[start_idx - 1]) if start_idx > 0 else 0.0
    except (IndexError, TypeError, ValueError):
        return None
    return end - before


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
    segments: Sequence[Mapping[str, Any]],
    bounds: Sequence[int],
    sample_weights: Sequence[Any],
) -> list[float]:
    """Accumulate time in each HR zone over the moving segments only.

    Each active record contributes its explicit represented sample window to
    the zone of its heart-rate reading.

    Args:
        heart_rate: Per-record heart-rate readings; None/0/unparseable values
            are treated as no signal and credited to no zone.
        segments: Moving segments (dicts with ``start_idx``/``end_idx``,
            inclusive) from ``app.segmentation`` — the same list used for laps.
        bounds: Ascending zone lower bounds (bpm), e.g. from
            :func:`zone_lower_bounds`.
        sample_weights: Represented seconds per record.

    Returns:
        Seconds per zone, one entry per bound (Z1..Zn).
    """
    zone_seconds = [0.0] * len(bounds)
    last = len(heart_rate) - 1
    for seg in segments:
        start = max(int(seg["start_idx"]), 0)
        end = min(int(seg["end_idx"]), last)
        for i in range(start, end + 1):
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
    weights: Sequence[Any] | None,
    maximum: float | None = None,
) -> float | None:
    """Mean of positive readings, weighted by their represented seconds."""
    if weights is None:
        positive = _positive_floats(values, maximum)
        return sum(positive) / len(positive) if positive else None
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
    pull_force: Sequence[Any] | None,
    cadence: Sequence[Any] | None,
    pull_length_m: Any = DEFAULT_PULL_LENGTH_M,
) -> list[float]:
    """Per-record power estimates (W), keeping only strictly-positive ones."""
    if pull_force is None or cadence is None:
        return []
    powers: list[float] = []
    for pf, cd in zip(pull_force, cadence, strict=False):
        p = estimate_power(pf, cd, pull_length_m)
        if p is not None and 0 < p <= 65535:
            powers.append(float(p))
    return powers


def aggregate_metrics(
    *,
    heart_rate: Sequence[Any] | None = None,
    cadence: Sequence[Any] | None = None,
    pull_force: Sequence[Any] | None = None,
    speed: Sequence[Any] | None = None,
    distance_m: float | None = None,
    duration_s: float | None = None,
    sample_weights: Sequence[Any] | None = None,
    pull_length_m: Any = DEFAULT_PULL_LENGTH_M,
) -> dict[str, float | None]:
    """Compute averaged/peak workout metrics with the shared rules.

    Args:
        heart_rate: per-record heart-rate readings (any may be missing/None;
            0 readings are treated as no signal).
        cadence: per-record cadence readings (same missing/zero handling).
        pull_force: per-record pull-force readings (same missing/zero handling).
        speed: per-record readings used for peak speed by compatibility callers
            (same missing/zero handling). Finalized workout metrics derive speed
            from active record distance and duration instead.
        distance_m: distance covered over the slice (metres) -- the numerator
            for average speed.
        duration_s: elapsed moving time over the slice (seconds) -- the
            denominator for average speed.
        sample_weights: optional represented seconds per sensor sample. When
            present, HR, cadence and power averages are time-weighted.
        pull_length_m: Effective handle pull length used for power estimation.

    Returns:
        Dict of plain floats (or None) with keys ``avg_heart_rate``,
        ``max_heart_rate``, ``avg_cadence``, ``max_cadence``, ``avg_power``,
        ``max_power``, ``avg_speed``, ``max_speed``. Averages/peaks are over
        strictly-positive readings; ``avg_speed`` is ``distance_m / duration_s``
        (None unless duration is positive and distance is non-negative). Callers
        coerce types.
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

    powers = positive_power_values(pull_force, cadence, pull_length_m)
    if powers:
        if sample_weights is None:
            result["avg_power"] = sum(powers) / len(powers)
        else:
            aligned_power = [
                estimate_power(force, rate, pull_length_m)
                for force, rate in zip(pull_force or (), cadence or (), strict=False)
            ]
            result["avg_power"] = _weighted_positive_mean(
                aligned_power, sample_weights, 65535
            )
        result["max_power"] = max(powers)

    spd = _positive_floats(speed, 65.535)
    if spd:
        result["max_speed"] = max(spd)

    if (
        distance_m is not None
        and duration_s is not None
        and distance_m >= 0
        and duration_s > 0
    ):
        result["avg_speed"] = distance_m / duration_s

    return result
