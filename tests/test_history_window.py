"""Workout-history classification tests for the strict v1 contract."""

import json
import stat
import tempfile
from pathlib import Path

from app.workout_paths import WorkoutPaths
from gui.history_window import _display_name, _workout_type, list_workouts
from gui.recording_lifecycle import _remove_workout_directory, _retry_readonly_removal


def _write_metadata(csv_path: Path, mode: str) -> None:
    processing_config = {
        "pull_length_m": 0.623,
        "max_hr": 185,
        "hr_zone_mode": "auto",
        "hr_zones": [],
    }
    WorkoutPaths.from_csv(csv_path).metadata.write_text(
        json.dumps({
            "version": 1,
            "workout_mode": mode,
            "processing_config": processing_config,
        }),
        encoding="utf-8",
    )


def test_v1_free_csv_is_free() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = Path(tmp) / "free.csv"
        csv_path.touch()
        _write_metadata(csv_path, "free")
        assert _workout_type(csv_path) == "free"


def test_v1_planned_csv_is_planned() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = Path(tmp) / "planned.csv"
        csv_path.touch()
        _write_metadata(csv_path, "planned")
        assert _workout_type(csv_path) == "planned"


def test_missing_metadata_is_invalid() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = Path(tmp) / "missing.csv"
        csv_path.touch()
        assert _workout_type(csv_path) == "invalid"


def test_invalid_metadata_is_invalid() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = Path(tmp) / "invalid.csv"
        csv_path.touch()
        _write_metadata(csv_path, "other")
        assert _workout_type(csv_path) == "invalid"


def test_fit_file_does_not_claim_free_or_planned() -> None:
    assert _workout_type(Path("external.FIT")) == "fit"


def test_list_workouts_stops_at_newest_years() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        old = root / "2024"
        new = root / "2026"
        old.mkdir()
        new.mkdir()
        for parent, label in ((old, "old"), (new, "new")):
            for index in range(3):
                workout_dir = parent / f"workout_{label}_{index}"
                workout_dir.mkdir()
                WorkoutPaths(workout_dir).csv.touch()
        found = list_workouts(root, limit=2)
        assert len(found) == 2
        assert all(path.parent.parent == new for path in found)


def test_list_workouts_default_cap_keeps_history_bounded() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        year = Path(tmp) / "2026"
        year.mkdir()
        for index in range(201):
            workout_dir = year / f"workout_{index:03d}"
            workout_dir.mkdir()
            WorkoutPaths(workout_dir).csv.touch()
        assert len(list_workouts(year.parent)) == 200


def test_display_name_uses_workout_directory_for_canonical_csv() -> None:
    csv_path = Path("2026/workout_20260714_120000/workout.csv")
    assert _display_name(csv_path) == "workout_20260714_120000"


def test_remove_workout_directory_handles_readonly_csv() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        workout_directory = Path(tmp) / "workout_20260714_120000"
        workout_directory.mkdir()
        csv_path = WorkoutPaths(workout_directory).csv
        csv_path.write_text("timestamp\n", encoding="utf-8")
        csv_path.chmod(stat.S_IREAD)

        _remove_workout_directory(workout_directory)

        assert not workout_directory.exists()


def test_readonly_removal_retry_restores_write_permission() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "readonly.csv"
        path.write_text("timestamp\n", encoding="utf-8")
        path.chmod(stat.S_IREAD)

        def remove(candidate: str) -> None:
            assert Path(candidate).stat().st_mode & stat.S_IWRITE
            Path(candidate).unlink()

        _retry_readonly_removal(remove, str(path), PermissionError("read-only"))

        assert not path.exists()
