"""
Tests for planned-step statistics and freestyle autopause grouping.

Plans define the step/lap structure. The ergometer's active signal selects the
records used for each step's moving time and averages, so inactive rest remains
visible without lowering HR/cadence/power. Free workouts use autopause instead.

Run from the project root:

    python tests/test_program_laps.py
"""

import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.finalized_workout import finalize_workout
from app.summary import compute_summary
from app.table import Table

COLUMNS = [
    "timestamp", "heart_rate", "cadence", "distance", "speed", "pull_force",
    "active", "sample_duration",
]
T0 = 1_700_000_000_000  # arbitrary epoch ms


class _WorkoutBuilder:
    """Build a synthetic 1 Hz recording plus its program-step timeline."""

    def __init__(self):
        self.rows = []
        self.steps = []
        self._d = 0.0
        self._i = 0

    def _append(self, speed, advance_m):
        self._d += advance_m
        self.rows.append(
            {
                "timestamp": T0 + self._i * 1000,
                "heart_rate": 140,
                "cadence": 70 if advance_m else 0,
                "distance": self._d,
                "speed": speed,
                "pull_force": 180 if advance_m else 0,
                "active": 1 if advance_m else 0,
                "sample_duration": 1,
            }
        )
        self._i += 1

    def step(self, kind, label, phases):
        """One program step made of (n_records, speed, advance_m) phases."""
        start_i = self._i
        for n, speed, advance in phases:
            for _ in range(n):
                self._append(speed, advance)
        self.steps.append(
            {
                "index": len(self.steps),
                "type": kind,
                "label": label,
                "start_ms": T0 + (start_i - 1) * 1000,
                "end_ms": T0 + (self._i - 1) * 1000,
            }
        )

    def build(self):
        return Table(COLUMNS, self.rows), self.steps

    @property
    def timestamps(self):
        return [r["timestamp"] for r in self.rows]

    @property
    def distances(self):
        return [r["distance"] for r in self.rows]


def _coast_down_workout():
    """work 60s -> rest 30s (6s of coast, then motionless) -> work 60s."""
    b = _WorkoutBuilder()
    b.step("work", "Work 1:00", [(60, 3.0, 3.0)])
    b.step("rest", "Rest 0:30", [(6, 1.5, 1.5), (24, 0.0, 0.0)])
    b.step("work", "Work 1:00", [(60, 3.0, 3.0)])
    return b


def test_summary_uses_program_steps_and_labels_laps() -> None:
    b = _coast_down_workout()
    table, steps = b.build()
    s = compute_summary(table, program_steps=steps)
    assert len(s.laps) == 3, f"expected one lap per step, got {len(s.laps)}"
    assert [lap.kind for lap in s.laps] == ["work", "rest", "work"]
    assert s.laps[0].label == "Work 1:00"
    assert s.laps[1].label == "Rest 0:30"
    # Each planned step owns its own active-only averages. The six coast rows
    # are no longer folded into Work; they belong to Rest, and its motionless
    # remainder cannot lower the averages.
    assert abs(s.laps[0].duration_s - 60.0) < 1e-6, s.laps[0].duration_s
    assert s.laps[1].duration_s == 6.0
    assert abs(s.laps[2].start_s - 90.0) < 1e-6
    print("  ok: summary reports active-only averages for every planned step")


def test_summary_agrees_with_fit_export() -> None:
    """The in-app summary and the exported FIT must show the same laps."""
    from app.export_fit import FitExporter

    b = _coast_down_workout()
    table, steps = b.build()

    class _RecordingBuilder:
        def __init__(self):
            self.messages = []

        def add(self, message=None):
            self.messages.append(message)

    exporter = FitExporter(config={"log_level": "warning"})
    finalized = finalize_workout(table, program_steps=steps)
    exporter._add_records(
        builder=_RecordingBuilder(),
        workout_df=table,
        finalized=finalized,
    )

    summary = compute_summary(table, program_steps=steps)
    assert len(finalized.laps) == len(summary.laps), (
        f"FIT has {len(finalized.laps)} laps, summary has {len(summary.laps)}"
    )
    t0 = finalized.start_time_ms
    assert t0 is not None
    for seg, lap in zip(finalized.laps, summary.laps, strict=False):
        fit_start_s = (seg["start_time_ms"] - t0) / 1000.0
        fit_elapsed_s = (seg["end_time_ms"] - seg["start_time_ms"]) / 1000.0
        fit_duration_s = seg["timer_time_ms"] / 1000.0
        assert abs(fit_start_s - lap.start_s) < 1e-6, (fit_start_s, lap.start_s)
        assert abs(fit_elapsed_s - lap.elapsed_s) < 1e-6, (
            fit_elapsed_s,
            lap.elapsed_s,
        )
        assert abs(fit_duration_s - lap.duration_s) < 1e-6, (
            fit_duration_s,
            lap.duration_s,
        )
    print(f"  ok: FIT export and summary agree on {len(summary.laps)} identical laps")


def test_program_lap_is_clipped_to_active_signal() -> None:
    """One planned step groups active bursts but excludes inactive samples."""
    columns = COLUMNS
    actives = [0, 1, 1, 0, 1, 1, 0]
    distances = [0.0, 0.0, 3.0, 3.0, 6.0, 9.0, 9.0]
    rows = []
    for i, active in enumerate(actives):
        rows.append(
            {
                "timestamp": T0 + i * 1000,
                "heart_rate": 120 + i,
                "cadence": 70 if active else 0,
                "distance": distances[i],
                "speed": 3.0 if active else 0.0,
                "pull_force": 180 if active else 0,
                "active": active,
                "sample_duration": 1,
            }
        )
    steps = [
        {
            "type": "work",
            "label": "Work",
            "start_ms": T0 - 1000,
            "end_ms": T0 + 6000,
        }
    ]
    summary = compute_summary(Table(columns, rows), program_steps=steps)
    assert summary.duration_s == 4.0
    assert len(summary.laps) == 1
    assert summary.series_hr == [121.0, 122.0, 124.0, 125.0]
    assert summary.series_breaks == [True, False, False, False]
    assert summary.laps[0].avg_hr == 123.0
    assert summary.laps[0].avg_spm == 70.0
    print("  ok: planned lap groups active bursts without counting inactive time")


def test_sample_window_crossing_step_boundary_is_split_exactly() -> None:
    """A sensor window can contribute proportionally to two adjacent steps."""
    columns = COLUMNS
    table = Table(
        columns,
        [
            {
                "timestamp": T0 + 2000,
                "heart_rate": 150,
                "cadence": 80,
                "distance": 10.0,
                "speed": 5.0,
                "pull_force": 200,
                "active": 1,
                "sample_duration": 2.0,
            }
        ],
    )
    steps = [
        {"type": "work", "label": "A", "start_ms": T0, "end_ms": T0 + 1000},
        {
            "type": "rest",
            "label": "B",
            "start_ms": T0 + 1000,
            "end_ms": T0 + 2000,
        },
    ]

    finalized = finalize_workout(table, program_steps=steps)
    summary = compute_summary(table, program_steps=steps)

    assert [lap.record_weights_s for lap in finalized.laps] == [(1.0,), (1.0,)]
    assert [lap.distance_m for lap in summary.laps] == [5.0, 5.0]
    assert [lap.duration_s for lap in summary.laps] == [1.0, 1.0]
    assert sum(lap.distance_m for lap in summary.laps) == summary.distance_m
    assert sum(lap.duration_s for lap in summary.laps) == summary.duration_s


def test_centimetre_split_keeps_every_average_bounded_by_its_maximum() -> None:
    """FIT distance quantization may not produce an avg/max inversion."""
    table = Table(
        COLUMNS,
        [
            {
                "timestamp": T0 + 1000,
                "heart_rate": 150,
                "cadence": 80,
                "distance": 0.01,
                "speed": 0.01,
                "pull_force": 200,
                "active": 1,
                "sample_duration": 1.0,
            }
        ],
    )
    steps = [
        {"type": "work", "label": "A", "start_ms": T0, "end_ms": T0 + 500},
        {
            "type": "rest",
            "label": "B",
            "start_ms": T0 + 500,
            "end_ms": T0 + 1000,
        },
    ]

    summary = compute_summary(table, program_steps=steps)
    assert sum(lap.distance_m for lap in summary.laps) == summary.distance_m == 0.01
    assert all(
        lap.avg_speed_mps is None
        or (
            lap.max_speed_mps is not None
            and lap.avg_speed_mps <= lap.max_speed_mps
        )
        for lap in summary.laps
    )
    assert summary.avg_speed_mps is not None
    assert summary.max_speed_mps is not None
    assert summary.avg_speed_mps <= summary.max_speed_mps


def test_session_speed_is_weighted_lap_speed_and_raw_peak() -> None:
    columns = [*COLUMNS, "speed_instant"]
    rows = [
        {
            "timestamp": T0 + 1000,
            "heart_rate": 140,
            "cadence": 70,
            "distance": 4.0,
            "speed": 2.5,
            "speed_instant": 3.0,
            "pull_force": 180,
            "active": 1,
            "sample_duration": 1,
        },
        {
            "timestamp": T0 + 4000,
            "heart_rate": 140,
            "cadence": 70,
            "distance": 7.0,
            "speed": 2.5,
            "speed_instant": 3.0,
            "pull_force": 180,
            "active": 1,
            "sample_duration": 3,
        },
    ]
    steps = [
        {"type": "work", "label": "Fast", "start_ms": T0, "end_ms": T0 + 1000},
        {
            "type": "work",
            "label": "Easy",
            "start_ms": T0 + 1000,
            "end_ms": T0 + 4000,
        },
    ]

    summary = compute_summary(Table(columns, rows), program_steps=steps)

    # Speed is one coherent story from the distance channel: averages are
    # distance/time and peaks come from that same per-record series, never the
    # instantaneous speed_instant sensor (which reads 3.0 here and is ignored).
    # Lap 1 covers 4 m in 1 s (4.0 m/s), lap 2 covers 3 m in 3 s (1.0 m/s), and
    # the session maximum is the fastest canonical record/lap speed.
    assert [lap.avg_speed_mps for lap in summary.laps] == [4.0, 1.0]
    assert summary.avg_speed_mps == 1.75
    assert summary.avg_speed_mps == sum(
        (lap.avg_speed_mps or 0.0) * lap.active_time_s for lap in summary.laps
    ) / summary.active_time_s
    assert summary.max_speed_mps == 4.0
    # The hierarchy can never contradict itself: every peak is at least its own
    # average and is drawn from distance rather than the 3.0 sensor reading.
    lap_averages = [
        value
        for lap in summary.laps
        if (value := lap.avg_speed_mps) is not None
    ]
    lap_maxima = [
        value
        for lap in summary.laps
        if (value := lap.max_speed_mps) is not None
    ]
    assert len(lap_averages) == len(lap_maxima) == len(summary.laps)
    assert all(
        maximum >= average
        for average, maximum in zip(lap_averages, lap_maxima, strict=True)
    )
    assert summary.max_speed_mps == max(lap_maxima)
    assert summary.max_speed_mps != 3.0


def test_terminal_planned_step_owns_the_complete_recorded_tail() -> None:
    """Natural completion may land inside a sample with a packet already buffered."""
    rows = [
        {
            "timestamp": T0 + i * 1000,
            "heart_rate": 140,
            "cadence": 70,
            "distance": float(i * 3),
            "speed": 3.0,
            "pull_force": 180,
            "active": 1,
            "sample_duration": 1,
        }
        for i in range(1, 6)
    ]
    steps = [
        {
            "type": "work",
            "label": "10 m",
            "start_ms": T0,
            # A 10 m distance target crossed in the fourth 1-second sample.
            "end_ms": T0 + 3_333,
        }
    ]

    table = Table(COLUMNS, rows)
    finalized = finalize_workout(table, program_steps=steps)
    summary = compute_summary(table, program_steps=steps)

    assert finalized.laps[0].end_time_ms == T0 + 5_000
    assert finalized.laps[0].timer_time_ms == finalized.total_timer_time_ms == 5_000
    assert finalized.laps[0].distance_m == finalized.total_distance_m == 15.0
    assert summary.laps[0].duration_s == summary.duration_s == 5.0
    assert summary.laps[0].distance_m == summary.distance_m == 15.0


@pytest.mark.parametrize("resume_speed", [0.0, None, "invalid", float("nan")])
def test_resume_odometer_jump_is_not_credited_without_valid_speed(
    resume_speed: Any,
) -> None:
    """A resume packet without positive finite speed cannot justify an odometer jump."""
    distances = [3.0, 6.0, 6.0, 6.0, 18.0, 21.0]
    speeds: list[Any] = [3.0, 3.0, 0.0, 0.0, resume_speed, 3.0]
    actives = [1, 1, 0, 0, 1, 1]
    rows = [
        {
            "timestamp": T0 + i * 1000,
            "heart_rate": 140,
            "cadence": 70 if active else 0,
            "distance": distances[i],
            "speed": speeds[i],
            "pull_force": 180 if active else 0,
            "active": active,
            "sample_duration": 1,
        }
        for i, active in enumerate(actives)
    ]

    finalized = finalize_workout(Table(COLUMNS, rows))

    assert finalized.total_distance_m == 9.0
    assert finalized.record_cumulative_distance_m[4] == 6.0
    assert finalized.record_cumulative_distance_m[5] == 9.0


def test_meaningful_pause_breaks_chart_without_splitting_planned_lap() -> None:
    """A long pause remains visible inside its single planned step/lap."""
    columns = COLUMNS
    actives = [1, 1, 0, 0, 0, 0, 0, 1, 1]
    rows = []
    distance = 0.0
    for i, active in enumerate(actives):
        if active:
            distance += 3.0
        rows.append(
            {
                "timestamp": T0 + i * 1000,
                "heart_rate": 130 + i,
                "cadence": 70 if active else 0,
                "distance": distance,
                "speed": 3.0 if active else 0.0,
                "pull_force": 180 if active else 0,
                "active": active,
                "sample_duration": 1,
            }
        )
    steps = [
        {
            "type": "work",
            "label": "Long work",
            "start_ms": T0 - 1000,
            "end_ms": T0 + 8000,
        }
    ]
    summary = compute_summary(Table(columns, rows), program_steps=steps)
    assert summary.duration_s == 4.0
    assert len(summary.laps) == 1
    assert summary.series_breaks == [True, False, True, False]
    print("  ok: long pause breaks chart but planned step remains one lap")


def test_active_rest_stays_a_labelled_lap() -> None:
    """An athlete who keeps paddling hard through a 'rest' step really did move:
    that effort stays a distinct lap (honest data), but carries kind='rest' so
    the UI can label it instead of showing an anonymous interval."""
    b = _WorkoutBuilder()
    b.step("work", "Work 1:00", [(60, 3.0, 3.0)])
    b.step("rest", "Rest 0:30", [(30, 2.0, 2.0)])  # 58 m of real paddling
    b.step("work", "Work 1:00", [(60, 3.0, 3.0)])
    table, steps = b.build()

    s = compute_summary(table, program_steps=steps)
    assert len(s.laps) == 3
    assert s.laps[1].kind == "rest" and s.laps[1].label == "Rest 0:30"
    assert len(s.series_steps) == 3
    # Adjacent planned phases are shown as chart bands, not chopped into three
    # polylines. Only the first sample (or a real pause) starts a line.
    assert sum(s.series_breaks) == 1
    print("  ok: hard-paddled rest step kept as its own lap, labelled 'rest'")


def test_summary_keeps_motionless_rest_as_empty_planned_step() -> None:
    """The plan view keeps Rest, but inactive samples cannot lower averages."""
    columns = COLUMNS
    rows = []
    distance = 0.0
    actives = [1] * 10 + [0] * 5 + [1] * 10
    for i, active in enumerate(actives):
        if active:
            distance += 3.0
        rows.append(
            {
                "timestamp": T0 + i * 1000,
                "heart_rate": 140 if active else 100,
                "cadence": 70 if active else 0,
                "distance": distance,
                "speed": 3.0 if active else 0.0,
                "pull_force": 180 if active else 0,
                "active": active,
                "sample_duration": 1,
            }
        )
    steps = [
        {
            "index": 0,
            "type": "work",
            "label": "Work 1",
                "start_ms": T0 - 1000,
            "end_ms": T0 + 9000,
        },
        {
            "index": 1,
            "type": "rest",
            "label": "Rest",
            "start_ms": T0 + 9000,
            "end_ms": T0 + 14000,
        },
        {
            "index": 2,
            "type": "work",
            "label": "Work 2",
            "start_ms": T0 + 14000,
            "end_ms": T0 + 24000,
        },
    ]
    summary = compute_summary(Table(columns, rows), program_steps=steps)
    assert [lap.kind for lap in summary.laps] == ["work", "rest", "work"]
    rest = summary.laps[1]
    assert rest.duration_s == 0.0
    assert rest.elapsed_s == 5.0
    assert rest.avg_hr is None and rest.avg_power is None and rest.avg_spm is None
    from app.export_fit import FitExporter

    with tempfile.TemporaryDirectory() as directory:
        fit_path = Path(directory) / "planned-rest.fit"
        assert FitExporter({"log_level": "warning"}).export(
            Table(columns, rows), str(fit_path), program_steps=steps
        )
        assert fit_path.exists()
    print("  ok: pure rest remains visible with blank active-only averages")


def test_planned_rest_survives_a_recording_gap() -> None:
    """The durable plan, not presence of idle CSV packets, defines the steps."""
    columns = COLUMNS
    rows = [
        {
            "timestamp": T0 + offset,
            "heart_rate": 140,
            "cadence": 70,
            "distance": float(i * 3),
            "speed": 3.0,
            "pull_force": 180,
            "active": 1,
            "sample_duration": 1,
        }
        for i, offset in enumerate((0, 1000, 7000, 8000), start=1)
    ]
    steps = [
        {"type": "work", "label": "Work 1", "start_ms": T0 - 1000, "end_ms": T0 + 1000},
        {"type": "rest", "label": "Rest", "start_ms": T0 + 2000, "end_ms": T0 + 6000},
        {"type": "work", "label": "Work 2", "start_ms": T0 + 6000, "end_ms": T0 + 8000},
    ]
    summary = compute_summary(Table(columns, rows), program_steps=steps)
    assert [lap.kind for lap in summary.laps] == ["work", "rest", "work"]
    rest = summary.laps[1]
    assert rest.duration_s == 0.0 and rest.elapsed_s == 4.0
    assert rest.avg_hr is None
    print("  ok: planned rest remains visible even when no idle row was recorded")


def test_all_malformed_steps_are_rejected() -> None:
    b = _coast_down_workout()
    # Deliberately malformed timeline (wrong item types) to exercise the
    # skip-and-fall-back path; typed list[Any] so mypy accepts the bad input.
    bad_steps: list[Any] = [
        "nope",
        123,
        None,
        {"start_ms": "x", "end_ms": "y"},
    ]
    table, _steps = b.build()
    with pytest.raises(ValueError, match="invalid step"):
        compute_summary(table, program_steps=bad_steps)


def test_summary_rejects_partially_malformed_steps() -> None:
    b = _coast_down_workout()
    table, steps = b.build()
    bad_steps = list(steps)
    bad_steps.insert(1, "oops-not-a-dict")          # Case A
    bad_steps.append({"start_ms": "x", "end_ms": 5})  # Case B

    with pytest.raises(ValueError, match="invalid step"):
        compute_summary(table, program_steps=bad_steps)


def test_finalizer_rejects_partially_malformed_steps() -> None:
    b = _coast_down_workout()
    table, steps = b.build()
    bad_steps = list(steps)
    bad_steps.insert(1, "oops-not-a-dict")

    with pytest.raises(ValueError, match="invalid step"):
        finalize_workout(table, program_steps=bad_steps)


def test_free_workout_uses_activity_based_laps() -> None:
    b = _coast_down_workout()
    table, _steps = b.build()

    s = compute_summary(table)  # no program_steps at all
    assert all(lap.kind is None and lap.label is None for lap in s.laps)

    assert len(s.laps) == 2
    with pytest.raises(ValueError, match="non-empty"):
        compute_summary(table, program_steps=[])
