"""Tests for the single-directory workout storage contract."""

from datetime import datetime
from pathlib import Path

from app.workout_paths import WorkoutPaths


def test_reserve_creates_fixed_artifact_directory_with_collision_suffix(tmp_path: Path) -> None:
    started = datetime(2026, 7, 14, 12, 0, 0)
    first = WorkoutPaths.reserve(tmp_path, started)
    second = WorkoutPaths.reserve(tmp_path, started)

    assert first.directory.name == "workout_20260714_120000"
    assert second.directory.name == "workout_20260714_120000_001"
    assert first.csv == first.directory / "workout.csv"
    assert first.metadata == first.directory / "metadata.json"
    assert first.steps == first.directory / "steps.json"
    assert first.fit == first.directory / "activity.fit"
    assert first.fit_temp == first.directory / "activity.fit.tmp"


def test_owned_directory_validation_rejects_non_workout_paths(tmp_path: Path) -> None:
    paths = WorkoutPaths.reserve(tmp_path, datetime(2026, 7, 14, 12, 0, 0))

    assert paths.is_owned_by(tmp_path)
    assert not WorkoutPaths(tmp_path / "2026").is_owned_by(tmp_path)
    assert not WorkoutPaths(tmp_path / "elsewhere" / "workout_1").is_owned_by(tmp_path)


def test_from_csv_uses_fixed_artifact_names(tmp_path: Path) -> None:
    directory = tmp_path / "workout_20260714_225154"
    directory.mkdir()
    csv_path = directory / "workout.csv"
    csv_path.touch()

    paths = WorkoutPaths.from_csv(csv_path)
    assert paths.csv == csv_path
    assert paths.metadata == directory / "metadata.json"
    assert paths.steps == directory / "steps.json"
    assert paths.fit == directory / "activity.fit"
