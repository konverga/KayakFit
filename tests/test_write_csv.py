"""Tests for durable workout CSV writing and output-path reservation."""

import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from app import recovery
from app.workout_session import WorkoutSession
from app.write_csv import CsvWriteError, CsvWriter


def test_write_failure_is_terminal_and_does_not_increment_count() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "workout.csv"
        writer = CsvWriter(str(path), {})
        assert writer.writer is not None
        with (
            patch.object(writer.writer, "writerow", side_effect=OSError("disk full")),
            pytest.raises(CsvWriteError, match="disk full"),
        ):
            writer.write_record({"timestamp": 1})
        assert writer.failed
        assert writer.records_written == 0
        with pytest.raises(CsvWriteError, match="earlier write failure"):
            writer.write_record({"timestamp": 2})
        writer.close()


def test_successful_write_counts_only_flushed_rows() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "workout.csv"
        writer = CsvWriter(str(path), {})
        writer.write_record({"timestamp": 1, "distance__m": 3})
        assert writer.records_written == 1
        writer.close()


def test_session_reserves_unique_workout_directory_without_overwrite() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        marker = root / ".active_workout.json"
        config = {
            "output_dir": "workouts",
            "log_level": "warning",
            "pull_length_m": 0.623,
            "data_points": "instant",
            "autopause_stationary_threshold_records": 5,
            "autopause_gap_seconds": 5,
            "max_hr": 185,
            "hr_zone_mode": "auto",
            "hr_zones": [],
        }
        started = datetime(2026, 7, 13, 12, 0, 0)
        first = WorkoutSession(dict(config))
        second = WorkoutSession(dict(config))
        first.session_start = started
        second.session_start = started
        with (
            patch("app.workout_session.Path.home", return_value=root),
            patch.object(recovery, "_MARKER_PATH", marker),
        ):
            first._setup_csv_writer()
            second._setup_csv_writer()
        assert first.csv_path is not None and second.csv_path is not None
        assert first.csv_path != second.csv_path
        assert Path(first.csv_path).name == "workout.csv"
        assert Path(second.csv_path).name == "workout.csv"
        assert Path(first.csv_path).parent.name == "workout_20260713_120000"
        assert Path(second.csv_path).parent.name == "workout_20260713_120000_001"
        assert Path(first.csv_path).parent != Path(second.csv_path).parent
        assert (Path(first.csv_path).parent / "metadata.json").exists()
        assert (Path(second.csv_path).parent / "metadata.json").exists()
        assert first.csv_writer is not None and second.csv_writer is not None
        first.csv_writer.close()
        second.csv_writer.close()
