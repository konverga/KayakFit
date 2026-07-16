"""
Tests for the crash/interruption recovery marker.

Run from the project root:

    python tests/test_recovery.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import recovery
from app.workout_paths import WorkoutPaths


def _use_temp_marker(tmp: Path) -> None:
    """Point the module's marker at a temp file for the duration of a test."""
    recovery._MARKER_PATH = tmp / ".active_workout.json"


def _write_csv(path: Path, rows: int) -> None:
    lines = ["timestamp;distance"]
    for i in range(rows):
        lines.append(f"{1000 * i};{3.0 * i}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_no_marker_means_no_recovery() -> None:
    with tempfile.TemporaryDirectory() as d:
        _use_temp_marker(Path(d))
        assert recovery.pending_recovery() is None
        print("  ok: clean state offers no recovery")


def test_marker_with_data_is_recoverable() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        _use_temp_marker(tmp)
        workout_dir = tmp / "workout_20260714_120000"
        workout_dir.mkdir()
        csv_path = WorkoutPaths(workout_dir).csv
        _write_csv(csv_path, rows=20)
        recovery.mark_active(workout_dir)

        info = recovery.pending_recovery()
        assert info is not None, "expected a recoverable workout"
        assert info["csv_path"] == str(csv_path)
        print("  ok: interrupted workout with data is recoverable")


def test_marker_with_empty_csv_self_heals() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        _use_temp_marker(tmp)
        workout_dir = tmp / "workout_20260714_120000"
        workout_dir.mkdir()
        csv_path = WorkoutPaths(workout_dir).csv
        csv_path.write_text("timestamp;distance\n", encoding="utf-8")  # header only
        recovery.mark_active(workout_dir)

        assert recovery.pending_recovery() is None, "header-only CSV is not recoverable"
        assert not recovery._MARKER_PATH.exists(), "stale marker should self-clear"
        print("  ok: header-only CSV clears the marker")


def test_marker_with_missing_csv_self_heals() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        _use_temp_marker(tmp)
        recovery.mark_active(tmp / "workout_20260714_120000")

        assert recovery.pending_recovery() is None
        assert not recovery._MARKER_PATH.exists()
        print("  ok: missing CSV clears the marker")


def test_clear_active_removes_marker() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        _use_temp_marker(tmp)
        workout_dir = tmp / "workout_20260714_120000"
        workout_dir.mkdir()
        csv_path = WorkoutPaths(workout_dir).csv
        _write_csv(csv_path, rows=5)
        recovery.mark_active(workout_dir)
        assert recovery._MARKER_PATH.exists()

        recovery.clear_active()
        assert not recovery._MARKER_PATH.exists()
        assert recovery.pending_recovery() is None
        print("  ok: clear_active removes the marker")
