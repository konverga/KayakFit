"""
Tests for the post-workout summary computation (app/summary.py).

Builds small synthetic workout Tables (same shape as CsvReader.read_all output)
and checks totals, laps, zone time, and chart series.

Run from the project root:

    python tests/test_summary.py
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.summary import compute_summary
from app.table import Table
from gui.summary_window import (
    _axis_bounds,
    _downsample_indices,
    _hr_zone_parts,
    _MultiChart,
)

COLUMNS = [
    "timestamp", "session_elapsed", "heart_rate", "cadence", "cadence_instant", "distance",
    "speed_instant", "pull_force", "pull_force_instant", "active", "sample_duration",
]
T0 = 1_700_000_000_000  # arbitrary epoch ms


def test_planned_phase_strip_geometry():
    """Only time-based planned charts reserve the compact phase strip."""
    expected = _MultiChart._PHASE_H + _MultiChart._PHASE_GAP
    assert _MultiChart._phase_space("time", has_steps=True) == expected
    assert _MultiChart._phase_space("time", has_steps=False) == 0
    assert _MultiChart._phase_space("distance", has_steps=True) == 0


def _row(
    i, hr=None, cadence=60, distance=None, speed=2.5, pull_force=100,
    ts=None, active=1, sample_duration=1,
):
    endpoint_s = (ts if ts is not None else i) + 1
    return {
        "timestamp": T0 + endpoint_s * 1000,
        "session_elapsed": endpoint_s,
        "heart_rate": hr,
        "cadence": cadence,
        "cadence_instant": cadence,
        "distance": distance if distance is not None else 2.5 * (i + 1),
        "speed_instant": speed,
        "pull_force": pull_force,
        "pull_force_instant": pull_force,
        "active": active,
        "sample_duration": sample_duration,
    }


def _steady_table(n=60, hr=150):
    """n seconds at 2.5 m/s, constant HR/cadence/force."""
    return Table(COLUMNS, [_row(i, hr=hr) for i in range(n)])


def test_totals_and_series():
    n = 60
    s = compute_summary(_steady_table(n))

    assert s.start_time is not None
    assert abs(s.distance_m - 2.5 * n) < 1e-6
    # One continuous lap covering the whole workout.
    assert len(s.laps) == 1
    assert abs(s.active_time_s - n) < 1e-6
    # Constant 2.5 m/s
    assert s.avg_speed_mps is not None and s.max_speed_mps is not None
    assert s.avg_hr is not None
    lap0_avg_speed = s.laps[0].avg_speed_mps
    assert lap0_avg_speed is not None
    assert abs(s.avg_speed_mps - 2.5) < 1e-6
    assert abs(s.max_speed_mps - 2.5) < 1e-6
    assert abs(lap0_avg_speed - 2.5) < 1e-6
    assert abs(s.avg_hr - 150.0) < 1e-6
    assert s.max_hr == 150
    # power = 100 N * 0.600 m * 60 spm / 60 = 60 W
    assert s.avg_power is not None and abs(s.avg_power - 60.0) < 1e-6
    assert s.max_power == 60
    # Series lengths all match record count.
    assert (
        len(s.series_t)
        == len(s.series_speed)
        == len(s.series_hr)
        == len(s.series_power)
        == n
    )
    assert s.series_t[0] == 1.0 and s.series_t[-1] == float(n)
    assert s.series_time_bounds == (0.0, float(n))
    series_speed0 = s.series_speed[0]
    assert series_speed0 is not None
    assert abs(series_speed0 - 2.5) < 1e-6
    print("test_totals_and_series passed")


def test_two_laps_from_gap():
    """An explicit inactive record separates two free-workout laps."""
    rows = [_row(i) for i in range(30)]
    rows.append(_row(30, active=0, speed=0, distance=75.0))
    # Second effort resumes later; the activity transition owns the split.
    rows += [
        _row(30 + i, ts=90 + i, distance=2.5 * (30 + i)) for i in range(30)
    ]
    s = compute_summary(Table(COLUMNS, rows))

    assert len(s.laps) == 2
    assert abs(s.laps[0].active_time_s - 30.0) < 1e-6
    assert abs(s.laps[1].active_time_s - 30.0) < 1e-6
    # Moving time excludes the 60s pause.
    assert abs(s.active_time_s - 60.0) < 1e-6
    assert s.active_time_s == 60.0
    assert s.elapsed_time_s == 120.0
    assert s.pause_time_s == 60.0
    assert sum(
        lap.elapsed_time_s + lap.pause_after_s for lap in s.laps
    ) == s.elapsed_time_s
    assert sum(lap.active_time_s for lap in s.laps) == s.active_time_s
    # Lap 2 starts 90s after the workout start (wall clock).
    assert abs(s.laps[1].start_s - 90.0) < 1e-6
    # Each lap covers its own distance.
    assert abs(s.laps[0].distance_m - 2.5 * 30) < 1e-6
    print("test_two_laps_from_gap passed")


def test_split_speed_uses_recorded_signal_after_distance_jump():
    """A coarse odometer jump does not rewrite recorded speed statistics."""
    rows = [
        _row(0, distance=3.0, speed=3.0),
        _row(1, distance=6.0, speed=3.0),
        _row(2, distance=6.0, speed=0.0, active=0),
        _row(3, distance=6.0, speed=0.0, active=0),
        # The machine reports distance accumulated around the restart in one
        # coarse update, while its recorded speed remains 3 m/s.
        _row(4, ts=10, distance=18.0, speed=3.0),
        _row(5, ts=11, distance=21.0, speed=3.0),
    ]
    summary = compute_summary(Table(COLUMNS, rows))

    assert len(summary.laps) == 2
    assert summary.laps[1].distance_m == 15.0
    assert summary.laps[1].avg_speed_mps == 3.0
    assert summary.laps[1].avg_speed_mps <= max(
        speed for speed in summary.series_speed if speed is not None
    )


def test_raw_speed_line_never_exceeds_reported_max():
    """The plotted raw speed points and cards use the same recorded channel."""
    rows = [_row(i, distance=_cumulative_distance(i), speed=1.0) for i in range(12)]
    s = compute_summary(Table(COLUMNS, rows))

    assert s.avg_speed_mps == 1.0
    assert s.max_speed_mps == 1.0

    line = [v for v in s.series_speed if v is not None]
    assert line, "expected a plotted speed line"
    assert all(v <= s.max_speed_mps + 1e-9 for v in line)
    assert max(line) == s.max_speed_mps
    # A maximum is never below the average.
    assert s.max_speed_mps >= s.avg_speed_mps


def _cumulative_distance(i: int) -> float:
    """Cumulative metres at record ``i`` for 2 m/s (first 6 s) then 4 m/s."""
    slow = min(i + 1, 6) * 2.0
    fast = max(i - 5, 0) * 4.0
    return slow + fast


def test_overlapping_sample_windows_are_rejected():
    """Corrupt timing must not produce FIT timer time greater than elapsed time."""
    rows = [
        _row(0, sample_duration=1),
        _row(1, sample_duration=2),
    ]

    with pytest.raises(ValueError, match="sample windows overlap"):
        compute_summary(Table(COLUMNS, rows))


def test_chart_axis_includes_authoritative_maximum():
    _lo, hi = _axis_bounds(
        [1.0, 2.0], floor=0.0, ceiling=5.0, authoritative_max=10.0
    )

    assert hi > 10.0


def test_each_active_signal_gap_fragments_chart_and_laps():
    columns = COLUMNS
    actives = [1, 1, 1, 0, 0, 1, 1, 1]
    rows = []
    distance = 0.0
    for i, active in enumerate(actives):
        if active:
            distance += 3.0
        rows.append(
            {
                **_row(
                    i,
                    hr=120 + i if active else 220,
                    distance=distance,
                    speed=3.0 if active else 0.0,
                ),
                "active": active,
                "sample_duration": 1,
            }
        )
    summary = compute_summary(Table(columns, rows))
    assert summary.active_time_s == 6.0
    assert len(summary.laps) == 2
    assert len(summary.series_t) == 6
    assert summary.series_breaks == [True, False, False, True, False, False]
    assert summary.max_hr == 127
    print("test_brief_active_signal_gaps_do_not_fragment_chart_or_laps passed")


def test_zone_time():
    """HR zone accumulation with default bounds (max_hr=200 -> Z bounds 0/120/140/160/180).

    Each record credits the delta to the next record of its segment; the final
    record contributes nothing, so zone time sums to moving time exactly.
    """
    rows = (
        [_row(i, hr=110) for i in range(0, 10)]  # Z1
        + [_row(i, hr=150) for i in range(10, 20)]  # Z3
        + [_row(i, hr=185) for i in range(20, 30)]  # Z5
    )
    s = compute_summary(Table(COLUMNS, rows), max_hr=200)

    assert abs(sum(s.zone_seconds) - s.active_time_s) < 1e-6
    assert abs(sum(s.zone_seconds) - 30.0) < 1e-6
    assert abs(s.zone_seconds[0] - 10.0) < 1e-6
    assert abs(s.zone_seconds[2] - 10.0) < 1e-6
    assert abs(s.zone_seconds[4] - 10.0) < 1e-6
    assert s.zone_seconds[1] == 0.0 and s.zone_seconds[3] == 0.0
    print("test_zone_time passed")


def test_zone_time_excludes_pause_and_tail():
    """Zone time counts moving segments only, matching the FIT time domain.

    The HR strap keeps reporting during a mid-workout pause and through the
    post-workout cooldown, but neither period may be credited to a zone: the
    FIT file brackets the pause with STOP_ALL/START events and drops the
    frozen-distance tail, so Garmin/Strava won't count them either. The zone
    total must equal the moving time (both efforts), not the recording length.
    """
    rows = [_row(i, hr=150) for i in range(30)]  # effort 1 (Z3 @ max_hr=200)
    # 20 s pause: HR still reporting high, but distance frozen and speed 0.
    rows += [
        _row(30 + i, hr=165, distance=2.5 * 30, speed=0.0, cadence=0,
             pull_force=0, active=0)
        for i in range(20)
    ]
    # Effort 2 resumes: distance advances again.
    rows += [
        _row(50 + i, hr=150, distance=2.5 * 30 + 2.5 * (i + 1)) for i in range(30)
    ]
    # Cooldown tail: strap keeps reporting (Z2) after the last stroke.
    last_dist = 2.5 * 60
    rows += [
        _row(80 + i, hr=125, distance=last_dist, speed=0.0, cadence=0,
             pull_force=0, active=0)
        for i in range(15)
    ]
    s = compute_summary(Table(COLUMNS, rows), max_hr=200)

    assert len(s.laps) == 2, [(lap.start_s, lap.active_time_s) for lap in s.laps]
    # Zone total == moving time, not the 95 s recording span.
    assert abs(sum(s.zone_seconds) - s.active_time_s) < 1e-6, (
        sum(s.zone_seconds),
        s.active_time_s,
    )
    # The efforts (Z3) are counted; the pause (hr=165, Z4) and the cooldown
    # tail (hr=125, Z2) are not credited at all.
    assert s.zone_seconds[2] == max(s.zone_seconds)
    assert s.zone_seconds[1] == 0.0, s.zone_seconds
    assert s.zone_seconds[3] == 0.0, s.zone_seconds
    print("test_zone_time_excludes_pause_and_tail passed")


def test_manual_zones():
    rows = [_row(i, hr=130) for i in range(10)]
    # Manual bounds put 130 bpm in Z4 (>=125, <135).
    s = compute_summary(
        Table(COLUMNS, rows), zones=[80, 100, 115, 125, 135]
    )
    assert s.zone_seconds[3] == max(s.zone_seconds)
    print("test_manual_zones passed")


def test_missing_hr_and_force():
    """No HR strap and no force data: summary still works, optional fields None."""
    rows = [
        _row(i, hr=None, pull_force=None, cadence=None) for i in range(20)
    ]
    s = compute_summary(Table(COLUMNS, rows))
    assert s.avg_hr is None and s.max_hr is None
    assert s.avg_power is None and s.max_power is None
    assert s.avg_spm is None
    assert sum(s.zone_seconds) == 0.0
    assert all(v is None for v in s.series_hr)
    assert all(v is None for v in s.series_power)
    assert abs(s.distance_m - 2.5 * 20) < 1e-6
    print("test_missing_hr_and_force passed")


def test_hr_zone_percentages_include_missing_signal_time():
    rows = [
        _row(i, hr=150 if i < 5 else None)
        for i in range(10)
    ]
    summary = compute_summary(Table(COLUMNS, rows), max_hr=200)
    parts, total = _hr_zone_parts(summary)

    assert total == summary.active_time_s == 10.0
    assert sum(seconds for _name, seconds, _color in parts) == total
    assert parts[-1][0] == "No HR signal"
    assert parts[-1][1] == 5.0


def test_series_distance_matches_column():
    """series_distance mirrors the distance column, same length as series_t."""
    n = 60
    s = compute_summary(_steady_table(n))
    assert len(s.series_distance) == len(s.series_t) == n
    # Distance column advances by 2.5 m in every represented window.
    for i, d in enumerate(s.series_distance):
        assert d is not None and abs(d - 2.5 * (i + 1)) < 1e-6
    print("test_series_distance_matches_column passed")


def test_series_distance_preserves_none():
    """Missing distance cells surface as None in the right positions and don't
    disturb the other series."""
    rows = [_row(i) for i in range(20)]
    # Blank out the distance reading on two records (an empty CSV cell parses
    # to None) without touching anything else.
    rows[5]["distance"] = None
    rows[12]["distance"] = None
    s = compute_summary(Table(COLUMNS, rows))

    assert len(s.series_distance) == 20
    assert s.series_distance[5] is None
    assert s.series_distance[12] is None
    for i, d in enumerate(s.series_distance):
        if i in (5, 12):
            continue
        assert d is not None and abs(d - 2.5 * (i + 1)) < 1e-6
    # Other series are unaffected by the missing distance cells.
    assert len(s.series_t) == 20 and s.series_t[5] == 6.0
    assert all(v is not None for v in s.series_speed)
    print("test_series_distance_preserves_none passed")


def test_empty_table_raises():
    try:
        compute_summary(Table(COLUMNS, []))
    except ValueError:
        print("test_empty_table_raises passed")
        return
    raise AssertionError("expected ValueError for empty table")


def test_all_inactive_table_raises():
    rows = [_row(i, active=0, speed=0, cadence=0, pull_force=0) for i in range(5)]
    with pytest.raises(ValueError, match="no active ergometer"):
        compute_summary(Table(COLUMNS, rows))


def test_summary_excludes_inactive_heart_rate_tail():
    """The ergometer activity signal alone defines the final record."""
    rows = [_row(i, hr=140) for i in range(20)]  # 20 moving records
    frozen_dist = 2.5 * 20
    rows += [
        _row(20 + j, hr=138, distance=frozen_dist, speed=3.0, cadence=0,
             pull_force=0, active=0)
        for j in range(3)
    ]
    s = compute_summary(Table(COLUMNS, rows))
    assert len(s.laps) == 1
    assert s.laps[0].active_time_s == 20.0
    assert abs(s.laps[0].distance_m - frozen_dist) < 1e-6
    assert len(s.series_t) == 20
    assert s.series_t[-1] == 20.0
    assert s.active_time_s == 20.0


def test_chart_downsampling_preserves_extrema_and_breaks():
    xs = [float(i) for i in range(10_000)]
    ys = [100.0] * len(xs)
    ys[4_321] = 999.0
    breaks = [False] * len(xs)
    breaks[7_000] = True
    indices = _downsample_indices(xs, ys, breaks, width=500)
    assert len(indices) <= 2_100
    assert 4_321 in indices
    assert 7_000 in indices
    assert 0 in indices and 9_999 in indices
