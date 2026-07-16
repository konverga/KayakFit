"""Shared best-effort numeric coercion helpers.

The single home for the "coerce to float, tolerate garbage" rule that was
previously re-implemented per module (``segmentation._to_float``,
``summary._f``, ``program_runner._to_float``). Values arrive from sources the
app does not fully control (BLE packets, hand-edited CSV cells, config), so
callers want ``None``/fallback on failure, never an exception.

Stdlib-only on purpose: modules like ``app.segmentation`` that stay
dependency-free for unit tests can import it without pulling anything else in.
"""

from typing import Any


def to_float(value: Any) -> float | None:
    """Best-effort float coercion; returns None for missing/invalid values."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_float_or(value: Any, fallback: float) -> float:
    """Best-effort float coercion; returns ``fallback`` for missing/invalid values."""
    result = to_float(value)
    return fallback if result is None else result
