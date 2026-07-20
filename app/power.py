"""Shared power estimation.

The KayakFirst ergometer has no power sensor; its display derives power from
flywheel-based pull force and stroke rate. This module reproduces that model:

    power (W) = pull_force (N) x pull_length (m) x strokes_per_second

Physics: work per stroke = force x pull distance (J); strokes per second =
cadence / 60; power = J/s = W. KayakFit applies the model to the ergometer's
instantaneous pull-force and cadence points. The calibrated effective handle
pull length is L = 0.600 m.

The pull length can be recalibrated for other KayakFirst models or firmware
revisions (see docs/POWER_MODEL.md); callers pass the recorded
``pull_length_m`` value explicitly so concurrent exports cannot affect each
other through process-global state.

This single formula and instantaneous-source policy are used by the live
dashboard, summary window, and FIT exporter.
"""

from typing import Any

# Effective handle pull length (m). See module docstring for the calibration.
DEFAULT_PULL_LENGTH_M = 0.600

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

    Callers supply the instantaneous pull-force and cadence fields retained
    from the same ergometer packet.

    Returns None when either input is missing or non-numeric.
    """
    if pull_force_n is None or cadence_spm is None:
        return None
    try:
        length = normalize_pull_length(pull_length_m)
        return round(float(pull_force_n) * length * float(cadence_spm) / 60)
    except (TypeError, ValueError):
        return None
