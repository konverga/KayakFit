"""Canonical workout timing created once when ergometer packets arrive."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any


def _seconds_to_ms(value: Any, field: str) -> int:
    """Parse a finite non-negative seconds value at millisecond precision."""
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Ergometer packet has invalid {field}") from exc
    if not isfinite(seconds) or seconds < 0:
        raise ValueError(f"Ergometer packet has invalid {field}")
    return round(seconds * 1000)


@dataclass(frozen=True)
class RecordTiming:
    """Canonical endpoint and represented window for one retained packet."""

    timestamp_ms: int
    session_elapsed_s: float
    sample_duration_s: float
    gap_before_s: float


class RecordingTimeline:
    """Turn raw device timing into a monotonic, non-overlapping session clock.

    Advancing device elapsed time remains the primary clock. A stalled or reset
    counter is rebased to monotonic receipt time. The raw device window is then
    clipped so it never overlaps the preceding represented window; any time not
    covered by a sample remains an explicit pause.
    """

    def __init__(self, *, epoch_start_ms: int, monotonic_start_s: float) -> None:
        self.epoch_start_ms = int(epoch_start_ms)
        self.monotonic_start_s = float(monotonic_start_s)
        self._last_device_elapsed_ms: int | None = None
        self._last_session_elapsed_ms = 0

    @property
    def session_elapsed_s(self) -> float:
        """Latest canonical session endpoint in seconds."""
        return self._last_session_elapsed_ms / 1000.0

    def advance(
        self,
        *,
        device_elapsed_s: Any,
        device_window_s: Any,
        receipt_monotonic_s: float,
    ) -> RecordTiming:
        """Create canonical timing for one raw packet."""
        device_elapsed_ms = _seconds_to_ms(device_elapsed_s, "elapsed_time__s")
        requested_duration_ms = _seconds_to_ms(device_window_s, "window_size__s")
        receipt_elapsed_ms = max(
            round((float(receipt_monotonic_s) - self.monotonic_start_s) * 1000),
            0,
        )

        previous_endpoint_ms = self._last_session_elapsed_ms
        if self._last_device_elapsed_ms is None:
            endpoint_ms = device_elapsed_ms
        elif device_elapsed_ms > self._last_device_elapsed_ms:
            endpoint_ms = previous_endpoint_ms + (
                device_elapsed_ms - self._last_device_elapsed_ms
            )
        else:
            endpoint_ms = max(receipt_elapsed_ms, previous_endpoint_ms)

        available_ms = max(endpoint_ms - previous_endpoint_ms, 0)
        represented_ms = min(requested_duration_ms, available_ms)
        window_start_ms = endpoint_ms - represented_ms
        gap_before_ms = max(window_start_ms - previous_endpoint_ms, 0)

        self._last_device_elapsed_ms = device_elapsed_ms
        self._last_session_elapsed_ms = endpoint_ms
        return RecordTiming(
            timestamp_ms=self.epoch_start_ms + endpoint_ms,
            session_elapsed_s=endpoint_ms / 1000.0,
            sample_duration_s=represented_ms / 1000.0,
            gap_before_s=gap_before_ms / 1000.0,
        )
