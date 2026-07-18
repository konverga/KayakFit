"""Canonical capture-time recording timeline contracts."""

from app.finalized_workout import finalize_workout
from app.recording_timeline import RecordingTimeline
from app.speed_series import LiveSpeedTracker
from app.table import Table


def _timeline() -> RecordingTimeline:
    return RecordingTimeline(epoch_start_ms=1_700_000_000_000, monotonic_start_s=10.0)


def test_first_zero_elapsed_packet_is_a_zero_duration_observation() -> None:
    timing = _timeline().advance(
        device_elapsed_s=0, device_window_s=1, receipt_monotonic_s=10.2
    )

    assert timing.session_elapsed_s == 0.0
    assert timing.sample_duration_s == 0.0
    assert timing.gap_before_s == 0.0


def test_normal_and_multi_second_windows_follow_device_elapsed() -> None:
    timeline = _timeline()
    first = timeline.advance(
        device_elapsed_s=1, device_window_s=1, receipt_monotonic_s=11
    )
    delayed = timeline.advance(
        device_elapsed_s=3, device_window_s=2, receipt_monotonic_s=13.4
    )

    assert first.session_elapsed_s == first.sample_duration_s == 1.0
    assert delayed.session_elapsed_s == 3.0
    assert delayed.sample_duration_s == 2.0
    assert delayed.gap_before_s == 0.0


def test_repeated_counter_at_same_receipt_does_not_invent_active_time() -> None:
    timeline = _timeline()
    timeline.advance(device_elapsed_s=1, device_window_s=1, receipt_monotonic_s=11)
    repeated = timeline.advance(
        device_elapsed_s=1, device_window_s=1, receipt_monotonic_s=11
    )

    assert repeated.session_elapsed_s == 1.0
    assert repeated.sample_duration_s == 0.0


def test_counter_reset_rebases_to_receipt_time_and_exposes_gap() -> None:
    timeline = _timeline()
    timeline.advance(device_elapsed_s=5, device_window_s=1, receipt_monotonic_s=15)
    reset = timeline.advance(
        device_elapsed_s=0, device_window_s=1, receipt_monotonic_s=40
    )
    resumed = timeline.advance(
        device_elapsed_s=1, device_window_s=1, receipt_monotonic_s=41
    )

    assert reset.session_elapsed_s == 30.0
    assert reset.sample_duration_s == 1.0
    assert reset.gap_before_s == 24.0
    assert resumed.session_elapsed_s == 31.0
    assert resumed.sample_duration_s == 1.0
    assert resumed.gap_before_s == 0.0


def test_reconnect_gap_keeps_live_and_finalized_totals_equal() -> None:
    timeline = _timeline()
    tracker = LiveSpeedTracker()
    raw_samples = [
        (1, 11.0, 2.0),
        (2, 12.0, 4.0),
        (0, 40.0, 6.0),
    ]
    rows = []
    for raw_elapsed, receipt, distance in raw_samples:
        timing = timeline.advance(
            device_elapsed_s=raw_elapsed,
            device_window_s=1,
            receipt_monotonic_s=receipt,
        )
        row = {
            "timestamp": timing.timestamp_ms,
            "session_elapsed": timing.session_elapsed_s,
            "sample_duration": timing.sample_duration_s,
            "distance": distance,
            "speed_instant": 2.0,
            "active": 1,
        }
        rows.append(row)
        tracker.update(
            distance_m=distance,
            duration_s=timing.sample_duration_s,
            active=1,
            reported_speed_mps=2.0,
        )

    finalized = finalize_workout(Table(list(rows[0]), rows))

    assert finalized.total_elapsed_time_ms == 30_000
    assert finalized.total_timer_time_ms == 3_000
    assert len(finalized.segments) == 2
    assert tracker.active_time_s == finalized.total_timer_time_ms / 1000
    assert tracker.active_distance_m == finalized.total_distance_m == 6.0
