"""Shared best-effort numeric coercion helpers.

Values arrive from sources the app does not fully control (BLE packets,
hand-edited CSV cells, config), so callers need one consistent conversion that
returns ``None`` instead of raising.

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
