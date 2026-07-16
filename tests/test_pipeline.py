"""
End-to-end checks for the KayakFit data pipeline.

Run from the project root on a machine with the dependencies installed:

    python tests/test_pipeline.py

This avoids any Bluetooth / GUI dependency and exercises:
  * CSV -> FIT conversion produces a valid file
  * a malformed CSV fails gracefully (no SystemExit / thread kill)
  * runtime config validation raises ConfigError instead of sys.exit
"""

import contextlib
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

# Allow running directly from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fit_tool.fit_file import FitFile
from fit_tool.profile.profile_type import Activity, Event, EventType

from app.export_fit import (
    _GPS_COURSE_END,
    _GPS_COURSE_START,
    FitExporter,
    _synthetic_gps_position,
)
from app.field_mapping import ALL_FIELDS
from app.read_csv import CsvReader, CsvReadError
from app.summary import compute_summary
from app.table import Table
from app.workout_metadata import load_workout_config, save_workout_metadata
from app.workout_paths import WorkoutPaths

CONFIG = {
    "log_level": "warning",
    "data_points": "instant",
    "pull_length_m": 0.623,
    "autopause_stationary_threshold_records": 5,
    "autopause_gap_seconds": 5,
    "max_hr": 185,
    "hr_zone_mode": "auto",
    "hr_zones": [],
}


def _row(i: int) -> dict[str, Any]:
    """Build one synthetic data record (moving, increasing distance)."""
    base_ts = 1_700_000_000_000  # fixed ms epoch for determinism
    return {
        "timestamp": base_ts + i * 1000,
        "heart_rate__bpm": 120 + (i % 20),
        "kayakfirst_timestamp": base_ts + i * 1000,
        "col_2": 0, "col_3": 0,
        "col_4": "0.00", "col_5": "0.00",
        "col_6": 0, "active_paddling": 1, "col_8": 0,
        "distance__m": f"{i * 3.2:.2f}",
        "speed__mps": "3.20",
        "speed_instant__mps": "3.20",
        "cadence__spm": 80, "cadence_instant__spm": 80,
        "pace_200m__s": 62, "pace_200m_instant__s": 62,
        "pace_500m__s": 156, "pace_500m_instant__s": 156,
        "pace_1000m__s": 312, "pace_1000m_instant__s": 312,
        "pull_force__n": 200, "pull_force_instant__n": 200,
        "elapsed_time__s": i,
        "window_size__s": 1,
    }


def _write_csv(path: Path, rows: int, header: bool = True, full_schema: bool = True) -> None:
    fields = ALL_FIELDS if full_schema else ALL_FIELDS[:5]
    lines = []
    if header:
        lines.append(";".join(fields))
    for i in range(rows):
        record = _row(i)
        lines.append(";".join(str(record[f]) for f in fields))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    save_workout_metadata(path, CONFIG, "free")


def test_csv_to_fit_produces_file() -> None:
    with tempfile.TemporaryDirectory() as d:
        csv_path = Path(d) / "workout.csv"
        _write_csv(csv_path, rows=120)
        result = FitExporter.convert_csv_to_fit(str(csv_path), CONFIG)
        assert result["success"], f"conversion failed: {result.get('message')}"
        fit_path = Path(result["file_path"])
        assert fit_path.exists(), "FIT file was not created"
        assert fit_path.stat().st_size > 200, "FIT file is suspiciously small"
        print(f"  ok: produced {fit_path.name} ({fit_path.stat().st_size} bytes)")


def _fit_data_messages(path: Path, message: str) -> list[dict[str, Any]]:
    """Decode fit_tool row output into dictionaries for one message type."""
    decoded: list[dict[str, Any]] = []
    for row in FitFile.from_file(str(path)).to_rows():
        if len(row) < 3 or row[0] != "Data" or row[2] != message:
            continue
        decoded.append({row[i]: row[i + 1] for i in range(3, len(row), 3)})
    return decoded


def test_synthetic_gps_position_follows_out_and_back_course() -> None:
    """Each 2 km leg alternates between the fixed course endpoints."""
    assert _synthetic_gps_position(0.0) == _GPS_COURSE_START
    assert _synthetic_gps_position(2000.0) == _GPS_COURSE_END
    assert _synthetic_gps_position(4000.0) == _GPS_COURSE_START
    assert _synthetic_gps_position(6000.0) == _GPS_COURSE_END


def test_decoded_fit_exactly_matches_summary() -> None:
    """Compare final encoded FIT session/laps with the rendered summary model."""
    with tempfile.TemporaryDirectory() as d:
        csv_path = Path(d) / "paused.csv"
        rows = [_row(i) for i in range(10)]
        # Raw cumulative distance advances during the inactive period. The
        # finalized FIT record domain must rebase it so those ten metres do not
        # leak into the session, chart, or downstream platform totals.
        distances = [10.0, 13.0, 16.0, 18.0, 20.0, 22.0, 24.0, 26.0, 29.0, 32.0]
        rows[0]["window_size__s"] = 2
        for i, row in enumerate(rows):
            row["distance__m"] = f"{distances[i]:.2f}"
            if 3 <= i <= 7:
                row["active_paddling"] = 0
                row["speed__mps"] = row["speed_instant__mps"] = "0.00"
                row["cadence__spm"] = row["cadence_instant__spm"] = 0
                row["pull_force__n"] = row["pull_force_instant__n"] = 0
                row["heart_rate__bpm"] = 200
        _write_cols = [";".join(ALL_FIELDS)]
        _write_cols.extend(";".join(str(row[field]) for field in ALL_FIELDS) for row in rows)
        csv_path.write_text("\n".join(_write_cols) + "\n", encoding="utf-8")
        save_workout_metadata(csv_path, CONFIG, "free")

        table = CsvReader(str(csv_path), CONFIG).read_all("instant")
        summary = compute_summary(table, config=CONFIG)
        result = FitExporter.convert_csv_to_fit(str(csv_path), CONFIG)
        assert result["success"], result
        fit_path = Path(result["file_path"])
        session = _fit_data_messages(fit_path, "session")[0]
        activity = _fit_data_messages(fit_path, "activity")[0]
        laps = _fit_data_messages(fit_path, "lap")
        records = _fit_data_messages(fit_path, "record")
        sensor_records = [record for record in records if "heart_rate" in record]
        anchors = [record for record in records if "heart_rate" not in record]

        assert session["total_distance"] == summary.distance_m == 22.0
        assert session["total_timer_time"] == summary.duration_s == 6.0
        assert activity["total_timer_time"] == summary.duration_s
        assert activity["num_sessions"] == 1
        assert activity["type"] == Activity.MANUAL.value
        assert activity["event"] == Event.ACTIVITY.value
        assert activity["event_type"] == EventType.STOP.value
        assert session["num_laps"] == len(summary.laps) == 2
        assert session["avg_heart_rate"] == summary.avg_hr
        assert session["avg_cadence"] == summary.avg_spm
        assert session["avg_power"] == summary.avg_power
        assert session["max_heart_rate"] == summary.max_hr
        assert session["max_power"] == summary.max_power
        assert session["max_speed"] == summary.max_speed_mps
        assert abs(session["avg_speed"] - (summary.avg_speed_mps or 0.0)) < 0.001
        assert [record["distance"] for record in sensor_records] == summary.series_distance
        assert records[-1]["distance"] == session["total_distance"] == 22.0
        assert all("position_lat" in record for record in records)
        assert all("position_long" in record for record in records)
        assert [record["distance"] for record in anchors] == [0.0, 16.0]

        anchor_indices = [
            index for index, record in enumerate(records) if "heart_rate" not in record
        ]
        gps_spans = []
        for ordinal, start_index in enumerate(anchor_indices):
            end_index = (
                anchor_indices[ordinal + 1] - 1
                if ordinal + 1 < len(anchor_indices)
                else len(records) - 1
            )
            gps_spans.append(
                (records[end_index]["timestamp"] - records[start_index]["timestamp"])
                / 1000.0
            )
        assert gps_spans == [4.0, 2.0]
        assert sum(gps_spans) == session["total_timer_time"]

        # GPS is derived from the rebased active-only distance, not the raw
        # odometer, whose inactive jump would place the final point at 32 m.
        for record in records:
            expected_latitude, expected_longitude = _synthetic_gps_position(
                record["distance"]
            )
            assert abs(record["position_lat"] - expected_latitude) < 1e-6
            assert abs(record["position_long"] - expected_longitude) < 1e-6

        assert [lap["total_distance"] for lap in laps] == [
            item.distance_m for item in summary.laps
        ]
        assert [lap["total_timer_time"] for lap in laps] == [
            item.duration_s for item in summary.laps
        ]
        assert [lap["avg_heart_rate"] for lap in laps] == [
            item.avg_hr for item in summary.laps
        ]
        assert [lap.get("avg_speed") for lap in laps] == [
            item.avg_speed_mps for item in summary.laps
        ]
        assert summary.series_t[0] == 0.0
        assert summary.series_breaks == [True, False, False, True, False]


def test_resumed_distance_jump_keeps_fit_and_summary_splits_consistent() -> None:
    """A delayed odometer update may not inflate the resumed FIT lap."""
    with tempfile.TemporaryDirectory() as d:
        csv_path = Path(d) / "resume-jump.csv"
        rows = [_row(i) for i in range(6)]
        distances = [3.0, 6.0, 6.0, 6.0, 18.0, 21.0]
        for i, row in enumerate(rows):
            row["distance__m"] = f"{distances[i]:.2f}"
            row["speed__mps"] = row["speed_instant__mps"] = "3.00"
            if i in (2, 3):
                row["active_paddling"] = 0
                row["speed__mps"] = row["speed_instant__mps"] = "0.00"
                row["cadence__spm"] = row["cadence_instant__spm"] = 0
                row["pull_force__n"] = row["pull_force_instant__n"] = 0
            if i >= 4:
                row["timestamp"] = int(row["timestamp"]) + 6000
        lines = [";".join(ALL_FIELDS)]
        lines.extend(";".join(str(row[field]) for field in ALL_FIELDS) for row in rows)
        csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        save_workout_metadata(csv_path, CONFIG, "free")

        table = CsvReader(str(csv_path), CONFIG).read_all("instant")
        summary = compute_summary(table, config=CONFIG)
        result = FitExporter.convert_csv_to_fit(str(csv_path), CONFIG)
        assert result["success"], result
        fit_path = Path(result["file_path"])
        laps = _fit_data_messages(fit_path, "lap")
        records = _fit_data_messages(fit_path, "record")
        sensor_records = [record for record in records if "heart_rate" in record]
        anchors = [record for record in records if "heart_rate" not in record]

        assert [item.distance_m for item in summary.laps] == [6.0, 6.0]
        assert [item.avg_speed_mps for item in summary.laps] == [3.0, 3.0]
        assert [lap["total_distance"] for lap in laps] == [6.0, 6.0]
        assert [lap["avg_speed"] for lap in laps] == [3.0, 3.0]
        assert [record["distance"] for record in sensor_records] == [3.0, 6.0, 9.0, 12.0]
        assert [record["distance"] for record in anchors] == [0.0, 6.0]
        assert all("position_lat" in record for record in records)
        assert all("position_long" in record for record in records)


def test_active_signal_excludes_heart_rate_prefix_and_tail() -> None:
    """Only active-paddling sample windows may become FIT/summary records."""
    with tempfile.TemporaryDirectory() as d:
        csv_path = Path(d) / "hr-tail.csv"
        rows = [_row(i) for i in range(7)]
        actives = [0, 0, 1, 1, 1, 0, 0]
        distances = [0.0, 0.0, 0.0, 3.0, 6.0, 6.0, 6.0]
        heart_rates = [90, 95, 120, 125, 130, 180, 175]
        for i, row in enumerate(rows):
            row["active_paddling"] = actives[i]
            row["distance__m"] = f"{distances[i]:.2f}"
            row["heart_rate__bpm"] = heart_rates[i]
            if not actives[i]:
                row["speed__mps"] = row["speed_instant__mps"] = "0.00"
                row["cadence__spm"] = row["cadence_instant__spm"] = 0
                row["pull_force__n"] = row["pull_force_instant__n"] = 0
        # The middle active row represents two ergometer seconds. Timer time
        # must follow these device windows, not app receipt timestamp deltas.
        rows[3]["window_size__s"] = 2
        for i in range(3, len(rows)):
            rows[i]["timestamp"] = int(rows[i]["timestamp"]) + 1000
        lines = [";".join(ALL_FIELDS)]
        lines.extend(";".join(str(row[field]) for field in ALL_FIELDS) for row in rows)
        csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        save_workout_metadata(csv_path, CONFIG, "free")

        table = CsvReader(str(csv_path), CONFIG).read_all("instant")
        summary = compute_summary(table, config=CONFIG)
        result = FitExporter.convert_csv_to_fit(str(csv_path), CONFIG)
        assert result["success"], result
        fit_path = Path(result["file_path"])
        session = _fit_data_messages(fit_path, "session")[0]
        records = _fit_data_messages(fit_path, "record")
        sensor_records = [record for record in records if "heart_rate" in record]

        assert summary.duration_s == session["total_timer_time"] == 4.0
        assert summary.series_hr == [120.0, 125.0, 130.0]
        assert summary.series_t[0] == 0.0
        assert summary.series_breaks == [True, False, False]
        assert summary.max_hr == session["max_heart_rate"] == 130
        assert [record["heart_rate"] for record in sensor_records] == [120, 125, 130]


def test_decoded_planned_laps_match_active_only_step_summary() -> None:
    """Planned FIT laps retain empty rest and exactly mirror step statistics."""
    with tempfile.TemporaryDirectory() as d:
        fit_path = Path(d) / "planned.fit"
        rows = [_row(i) for i in range(15)]
        distances = [3.0 * (i + 1) if i < 5 else 15.0 for i in range(15)]
        for i in range(10, 15):
            distances[i] = 18.0 + 3.0 * (i - 10)
        for i, row in enumerate(rows):
            row["distance__m"] = f"{distances[i]:.2f}"
            if 5 <= i < 10:
                row["active_paddling"] = 0
                row["heart_rate__bpm"] = 200
                row["speed__mps"] = row["speed_instant__mps"] = "0.00"
                row["cadence__spm"] = row["cadence_instant__spm"] = 0
                row["pull_force__n"] = row["pull_force_instant__n"] = 0
        csv_path = Path(d) / "planned.csv"
        lines = [";".join(ALL_FIELDS)]
        lines.extend(";".join(str(row[field]) for field in ALL_FIELDS) for row in rows)
        csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        table = CsvReader(str(csv_path), CONFIG).read_all("instant")
        t0 = int(rows[0]["timestamp"])
        steps = [
            {
                "type": "work",
                "label": "Work 1",
                "start_ms": t0 - 1000,
                "end_ms": t0 + 4000,
            },
            {
                "type": "rest",
                "label": "Rest",
                "start_ms": t0 + 4000,
                "end_ms": t0 + 9000,
            },
            {
                "type": "work",
                "label": "Work 2",
                "start_ms": t0 + 9000,
                "end_ms": t0 + 14000,
            },
        ]
        summary = compute_summary(table, program_steps=steps, config=CONFIG)
        assert FitExporter(CONFIG).export(table, str(fit_path), program_steps=steps)
        laps = _fit_data_messages(fit_path, "lap")

        assert len(laps) == len(summary.laps) == 3
        assert [lap["total_timer_time"] for lap in laps] == [
            item.duration_s for item in summary.laps
        ]
        assert [lap["total_elapsed_time"] for lap in laps] == [
            item.elapsed_s for item in summary.laps
        ]
        assert [lap["total_distance"] for lap in laps] == [
            item.distance_m for item in summary.laps
        ]
        assert [lap.get("avg_heart_rate") for lap in laps] == [
            item.avg_hr for item in summary.laps
        ]
        assert [lap.get("avg_speed") for lap in laps] == [
            item.avg_speed_mps for item in summary.laps
        ]
        assert summary.laps[1].duration_s == 0.0
        assert summary.laps[1].avg_hr is None
        assert summary.max_hr != 200


def test_export_accepts_a_workout_longer_than_72_minutes() -> None:
    """FIT duration fields accept seconds, so long sessions cannot overflow."""
    base = 1_700_000_000_000
    rows = [
        {
            "timestamp": base + 4_320_000,
            "heart_rate": 140,
            "cadence": 70,
            "distance": 12_960.0,
            "speed": 3.0,
            "pull_force": 180,
            "active": 1,
            "sample_duration": 4_320,
        },
        {
            "timestamp": base + 4_321_000,
            "heart_rate": 140,
            "cadence": 70,
            "distance": 12_963.0,
            "speed": 3.0,
            "pull_force": 180,
            "active": 1,
            "sample_duration": 1,
        },
    ]
    table = Table(
        [
            "timestamp",
            "heart_rate",
            "cadence",
            "distance",
            "speed",
            "pull_force",
            "active",
            "sample_duration",
        ],
        rows,
    )

    with tempfile.TemporaryDirectory() as directory:
        fit_path = Path(directory) / "long.fit"
        assert FitExporter(CONFIG).export(table, str(fit_path))
        session = _fit_data_messages(fit_path, "session")[0]
        activity = _fit_data_messages(fit_path, "activity")[0]
        laps = _fit_data_messages(fit_path, "lap")

    assert session["total_timer_time"] == 4_321.0
    assert session["total_elapsed_time"] == 4_321.0
    assert activity["total_timer_time"] == 4_321.0
    assert [lap["total_timer_time"] for lap in laps] == [4_321.0]


def test_export_uses_recorded_processing_config() -> None:
    """Historical FIT generation must not change with the current app config."""
    with tempfile.TemporaryDirectory() as d:
        csv_path = Path(d) / "snapshot.csv"
        _write_csv(csv_path, rows=10)
        recorded = {**CONFIG, "pull_length_m": 0.8}
        save_workout_metadata(csv_path, recorded, "free")

        current = {**CONFIG, "pull_length_m": 0.4}
        assert load_workout_config(csv_path)["pull_length_m"] == 0.8
        result = FitExporter.convert_csv_to_fit(str(csv_path), current)
        assert result["success"], result
        session = _fit_data_messages(Path(result["file_path"]), "session")[0]
        # 200 N * 0.8 m * 80 spm / 60 = 213.33 -> estimate_power rounds to 213 W.
        assert session["avg_power"] == 213


def test_failed_export_preserves_existing_fit() -> None:
    """A failed replacement must not delete the last valid FIT file."""
    with tempfile.TemporaryDirectory() as d:
        csv_path = Path(d) / "atomic.csv"
        fit_path = WorkoutPaths.from_csv(csv_path).fit
        _write_csv(csv_path, rows=10)
        fit_path.write_bytes(b"previous-valid-fit")
        table = CsvReader(str(csv_path), CONFIG).read_all("instant")

        class _FailingFit:
            def to_file(self, path: str) -> None:
                raise OSError("simulated full disk")

        class _Builder:
            def __init__(self, **_kwargs: Any) -> None:
                pass

            def add(self, message: Any = None) -> None:
                pass

            def build(self) -> _FailingFit:
                return _FailingFit()

        with patch("app.export_fit.FitFileBuilder", _Builder):
            success = FitExporter(CONFIG).export(
                table,
                str(fit_path),
            )
        assert not success
        assert fit_path.read_bytes() == b"previous-valid-fit"


def test_stationary_recording_does_not_become_activity() -> None:
    with tempfile.TemporaryDirectory() as d:
        csv_path = Path(d) / "stationary.csv"
        rows = [_row(i) for i in range(10)]
        for row in rows:
            row["distance__m"] = "0.00"
            row["speed__mps"] = row["speed_instant__mps"] = "0.00"
            row["active_paddling"] = 0
        lines = [";".join(ALL_FIELDS)]
        lines.extend(";".join(str(row[field]) for field in ALL_FIELDS) for row in rows)
        csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        result = FitExporter.convert_csv_to_fit(str(csv_path), CONFIG)
        assert not result["success"]
        assert not WorkoutPaths.from_csv(csv_path).fit.exists()


def test_malformed_csv_fails_gracefully() -> None:
    with tempfile.TemporaryDirectory() as d:
        csv_path = Path(d) / "bad.csv"
        _write_csv(csv_path, rows=10, full_schema=False)  # missing columns
        # Direct reader call should raise the typed error (not SystemExit).
        try:
            CsvReader(str(csv_path), CONFIG).read_all()
            raised = False
        except CsvReadError:
            raised = True
        except SystemExit as err:
            raise AssertionError("read_all raised SystemExit instead of CsvReadError") from err
        assert raised, "expected CsvReadError for malformed CSV"

        # And the public conversion API turns it into a clean failure result.
        result = FitExporter.convert_csv_to_fit(str(csv_path), CONFIG)
        assert not result["success"]
        print(f"  ok: malformed CSV handled -> {result.get('message')!r}")


def test_runtime_config_validation() -> None:
    from gui.config_manager import ConfigError, ConfigManager

    mgr = ConfigManager.__new__(ConfigManager)  # bypass home-dir side effects
    mgr.config_file = Path(tempfile.gettempdir()) / "kayakfit_nonexistent.yml"
    with contextlib.suppress(OSError):
        mgr.config_file.unlink()
    try:
        mgr.load_runtime_config()
        raise AssertionError("expected ConfigError when no ergometer is configured")
    except ConfigError as e:
        print(f"  ok: runtime config rejected -> {e}")
