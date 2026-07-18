"""
Tests for the live speed preview (app.speed_series.LiveSpeedTracker).

The tracker uses the same recorded-point policy as batch finalization:
instantaneous speed is time-weighted over active intervals, its raw peak is the
maximum, and cumulative distance is accumulated independently.

The finalized (post-workout) series itself is pinned end-to-end by
tests/test_summary.py and tests/test_pipeline.py; this file covers only the
streaming preview, which has no other test surface (the GUI is untested).
"""

from app.finalized_workout import finalize_workout
from app.speed_series import LiveSpeedTracker
from app.summary import compute_summary
from app.table import Table


def _feed(
    tracker: LiveSpeedTracker,
    cumulative_m: float | None,
    active: int = 1,
    duration_s: float = 1.0,
    reported_mps: float | None = 2.0,
) -> None:
    tracker.update(
        distance_m=cumulative_m,
        duration_s=duration_s,
        active=active,
        reported_speed_mps=reported_mps,
    )


def test_avg_and_max_use_recorded_instantaneous_speed():
    """Six slow seconds (2 m) then six fast seconds (4 m): avg 3.0, max 4.0."""
    tracker = LiveSpeedTracker()
    cumulative = 0.0
    for _ in range(6):
        cumulative += 2.0
        _feed(tracker, cumulative, reported_mps=2.0)
    for _ in range(6):
        cumulative += 4.0
        _feed(tracker, cumulative, reported_mps=4.0)

    assert tracker.avg_speed_mps == 3.0
    assert tracker.max_speed_mps == 4.0


def test_single_sample_sensor_peak_is_reported_verbatim():
    """Canonical max is the largest active instantaneous speed sample."""
    tracker = LiveSpeedTracker()
    cumulative = 0.0
    for step in (2.0, 2.0, 2.0, 10.0, 2.0, 2.0, 2.0):
        cumulative += step
        _feed(tracker, cumulative, reported_mps=step)

    assert tracker.max_speed_mps is not None
    assert tracker.max_speed_mps == 10.0


def test_pause_excludes_idle_time_and_distance():
    """Idle samples add neither average denominator nor active distance."""
    tracker = LiveSpeedTracker()
    cumulative = 0.0
    for _ in range(5):  # 10 m in 5 s
        cumulative += 2.0
        _feed(tracker, cumulative, reported_mps=2.0)
    cumulative += 30.0  # odometer drift while paused belongs to no lap
    for _ in range(3):
        _feed(tracker, cumulative, active=0)
    cumulative += 50.0  # coarse resume jump remains recorded distance
    _feed(tracker, cumulative, reported_mps=4.0)
    for _ in range(4):  # steady 4 m/s
        cumulative += 4.0
        _feed(tracker, cumulative, reported_mps=4.0)

    assert tracker.active_time_s == 10.0  # 3 idle seconds excluded
    assert tracker.active_distance_m == 76.0  # 10 + 50 + 16
    assert tracker.avg_speed_mps == 3.0
    assert tracker.max_speed_mps == 4.0


def test_dropped_odometer_reading_is_bridged_not_spiked():
    """A missing distance cell defers its span to the next valid reading."""
    tracker = LiveSpeedTracker()
    _feed(tracker, 2.0, reported_mps=2.0)
    _feed(tracker, None, reported_mps=2.0)
    _feed(tracker, 6.0, reported_mps=2.0)

    assert tracker.active_distance_m == 6.0
    assert tracker.active_time_s == 3.0
    assert tracker.max_speed_mps is not None
    assert tracker.max_speed_mps <= 2.0


def test_slow_active_spans_remain_valid_measurements():
    """The activity signal, not an arbitrary speed floor, owns measurements."""
    tracker = LiveSpeedTracker()
    cumulative = 0.0
    for _ in range(5):
        cumulative += 0.2
        _feed(tracker, cumulative, reported_mps=0.2)
    assert tracker.max_speed_mps is not None
    assert abs(tracker.max_speed_mps - 0.2) < 1e-9

    for _ in range(3):
        cumulative += 4.0
        _feed(tracker, cumulative, reported_mps=4.0)
    assert tracker.max_speed_mps == 4.0
    # The slow active seconds still count in the average's denominator,
    # exactly like finalized timer time.
    assert tracker.avg_speed_mps == (1.0 + 12.0) / 8.0


def test_resume_distance_is_not_rewritten_by_speed():
    """Recorded odometer jumps are independent of sensor speed statistics."""
    fresh = LiveSpeedTracker()
    _feed(fresh, 3.0, reported_mps=0.0)  # a wrong cap would zero this
    assert fresh.active_distance_m == 3.0

    resumed = LiveSpeedTracker()
    _feed(resumed, 0.0, active=0)
    _feed(resumed, 50.0, reported_mps=3.0)
    assert resumed.active_distance_m == 50.0


def test_zero_duration_active_observation_only_updates_the_baseline() -> None:
    tracker = LiveSpeedTracker()
    _feed(tracker, 10.0, duration_s=0, reported_mps=5.0)
    _feed(tracker, 12.0, duration_s=1, reported_mps=2.0)

    assert tracker.active_time_s == 1.0
    assert tracker.active_distance_m == 2.0
    assert tracker.avg_speed_mps == tracker.max_speed_mps == 2.0


def test_live_and_finalized_resume_distance_use_the_same_raw_odometer():
    columns = [
        "timestamp", "session_elapsed", "heart_rate", "cadence", "distance",
        "speed", "speed_instant", "pull_force", "active", "sample_duration",
    ]
    rows = [
        dict(zip(columns, [1000, 1, 120, 60, 3.0, 3.0, 3.0, 100, 1, 1], strict=True)),
        dict(zip(columns, [2000, 2, 120, 0, 3.0, 0.0, 0.0, 0, 0, 1], strict=True)),
        dict(zip(columns, [3000, 3, 120, 60, 13.0, 1.0, 3.0, 100, 1, 1], strict=True)),
    ]
    tracker = LiveSpeedTracker()
    for row in rows:
        tracker.update(
            distance_m=row["distance"],
            duration_s=row["sample_duration"],
            active=row["active"],
            reported_speed_mps=row["speed_instant"],
        )

    finalized = finalize_workout(Table(columns, rows))
    assert tracker.active_distance_m == finalized.total_distance_m == 13.0
    assert tracker.active_time_s == finalized.total_timer_time_ms / 1000.0 == 2.0
    assert tracker.avg_speed_mps == 3.0


def test_zero_distance_active_window_reports_zero_average_and_maximum():
    columns = [
        "timestamp", "session_elapsed", "heart_rate", "cadence", "distance",
        "speed_instant", "pull_force", "active", "sample_duration",
    ]
    row = dict(zip(columns, [1000, 1, 120, 60, 0.0, 0.0, 100, 1, 1], strict=True))
    tracker = LiveSpeedTracker()
    tracker.update(distance_m=0.0, duration_s=1, active=1, reported_speed_mps=0.0)
    summary = compute_summary(Table(columns, [row]))

    assert tracker.avg_speed_mps == summary.avg_speed_mps == 0.0
    assert tracker.max_speed_mps == summary.max_speed_mps == 0.0
