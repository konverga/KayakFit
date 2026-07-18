"""
Tests for the shared workout-metric aggregation (app/stats.py) and that both
consumers -- app.summary.compute_summary and app.export_fit's _calculate_stats
-- agree on the numbers for the same workout.

Two behaviours are pinned:

  * HR / cadence / power average over strictly-positive readings only (a 0 is a
    dropped sensor sample, not a real zero), so a run of 0 readings does NOT
    drag the average down.
  * Average speed is distance-covered / elapsed-moving-time, verified against a
    hand-computed value -- NOT a naive mean of the per-second speed samples.

The FIT dependency is part of the locked runtime environment, so this parity
test exercises the same exporter classes used by the application.
"""

from app.export_fit import FitExporter
from app.stats import aggregate_metrics
from app.summary import compute_summary
from app.table import Table

COLUMNS = [
    "timestamp", "heart_rate", "cadence", "distance", "speed", "pull_force",
    "active", "sample_duration",
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
        distance_m=100.0,
        duration_s=40.0,
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
    # Average speed is distance / time, NOT mean of speed samples (that mean
    # would be (2.5*10 + 0*10)/20 = 1.25).
    assert m["avg_speed"] == 100.0 / 40.0 == 2.5
    print("test_positive_filtering_and_avg_speed passed")


def test_all_missing_returns_none():
    m = aggregate_metrics(heart_rate=[None, 0, None], cadence=[0, 0], speed=[0, 0])
    assert m["avg_heart_rate"] is None
    assert m["max_heart_rate"] is None
    assert m["avg_cadence"] is None
    assert m["avg_power"] is None
    assert m["max_speed"] is None  # not a misleading 0
    assert m["avg_speed"] is None  # no distance/duration supplied
    print("test_all_missing_returns_none passed")


def test_avg_speed_needs_both_inputs():
    assert aggregate_metrics(distance_m=100.0)["avg_speed"] is None
    assert aggregate_metrics(duration_s=40.0)["avg_speed"] is None
    assert aggregate_metrics(distance_m=0.0, duration_s=40.0)["avg_speed"] == 0.0
    assert aggregate_metrics(distance_m=100.0, duration_s=25.0)["avg_speed"] == 4.0
    print("test_avg_speed_needs_both_inputs passed")


def test_generic_aggregator_does_not_rewrite_an_independent_peak() -> None:
    metrics = aggregate_metrics(
        speed=[2.70, 2.71],
        distance_m=10.0,
        duration_s=3.6,
    )

    assert metrics["avg_speed"] is not None
    assert metrics["max_speed"] == 2.71
    assert metrics["avg_speed"] == 10.0 / 3.6


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


def test_fit_integer_averages_use_nearest_rounding() -> None:
    table = Table(
        COLUMNS,
        [
            {
                "timestamp": T0,
                "heart_rate": 140,
                "cadence": 60,
                "distance": 2.5,
                "speed": 2.5,
                "pull_force": 100,
                "active": 1,
                "sample_duration": 1,
            },
            {
                "timestamp": T0 + 1000,
                "heart_rate": 141,
                "cadence": 61,
                "distance": 5.0,
                "speed": 2.5,
                "pull_force": 100,
                "active": 1,
                "sample_duration": 1,
            },
        ],
    )
    exporter = FitExporter(config={"pull_length_m": 0.623})

    stats = exporter._calculate_stats(table, sample_weights=[1, 4])

    assert stats["avg_heart_rate"] == 141
    assert stats["avg_cadence"] == 61
    assert stats["avg_power"] == 63


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
                "timestamp": T0 + i * 1000,
                "heart_rate": 150 if good else 0,
                "cadence": 60 if good else 0,
                "distance": 2.5 * (i + 1),
                "speed": 2.5,
                "pull_force": 100,
                "active": 1,
                "sample_duration": 1,
            }
        )
    return Table(COLUMNS, rows)


def test_summary_and_exporter_agree():
    table = _workout_with_dropouts()
    summary = compute_summary(table)

    # Single continuous lap covering the whole workout.
    assert len(summary.laps) == 1
    assert abs(summary.distance_m - 50.0) < 1e-9
    assert abs(summary.duration_s - 20.0) < 1e-9

    exporter = FitExporter(config={"log_level": "warning"})
    stats = exporter._calculate_stats(
        table,
        speed_distance_m=summary.distance_m,
        speed_duration_s=summary.duration_s,
    )

    # Zeros excluded on both sides: HR average is 150, not 75.
    assert summary.avg_hr == 150.0
    assert stats["avg_heart_rate"] == 150
    assert int(summary.avg_hr) == stats["avg_heart_rate"]

    # Cadence: summary reports avg_spm, exporter avg_cadence; both exclude 0s.
    assert summary.avg_spm == 60.0
    assert stats["avg_cadence"] == 60

    # Power: 62 W on both sides (drop rows estimate 0 W and are excluded).
    assert summary.avg_power == 62.0
    assert stats["avg_power"] == 62
    assert stats["max_power"] == 62

    # Average speed = distance / time = 2.5 m/s on both sides (a naive sample
    # mean would also be 2.5 here since speed is constant, so also check a
    # lap-level case below where it diverges).
    assert summary.avg_speed_mps is not None
    avg_speed = stats["avg_speed"]
    max_speed = stats["max_speed"]
    assert avg_speed is not None and max_speed is not None
    assert abs(summary.avg_speed_mps - 2.5) < 1e-9
    assert abs(float(avg_speed) - 2.5) < 1e-9
    assert abs(float(max_speed) - 2.5) < 1e-9
    print("test_summary_and_exporter_agree passed")


def test_avg_speed_diverges_from_sample_mean():
    """When the athlete accelerates, distance/time differs from a naive mean of
    the instantaneous speed samples; both consumers use distance/time."""
    # Speeds ramp 1..10 m/s but distance accumulates by the *previous* speed,
    # so the mean of samples (5.5) is not the physical average speed.
    rows = []
    dist = 0.0
    for i in range(10):
        rows.append(
            {
                "timestamp": T0 + i * 1000,
                "heart_rate": 140,
                "cadence": 70,
                "distance": dist,
                "speed": float(i + 1),
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
    expected_avg_speed = covered / duration

    # Sample mean of the speed column would be (1+..+10)/10 = 5.5; the physical
    # average is different.
    assert abs(expected_avg_speed - 5.5) > 0.1, expected_avg_speed
    assert summary.avg_speed_mps is not None
    assert abs(summary.avg_speed_mps - expected_avg_speed) < 1e-9

    exporter = FitExporter(config={"log_level": "warning"})
    stats = exporter._calculate_stats(
        table, speed_distance_m=covered, speed_duration_s=duration
    )
    avg_speed = stats["avg_speed"]
    assert avg_speed is not None
    assert abs(float(avg_speed) - expected_avg_speed) < 1e-9
    print("test_avg_speed_diverges_from_sample_mean passed")
