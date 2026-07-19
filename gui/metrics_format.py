"""Metric formatting helpers for the live dashboard.

Pure functions with no GUI dependencies, so they can be unit-tested in isolation
and reused by any front end.
"""

from typing import Any

from app import stats
from app.power import estimate_power


def fmt_time(seconds: Any) -> str:
    """Format seconds as m:ss (or h:mm:ss past an hour)."""
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return "—"
    hours, rem = divmod(max(total, 0), 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def fmt_time_precise(seconds: Any) -> str:
    """Format duration exactly to the nearest recorded millisecond."""
    try:
        total_ms = max(round(float(seconds) * 1000), 0)
    except (TypeError, ValueError):
        return "—"
    if total_ms % 1000 == 0:
        return fmt_time(total_ms // 1000)
    hours, remainder_ms = divmod(total_ms, 3_600_000)
    minutes, remainder_ms = divmod(remainder_ms, 60_000)
    seconds_text = f"{remainder_ms / 1000:06.3f}".rstrip("0").rstrip(".")
    return (
        f"{hours}:{minutes:02d}:{seconds_text}"
        if hours
        else f"{minutes:02d}:{seconds_text}"
    )


def fmt_metres(distance_m: float) -> str:
    """Format finalized centimetre precision without inventing split values."""
    if abs(distance_m - round(distance_m)) < 0.005:
        return f"{round(distance_m):,d} m"
    return f"{distance_m:,.2f} m"


def hr_zone(hr: Any, max_hr: Any = 185, zones: list[int] | None = None) -> int:
    """Classify a heart rate into a training zone 0-4.

    When ``zones`` (five ascending bpm lower bounds) is given it is used directly;
    otherwise the bounds are derived from ``max_hr`` (<60% Z1 ... >=90% Z5).
    Returns -1 when the heart rate is missing or invalid.
    """
    try:
        value = float(hr)
    except (TypeError, ValueError):
        return -1
    bounds = stats.zone_lower_bounds(max_hr=max_hr, zones=zones)
    zone = 0
    for i, lower in enumerate(bounds):
        if value >= lower:
            zone = i
    return zone


def zone_range(
    zone: int, max_hr: Any = 185, zones: list[int] | None = None
) -> tuple[int | None, int | None]:
    """Return the (min_bpm, max_bpm) range for a zone (max is None for Z5)."""
    if not 0 <= zone <= 4:
        return (None, None)
    bounds = stats.zone_lower_bounds(max_hr=max_hr, zones=zones)
    low = bounds[zone]
    high = bounds[zone + 1] - 1 if zone < 4 else None
    return (low, high)


def format_pace(data: dict[str, Any], distance_m: int) -> str | None:
    """Format the ergometer's averaged pace for a competition distance.

    Returns an ``m:ss`` string, ``"—"`` when out of range, or None if the field
    is absent.
    """
    field = f"pace_{int(distance_m)}m__s"
    value = data.get(field)
    if value is None:
        return None
    try:
        secs = int(value)
    except (TypeError, ValueError):
        return None
    return fmt_time(secs) if 0 < secs < 3600 else "—"


def compute_power(
    data: dict[str, Any], pull_length_m: Any = 0.623
) -> int | None:
    """Estimate power from the calibrated averaged force/cadence channels."""
    return estimate_power(
        data.get("pull_force__n"), data.get("cadence__spm"), pull_length_m
    )


def metric_value(
    key: str,
    data: dict[str, Any],
    pull_length_m: Any = 0.623,
) -> float | None:
    """Return one main-window value using averaged ergometer channels."""
    field_by_key = {
        "speed": "speed__mps",
        "stroke": "cadence__spm",
        "pull": "pull_force__n",
        "hr": "heart_rate__bpm",
    }
    if key == "power":
        power = compute_power(data, pull_length_m)
        return float(power) if power is not None else None
    field = field_by_key.get(key)
    if field is None or data.get(field) is None:
        return None
    try:
        value = float(data[field])
    except (TypeError, ValueError):
        return None
    return value * 3.6 if key == "speed" else value


def format_metrics(
    data: dict[str, Any], pull_length_m: Any = 0.623
) -> dict[str, str]:
    """Map a raw sensor-sample dictionary to display strings keyed by tile id."""
    out: dict[str, str] = {}
    if data.get("session_elapsed__s") is not None:
        out["time"] = fmt_time(data["session_elapsed__s"])
    if data.get("distance__m") is not None:
        out["distance"] = f"{float(data['distance__m']):,.0f}"
    for key in ("speed", "stroke", "hr", "pull", "power"):
        value = metric_value(key, data, pull_length_m)
        if value is not None:
            out[key] = f"{value:.1f}" if key == "speed" else f"{round(value)}"
    return out


# --------------------------------------------------------------------------- #
# Program target adherence                                                    #
# --------------------------------------------------------------------------- #
_PACE_DISTANCES = {"pace_200": 200, "pace_500": 500, "pace_1000": 1000}


def target_value(
    metric: str,
    data: dict[str, Any],
    max_hr: Any = 185,
    zones: list[int] | None = None,
    pull_length_m: Any = 0.623,
) -> float | None:
    """Extract the current value for a target metric from a live data dict.

    Returns None when the value is unavailable. Heart-rate zones are returned
    1-indexed (Z1..Z5) to match the way targets are written.
    """
    if metric == "hr_zone":
        hr = data.get("heart_rate__bpm")
        if hr is None:
            return None
        zone = hr_zone(hr, max_hr, zones)
        return float(zone + 1) if zone >= 0 else None
    if metric in _PACE_DISTANCES:
        pace_field = f"pace_{_PACE_DISTANCES[metric]}m__s"
        value = data.get(pace_field)
        try:
            secs = float(value) if value is not None else None
        except (TypeError, ValueError):
            return None
        # A zero/huge pace means "not moving"; treat as unavailable.
        return secs if secs is not None and 0 < secs < 3600 else None
    if metric == "power":
        power = compute_power(data, pull_length_m)
        return float(power) if power is not None else None
    if metric == "spm":
        value = data.get("cadence__spm")
        return float(value) if value is not None else None
    return None


def target_status(
    metric: str, value: float | None, low: Any, high: Any
) -> str | None:
    """Classify a live value against a target range.

    Returns "in", "hard" (harder than intended) or "easy" (easier than
    intended), or None if it can't be determined. Pace is inverted — fewer
    seconds per split means a *harder* effort.
    """
    if value is None or low is None or high is None:
        return None
    try:
        lo, hi = float(low), float(high)
    except (TypeError, ValueError):
        return None
    if metric in _PACE_DISTANCES:  # lower seconds == harder
        if value < lo:
            return "hard"
        if value > hi:
            return "easy"
        return "in"
    # hr_zone / power / spm: higher == harder
    if value > hi:
        return "hard"
    if value < lo:
        return "easy"
    return "in"


def target_text(
    target: dict[str, Any] | None, max_hr: Any = 185,
    zones: list[int] | None = None,
) -> str:
    """Human-readable label for a target range (e.g. 'Zone 3', '2:10-2:25 /500m')."""
    if not target:
        return ""
    metric = target.get("metric")
    low, high = target.get("low"), target.get("high")
    if low is None or high is None:
        return ""
    if metric == "hr_zone":
        return f"Zone {low}" if low == high else f"Zone {low}-{high}"
    if metric in _PACE_DISTANCES:
        dist = _PACE_DISTANCES[metric]
        return f"{fmt_time(low)}-{fmt_time(high)} /{dist}m"
    if metric == "power":
        return f"{low} W" if low == high else f"{low}-{high} W"
    if metric == "spm":
        return f"{low} spm" if low == high else f"{low}-{high} spm"
    return f"{low}-{high}"
