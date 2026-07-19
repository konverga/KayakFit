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
import json
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

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
from app.field_mapping import CSV_COLUMNS
from app.read_csv import CsvReader, CsvReadError
from app.summary import compute_summary
from app.table import Table
from app.workout_metadata import load_workout_config, save_workout_metadata
from app.workout_paths import WorkoutPaths

CONFIG = {
    "log_level": "warning",
    "pull_length_m": 0.623,
    "max_hr": 185,
    "hr_zone_mode": "auto",
    "hr_zones": [],
}


def test_recorded_free_workout_golden_metrics() -> None:
    """Verify free-workout timing and distance against a fixed sensor sample set."""
    elapsed_groups = [
        list(range(1, 18)),
        [18, 20, 21, 22, 23, 24],
        list(range(25, 38)),
        [38, 40, 41, 42, 43, 44, 45],
        list(range(46, 55)),
        [55, 56, 57, 58, 60, 61, 62],
    ]
    distance_groups = [
        [1, 3, 6, 8, 13, 16, 19, 21, 24, 27, 29, 32, 34, 39, 42, 44, 47],
        [47] * 6,
        [53, 56, 59, 63, 65, 68, 70, 74, 76, 78, 81, 85, 87],
        [87] * 7,
        [94, 97, 100, 104, 107, 109, 113, 114, 117],
        [117] * 7,
    ]
    speed_groups = [
        [0.72, 1.19, 1.54, 2.07, 2.77, 2.84, 2.77, 2.80, 2.73,
         2.74, 2.66, 2.72, 2.70, 2.76, 2.81, 2.78, 2.83],
        [0.0] * 6,
        [2.40, 2.43, 2.48, 2.41, 2.73, 2.69, 2.60, 2.61, 2.65,
         2.56, 2.63, 2.64, 2.65],
        [0.0] * 7,
        [2.37, 2.34, 2.37, 2.35, 2.68, 2.70, 2.54, 2.51, 2.53],
        [0.0] * 7,
    ]
    active_groups = [1, 0, 1, 0, 1, 0]
    rows: list[dict[str, Any]] = []
    previous_end_s = 0
    for elapsed, distances, speeds, active in zip(
        elapsed_groups, distance_groups, speed_groups, active_groups, strict=True
    ):
        for end_s, distance_m, speed_mps in zip(
            elapsed, distances, speeds, strict=True
        ):
            rows.append(
                {
                    "timestamp": 1_784_314_902_544 + end_s * 1000,
                    "session_elapsed": end_s,
                    "sample_duration": end_s - previous_end_s,
                    "heart_rate": 120,
                    "cadence": 60 if active else 0,
                    "cadence_instant": 60 if active else 0,
                    "distance": distance_m,
                    "speed_instant": speed_mps,
                    "pull_force": 100 if active else 0,
                    "pull_force_instant": 100 if active else 0,
                    "active": active,
                }
            )
            previous_end_s = end_s
    columns = list(rows[0])
    summary = compute_summary(Table(columns, rows), pull_length_m=CONFIG["pull_length_m"])

    assert summary.active_time_s == 39.0
    assert summary.elapsed_time_s == 62.0
    assert summary.pause_time_s == 23.0
    assert summary.distance_m == 117.0
    assert [lap.start_s for lap in summary.laps] == [0.0, 24.0, 45.0]
    assert [lap.active_time_s for lap in summary.laps] == [17.0, 13.0, 9.0]
    assert [lap.distance_m for lap in summary.laps] == [47.0, 40.0, 30.0]
    assert [round((lap.avg_speed_mps or 0.0) * 3.6, 2) for lap in summary.laps] == [
        8.77, 9.27, 8.96
    ]
    assert round((summary.max_speed_mps or 0.0) * 3.6, 2) == 10.22
    assert summary.series_time_bounds == (0.0, 62.0)
    assert sum(lap.active_time_s for lap in summary.laps) == summary.active_time_s
    assert sum(lap.distance_m for lap in summary.laps) == summary.distance_m


def test_canonical_fields_own_intervals() -> None:
    """Explicit session endpoints and durations own the processing clock."""
    rows = [
        {
            "timestamp": 1_700_000_000_000 + elapsed * 1000,
            "session_elapsed": elapsed,
            "heart_rate": 120,
            "cadence": 60 if active else 0,
            "cadence_instant": 60 if active else 0,
            "distance": distance,
            "speed_instant": 2.0 if active else 0.0,
            "pull_force": 100 if active else 0,
            "pull_force_instant": 100 if active else 0,
            "active": active,
            "sample_duration": elapsed - (0 if index == 0 else (2, 5, 6)[index - 1]),
        }
        for index, (elapsed, active, distance) in enumerate(
            ((2, 0, 0.0), (5, 1, 6.0), (6, 1, 8.0))
        )
    ]

    summary = compute_summary(
        Table(list(rows[0]), rows), pull_length_m=CONFIG["pull_length_m"]
    )

    assert summary.elapsed_time_s == 6.0
    assert summary.active_time_s == 4.0
    assert summary.pause_time_s == 2.0
    assert summary.series_t == [5.0, 6.0]
    assert summary.series_time_bounds == (0.0, 6.0)
    assert summary.laps[0].start_s == 2.0
    assert summary.laps[0].elapsed_time_s == 4.0
    assert summary.laps[0].distance_m == summary.distance_m == 8.0


def _row(i: int) -> dict[str, Any]:
    """Build one synthetic data record (moving, increasing distance)."""
    base_ts = 1_700_000_000_000  # fixed ms epoch for determinism
    return {
        "timestamp": base_ts + i * 1000,
        "session_elapsed__s": i + 1,
        "sample_duration__s": 1,
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
        "elapsed_time__s": i + 1,
        "window_size__s": 1,
    }


def _write_csv(path: Path, rows: int, header: bool = True, full_schema: bool = True) -> None:
    fields = CSV_COLUMNS if full_schema else CSV_COLUMNS[:5]
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
        result = FitExporter.convert_csv_to_fit(str(csv_path))
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
        rows[0]["sample_duration__s"] = 1
        for i, row in enumerate(rows):
            row["distance__m"] = f"{distances[i]:.2f}"
            if 3 <= i <= 7:
                row["active_paddling"] = 0
                row["speed__mps"] = row["speed_instant__mps"] = "0.00"
                row["cadence__spm"] = row["cadence_instant__spm"] = 0
                row["pull_force__n"] = row["pull_force_instant__n"] = 0
                row["heart_rate__bpm"] = 200
        _write_cols = [";".join(CSV_COLUMNS)]
        _write_cols.extend(
            ";".join(str(row[field]) for field in CSV_COLUMNS) for row in rows
        )
        csv_path.write_text("\n".join(_write_cols) + "\n", encoding="utf-8")
        save_workout_metadata(csv_path, CONFIG, "free")

        table = CsvReader(str(csv_path)).read_all()
        summary = compute_summary(table, pull_length_m=CONFIG["pull_length_m"])
        result = FitExporter.convert_csv_to_fit(str(csv_path))
        assert result["success"], result
        fit_path = Path(result["file_path"])
        session = _fit_data_messages(fit_path, "session")[0]
        activity = _fit_data_messages(fit_path, "activity")[0]
        laps = _fit_data_messages(fit_path, "lap")
        records = _fit_data_messages(fit_path, "record")
        sensor_records = [record for record in records if "heart_rate" in record]
        anchors = [record for record in records if "heart_rate" not in record]
        active_rows = [row for row in rows if row["active_paddling"] == 1]
        origin_ms = int(rows[0]["timestamp"]) - round(
            float(rows[0]["session_elapsed__s"]) * 1000
        )

        assert session["total_distance"] == summary.distance_m == 22.0
        assert session["total_timer_time"] == summary.active_time_s == 5.0
        assert session["total_elapsed_time"] == summary.elapsed_time_s == 10.0
        assert summary.pause_time_s == 5.0
        assert sum(
            lap.elapsed_time_s + lap.pause_after_s for lap in summary.laps
        ) == summary.elapsed_time_s
        assert activity["total_timer_time"] == summary.active_time_s
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
        assert [record["timestamp"] for record in sensor_records] == [
            row["timestamp"] for row in active_rows
        ]
        assert summary.series_t == [
            (int(row["timestamp"]) - origin_ms) / 1000.0 for row in active_rows
        ]
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
        assert gps_spans == [3.0, 2.0]
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
            item.active_time_s for item in summary.laps
        ]
        assert [lap["avg_heart_rate"] for lap in laps] == [
            item.avg_hr for item in summary.laps
        ]
        assert [lap.get("avg_speed") for lap in laps] == [
            item.avg_speed_mps for item in summary.laps
        ]
        assert [lap.get("max_speed") for lap in laps] == [
            item.max_speed_mps for item in summary.laps
        ]
        assert summary.series_t[0] == 1.0
        assert summary.series_breaks == [True, False, False, True, False]


def test_speed_statistics_and_fit_records_use_instantaneous_sensor() -> None:
    """Summary, graph and FIT all retain the recorded speed channel."""
    with tempfile.TemporaryDirectory() as d:
        csv_path = Path(d) / "workout.csv"
        config = dict(CONFIG)
        rows = []
        cumulative = 0.0
        for i in range(10):
            cumulative += 2.0 if i < 5 else 4.0
            row = _row(i)
            row["distance__m"] = f"{cumulative:.2f}"
            row["speed__mps"] = "3.00"
            row["speed_instant__mps"] = "9.00" if i == 7 else "3.00"
            rows.append(row)
        lines = [";".join(CSV_COLUMNS)]
        lines.extend(";".join(str(row[field]) for field in CSV_COLUMNS) for row in rows)
        csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        save_workout_metadata(csv_path, config, "free")

        table = CsvReader(str(csv_path)).read_all()
        summary = compute_summary(table, pull_length_m=config["pull_length_m"])
        result = FitExporter.convert_csv_to_fit(str(csv_path))
        assert result["success"], result

        session = _fit_data_messages(Path(result["file_path"]), "session")[0]
        laps = _fit_data_messages(Path(result["file_path"]), "lap")
        records = _fit_data_messages(Path(result["file_path"]), "record")
        sensor_speeds = [record["speed"] for record in records if "speed" in record]

        assert summary.avg_speed_mps == 3.6
        assert summary.max_speed_mps == 9.0
        assert max(sensor_speeds) == summary.max_speed_mps
        assert sensor_speeds == [3.0] * 7 + [9.0] + [3.0] * 2

        # Summary and FIT session agree, and the FIT file is self-consistent.
        assert session["avg_speed"] == summary.avg_speed_mps
        assert session["max_speed"] == summary.max_speed_mps
        assert max(sensor_speeds) <= session["max_speed"]
        assert all(lap["max_speed"] >= lap["avg_speed"] for lap in laps)
        assert session["max_speed"] >= session["avg_speed"]
        assert session["max_speed"] >= max(lap["max_speed"] for lap in laps)


def test_instant_metrics_and_averaged_power_use_separate_channels() -> None:
    """Summary/FIT use instant points except calibrated power inputs."""
    with tempfile.TemporaryDirectory() as directory:
        csv_path = Path(directory) / "metric-sources.csv"
        rows = []
        for i, instant_cadence in enumerate((70, 90, 110)):
            row = _row(i)
            row["cadence__spm"] = 60
            row["cadence_instant__spm"] = instant_cadence
            row["pull_force__n"] = 100
            row["pull_force_instant__n"] = 200 + i * 50
            rows.append(row)
        lines = [";".join(CSV_COLUMNS)]
        lines.extend(
            ";".join(str(row[field]) for field in CSV_COLUMNS) for row in rows
        )
        csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        save_workout_metadata(csv_path, CONFIG, "free")

        table = CsvReader(str(csv_path)).read_all()
        summary = compute_summary(table, pull_length_m=CONFIG["pull_length_m"])
        result = FitExporter.convert_csv_to_fit(str(csv_path))
        assert result["success"], result

        records = _fit_data_messages(Path(result["file_path"]), "record")
        sensor_records = [record for record in records if "cadence" in record]
        session = _fit_data_messages(Path(result["file_path"]), "session")[0]

        assert summary.series_spm == [70.0, 90.0, 110.0]
        assert summary.series_pull == [200.0, 250.0, 300.0]
        assert summary.avg_spm == 90.0
        assert [record["cadence"] for record in sensor_records] == [70, 90, 110]
        # 100 N * 0.623 m * 60 spm / 60 rounds to 62 W at every point.
        assert summary.series_power == [62.0, 62.0, 62.0]
        assert summary.avg_power == 62.0
        assert [record["power"] for record in sensor_records] == [62, 62, 62]
        assert session["avg_cadence"] == 90
        assert session["avg_power"] == 62


def test_resumed_distance_jump_is_preserved_consistently() -> None:
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
                row["session_elapsed__s"] = int(row["session_elapsed__s"]) + 6
        lines = [";".join(CSV_COLUMNS)]
        lines.extend(";".join(str(row[field]) for field in CSV_COLUMNS) for row in rows)
        csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        save_workout_metadata(csv_path, CONFIG, "free")

        table = CsvReader(str(csv_path)).read_all()
        summary = compute_summary(table, pull_length_m=CONFIG["pull_length_m"])
        result = FitExporter.convert_csv_to_fit(str(csv_path))
        assert result["success"], result
        fit_path = Path(result["file_path"])
        laps = _fit_data_messages(fit_path, "lap")
        records = _fit_data_messages(fit_path, "record")
        sensor_records = [record for record in records if "heart_rate" in record]
        anchors = [record for record in records if "heart_rate" not in record]

        assert [item.distance_m for item in summary.laps] == [6.0, 15.0]
        assert [item.avg_speed_mps for item in summary.laps] == [3.0, 3.0]
        assert [lap["total_distance"] for lap in laps] == [6.0, 15.0]
        assert [lap["avg_speed"] for lap in laps] == [3.0, 3.0]
        assert [record["distance"] for record in sensor_records] == [3.0, 6.0, 18.0, 21.0]
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
        # Raw device timing is diagnostic and cannot change canonical timing.
        rows[3]["window_size__s"] = 2
        lines = [";".join(CSV_COLUMNS)]
        lines.extend(";".join(str(row[field]) for field in CSV_COLUMNS) for row in rows)
        csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        save_workout_metadata(csv_path, CONFIG, "free")

        table = CsvReader(str(csv_path)).read_all()
        summary = compute_summary(table, pull_length_m=CONFIG["pull_length_m"])
        result = FitExporter.convert_csv_to_fit(str(csv_path))
        assert result["success"], result
        fit_path = Path(result["file_path"])
        session = _fit_data_messages(fit_path, "session")[0]
        records = _fit_data_messages(fit_path, "record")
        sensor_records = [record for record in records if "heart_rate" in record]

        assert summary.active_time_s == session["total_timer_time"] == 3.0
        assert summary.elapsed_time_s == session["total_elapsed_time"] == 7.0
        assert summary.series_hr == [120.0, 125.0, 130.0]
        assert summary.series_t[0] == 3.0
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
        lines = [";".join(CSV_COLUMNS)]
        lines.extend(";".join(str(row[field]) for field in CSV_COLUMNS) for row in rows)
        csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        table = CsvReader(str(csv_path)).read_all()
        steps = [
            {
                "index": 0,
                "type": "work",
                "label": "Work 1",
                "start_elapsed_s": 0,
                "end_elapsed_s": 5,
            },
            {
                "index": 1,
                "type": "rest",
                "label": "Rest",
                "start_elapsed_s": 5,
                "end_elapsed_s": 10,
            },
            {
                "index": 2,
                "type": "work",
                "label": "Work 2",
                "start_elapsed_s": 10,
                "end_elapsed_s": 15,
            },
        ]
        summary = compute_summary(
            table, program_steps=steps, pull_length_m=CONFIG["pull_length_m"]
        )
        assert FitExporter(CONFIG["pull_length_m"]).export(
            table, str(fit_path), program_steps=steps
        )
        laps = _fit_data_messages(fit_path, "lap")

        assert len(laps) == len(summary.laps) == 3
        assert [lap["total_timer_time"] for lap in laps] == [
            item.active_time_s for item in summary.laps
        ]
        assert [lap["total_elapsed_time"] for lap in laps] == [
            item.elapsed_time_s for item in summary.laps
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
        assert summary.laps[1].active_time_s == 0.0
        assert summary.laps[1].avg_hr is None
        assert summary.max_hr != 200


def test_export_accepts_a_workout_longer_than_72_minutes() -> None:
    """FIT duration fields accept seconds, so long sessions cannot overflow."""
    base = 1_700_000_000_000
    rows = [
        {
            "timestamp": base + 4_320_000,
            "session_elapsed": 4_320,
            "heart_rate": 140,
            "cadence": 70,
            "cadence_instant": 70,
            "distance": 12_960.0,
            "speed_instant": 3.0,
            "pull_force": 180,
            "pull_force_instant": 180,
            "active": 1,
            "sample_duration": 4_320,
        },
        {
            "timestamp": base + 4_321_000,
            "session_elapsed": 4_321,
            "heart_rate": 140,
            "cadence": 70,
            "cadence_instant": 70,
            "distance": 12_963.0,
            "speed_instant": 3.0,
            "pull_force": 180,
            "pull_force_instant": 180,
            "active": 1,
            "sample_duration": 1,
        },
    ]
    table = Table(
        [
            "timestamp",
            "session_elapsed",
            "heart_rate",
            "cadence",
            "cadence_instant",
            "distance",
            "speed_instant",
            "pull_force",
            "pull_force_instant",
            "active",
            "sample_duration",
        ],
        rows,
    )

    with tempfile.TemporaryDirectory() as directory:
        fit_path = Path(directory) / "long.fit"
        assert FitExporter(CONFIG["pull_length_m"]).export(table, str(fit_path))
        session = _fit_data_messages(fit_path, "session")[0]
        activity = _fit_data_messages(fit_path, "activity")[0]
        laps = _fit_data_messages(fit_path, "lap")

    assert session["total_timer_time"] == 4_321.0
    assert session["total_elapsed_time"] == 4_321.0
    assert activity["total_timer_time"] == 4_321.0
    assert [lap["total_timer_time"] for lap in laps] == [4_321.0]


def test_export_uses_recorded_processing_config() -> None:
    """FIT generation reads power calibration from recorded metadata."""
    with tempfile.TemporaryDirectory() as d:
        csv_path = Path(d) / "snapshot.csv"
        _write_csv(csv_path, rows=10)
        recorded = {**CONFIG, "pull_length_m": 0.8}
        save_workout_metadata(csv_path, recorded, "free")

        assert load_workout_config(csv_path)["pull_length_m"] == 0.8
        result = FitExporter.convert_csv_to_fit(str(csv_path))
        assert result["success"], result
        session = _fit_data_messages(Path(result["file_path"]), "session")[0]
        # 200 N * 0.8 m * 80 spm / 60 = 213.33 -> estimate_power rounds to 213 W.
        assert session["avg_power"] == 213


def test_metadata_rejects_removed_processing_keys() -> None:
    with tempfile.TemporaryDirectory() as directory:
        csv_path = Path(directory) / "strict.csv"
        save_workout_metadata(csv_path, CONFIG, "free")
        metadata_path = WorkoutPaths.from_csv(csv_path).metadata
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        payload["processing_config"]["data_points"] = "instant"
        metadata_path.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(ValueError, match="schema mismatch"):
            load_workout_config(csv_path)


def test_failed_export_preserves_existing_fit() -> None:
    """A failed replacement must not delete the last valid FIT file."""
    with tempfile.TemporaryDirectory() as d:
        csv_path = Path(d) / "atomic.csv"
        fit_path = WorkoutPaths.from_csv(csv_path).fit
        _write_csv(csv_path, rows=10)
        fit_path.write_bytes(b"previous-valid-fit")
        table = CsvReader(str(csv_path)).read_all()

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
            success = FitExporter(CONFIG["pull_length_m"]).export(
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
        lines = [";".join(CSV_COLUMNS)]
        lines.extend(";".join(str(row[field]) for field in CSV_COLUMNS) for row in rows)
        csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        result = FitExporter.convert_csv_to_fit(str(csv_path))
        assert not result["success"]
        assert not WorkoutPaths.from_csv(csv_path).fit.exists()


def test_malformed_csv_fails_gracefully() -> None:
    with tempfile.TemporaryDirectory() as d:
        csv_path = Path(d) / "bad.csv"
        _write_csv(csv_path, rows=10, full_schema=False)  # missing columns
        # Direct reader call should raise the typed error (not SystemExit).
        try:
            CsvReader(str(csv_path)).read_all()
            raised = False
        except CsvReadError:
            raised = True
        except SystemExit as err:
            raise AssertionError("read_all raised SystemExit instead of CsvReadError") from err
        assert raised, "expected CsvReadError for malformed CSV"

        # And the public conversion API turns it into a clean failure result.
        result = FitExporter.convert_csv_to_fit(str(csv_path))
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
