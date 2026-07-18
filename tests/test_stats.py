"""
Tests for the shared workout-metric aggregation and finalized summary metrics.

Two behaviours are pinned:

  * HR / cadence / power average over strictly-positive readings only (a 0 is a
    dropped sensor sample, not a real zero), so a run of 0 readings does NOT
    drag the average down.
  * Average speed is the represented-time-weighted mean of recorded speed.
"""

from app.stats import aggregate_metrics
from app.summary import compute_summary
from app.table import Table

COLUMNS = [
    "timestamp", "session_elapsed", "heart_rate", "cadence", "distance",
    "speed_instant", "pull_force", "active", "sample_duration",
]
T0 = 1_700_000_000_000


# --------------------------------------------------------------------------
# app.stats.aggregate_metrics -- the shared source of truth
# --------------------------------------------------------------------------
def test_positive_filtering_and_avg_speed():
    # Ten real HR readings (150) and ten dropped ones (0).
    hr = [150] * 10 + [0] * 10
    cadence = [60] * 10 + [0] * 10
    pull_force = [100] * 20
    speed = [2.5] * 10 + [0.0] * 10

    m = aggregate_metrics(
        heart_rate=hr,
        cadence=cadence,
        pull_force=pull_force,
        speed=speed,
    )
    # Zeros excluded: average is 150 / 60, not 75 / 30.
    assert m["avg_heart_rate"] == 150.0, m["avg_heart_rate"]
    assert m["max_heart_rate"] == 150.0
    assert m["avg_cadence"] == 60.0, m["avg_cadence"]
    # power = 100 * 0.623 * 60 / 60 = 62.3 -> 62 for the moving rows; the
    # cadence-0 rows estimate 0 W and are excluded.
    assert m["avg_power"] == 62.0, m["avg_power"]
    assert m["max_power"] == 62.0
    # Peak speed over positive samples.
    assert m["max_speed"] == 2.5
    # Speed is reproduced directly from the recorded samples.
    assert m["avg_speed"] == 1.25
    print("test_positive_filtering_and_avg_speed passed")


def test_all_missing_returns_none():
    m = aggregate_metrics(heart_rate=[None, 0, None], cadence=[0, 0], speed=[0, 0])
    assert m["avg_heart_rate"] is None
    assert m["max_heart_rate"] is None
    assert m["avg_cadence"] is None
    assert m["avg_power"] is None
    assert m["max_speed"] == 0.0
    assert m["avg_speed"] == 0.0
    print("test_all_missing_returns_none passed")


def test_invalid_recorded_speed_does_not_fall_back_to_distance():
    metrics = aggregate_metrics(
        speed=[None, "invalid"],
        sample_weights=[1.0, 1.0],
    )

    assert metrics["avg_speed"] is None
    assert metrics["max_speed"] is None


def test_generic_aggregator_does_not_rewrite_an_independent_peak() -> None:
    metrics = aggregate_metrics(speed=[2.70, 2.71])

    assert metrics["avg_speed"] is not None
    assert metrics["max_speed"] == 2.71
    assert metrics["avg_speed"] == (2.70 + 2.71) / 2


def test_sensor_averages_are_weighted_by_sample_window():
    metrics = aggregate_metrics(
        heart_rate=[100, 200],
        cadence=[50, 100],
        pull_force=[100, 100],
        sample_weights=[1, 3],
    )
    assert metrics["avg_heart_rate"] == 175.0
    assert metrics["avg_cadence"] == 87.5
    # Power estimates are 52 W and 104 W with the configured pull length.
    assert metrics["avg_power"] == 91.0
    print("test_sensor_averages_are_weighted_by_sample_window passed")


# --------------------------------------------------------------------------
# summary <-> FIT exporter parity for the same workout
# --------------------------------------------------------------------------
def _workout_with_dropouts() -> Table:
    """One continuous moving lap: 10s of full signal, then 10s of HR/cadence
    dropouts (0 readings) while still moving."""
    rows = []
    for i in range(20):
        good = i < 10
        rows.append(
            {
                "timestamp": T0 + (i + 1) * 1000,
                "session_elapsed": i + 1,
                "heart_rate": 150 if good else 0,
                "cadence": 60 if good else 0,
                "distance": 2.5 * (i + 1),
                "speed_instant": 2.5,
                "pull_force": 100,
                "active": 1,
                "sample_duration": 1,
            }
        )
    return Table(COLUMNS, rows)


def test_finalized_summary_excludes_dropouts():
    table = _workout_with_dropouts()
    summary = compute_summary(table)

    # Single continuous lap covering the whole workout.
    assert len(summary.laps) == 1
    assert abs(summary.distance_m - 50.0) < 1e-9
    assert abs(summary.duration_s - 20.0) < 1e-9

    # Zeros are excluded: HR average is 150, not 75.
    assert summary.avg_hr == 150.0

    # Cadence: summary reports avg_spm, exporter avg_cadence; both exclude 0s.
    assert summary.avg_spm == 60.0

    # Power: 62 W on both sides (drop rows estimate 0 W and are excluded).
    assert summary.avg_power == 62.0

    # Constant recorded speed remains 2.5 m/s.
    assert summary.avg_speed_mps is not None
    assert abs(summary.avg_speed_mps - 2.5) < 1e-9
    print("test_finalized_summary_excludes_dropouts passed")


def test_avg_speed_is_the_recorded_sample_mean():
    """Finalized speed stays reproducible from the recorded speed points."""
    # Speeds ramp 1..10 m/s but distance accumulates by the *previous* speed,
    # so the mean of samples (5.5) is not the physical average speed.
    rows = []
    dist = 0.0
    for i in range(10):
        rows.append(
            {
                "timestamp": T0 + (i + 1) * 1000,
                "session_elapsed": i + 1,
                "heart_rate": 140,
                "cadence": 70,
                "distance": dist,
                "speed_instant": float(i + 1),
                "pull_force": 120,
                "active": 1,
                "sample_duration": 1,
            }
        )
        dist += float(i + 1)  # advance by this second's speed
    table = Table(COLUMNS, rows)

    summary = compute_summary(table)
    covered = summary.distance_m
    duration = summary.duration_s
    distance_time_speed = covered / duration

    # Sample mean of the speed column would be (1+..+10)/10 = 5.5; the physical
    # average is different.
    assert abs(distance_time_speed - 5.5) > 0.1, distance_time_speed
    assert summary.avg_speed_mps is not None
    assert summary.avg_speed_mps == 5.5

    print("test_avg_speed_is_the_recorded_sample_mean passed")
