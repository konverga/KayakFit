"""Shared power estimation.

The KayakFirst ergometer has no power sensor; its display derives power from
flywheel-based pull force and stroke rate. This module reproduces that model:

    power (W) = pull_force (N) x pull_length (m) x strokes_per_second

Physics: work per stroke = force x pull distance (J); strokes per second =
cadence / 60; power = J/s = W. The effective handle pull length was fitted
against manually logged display values (2026-07-03 BULL session, 59 points):
L = 0.623 m, RMSE 1.1 W, R² 0.89, 98% of points within ±5 W. The same L also
reproduces the display's session Average Power, which the firmware computes
as L x avg_force x avg_cadence / 60 (product of cumulative means).

The pull length can be recalibrated for other KayakFirst models or firmware
revisions (see docs/POWER_MODEL.md); callers pass the recorded
``pull_length_m`` value explicitly so concurrent exports cannot affect each
other through process-global state.

This single definition is used by the live dashboard, the summary window and
the FIT exporter so the on-screen watts always match the exported file.
"""

from typing import Any

# Effective handle pull length (m). See module docstring for the calibration.
DEFAULT_PULL_LENGTH_M = 0.623

def normalize_pull_length(value: Any) -> float:
    """Return a valid effective pull length, or the calibrated default."""
    try:
        length = float(value)
    except (TypeError, ValueError):
        length = DEFAULT_PULL_LENGTH_M
    # Sanity range: a handle pull length outside this is a config mistake.
    if not 0.3 <= length <= 1.2:
        length = DEFAULT_PULL_LENGTH_M
    return length


def estimate_power(
    pull_force_n: Any,
    cadence_spm: Any,
    pull_length_m: Any = DEFAULT_PULL_LENGTH_M,
) -> int | None:
    """Estimate power (W) from pull force (N) and cadence (spm).

    Prefer the stroke-averaged fields (``pull_force__n`` / ``cadence__spm``):
    they track the machine's own power display to ~1 W RMSE, while the
    instantaneous fields are single-stroke snapshots that deviate by ~25 W RMSE.

    Returns None when either input is missing or non-numeric.
    """
    if pull_force_n is None or cadence_spm is None:
        return None
    try:
        length = normalize_pull_length(pull_length_m)
        return round(float(pull_force_n) * length * float(cadence_spm) / 60)
    except (TypeError, ValueError):
        return None
