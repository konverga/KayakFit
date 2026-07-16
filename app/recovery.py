"""Crash / interruption recovery for workout sessions.

A workout streams to its CSV one record at a time, so the data of an
interrupted session (app crash, power loss, forced quit) survives on disk -- it
just never got converted to a FIT or uploaded. This module tracks an
"active workout" marker so the GUI can offer to recover such a session on the
next launch.

Lifecycle:
  * The workout session calls :func:`mark_active` when recording starts.
  * The GUI calls :func:`clear_active` once the workout has been safely exported
    (or the user dismisses its summary), and on the no-data path.
  * On startup the GUI calls :func:`pending_recovery`; a non-None result means a
    previous session was interrupted and still has data worth recovering.

The marker is intentionally tiny and self-healing: a stale marker pointing at a
missing or empty CSV is cleared automatically.
"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from .atomic_json import write_atomic_json
from .workout_paths import WorkoutPaths

# Marker lives alongside the YAML config in the user's KayakFit folder.
_MARKER_PATH = Path.home() / "KayakFit" / ".active_workout.json"


def _marker_path() -> Path:
    return _MARKER_PATH


def mark_active(workout_dir: str | Path, started_at: datetime | None = None) -> None:
    """Record that a workout directory is in progress.

    Failure is propagated because a recording without durable recovery state
    must not be reported as safely started.
    """
    path = _marker_path()
    payload = {
        "version": 2,
        "state": "recording",
        "workout_dir": str(workout_dir),
        "started_at": (started_at or datetime.now()).isoformat(timespec="seconds"),
    }
    write_atomic_json(path, payload)


def mark_partial(workout_dir: str | Path, message: str) -> None:
    """Durably record a retained workout that needs user recovery."""
    write_atomic_json(
        _marker_path(),
        {
            "version": 2,
            "state": "partial",
            "workout_dir": str(workout_dir),
            "message": message,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        },
    )


def clear_active() -> None:
    """Remove the active-workout marker if present."""
    path = _marker_path()
    path.unlink(missing_ok=True)
    if os.name != "nt" and path.parent.exists():
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


def _read_marker() -> dict[str, Any] | None:
    path = _marker_path()
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if (
            isinstance(data, dict)
            and data.get("version") == 2
            and data.get("state") in ("recording", "partial")
            and data.get("workout_dir")
        ):
            return data
    except (OSError, ValueError):
        pass
    return None


def _csv_has_data(csv_path: Path) -> bool:
    """True when the CSV has at least one record beyond the header row."""
    try:
        with open(csv_path, encoding="utf-8") as f:
            f.readline()  # header
            return bool(f.readline().strip())
    except OSError:
        return False


def pending_recovery() -> dict[str, Any] | None:
    """Return marker info for an interrupted, recoverable workout, else None.

    A marker whose CSV is missing or empty is treated as nothing to recover and
    is cleared so the prompt does not reappear.
    """
    data = _read_marker()
    if not data:
        return None

    workout_dir = Path(str(data.get("workout_dir", "")))
    csv_path = WorkoutPaths(workout_dir).csv
    if not csv_path.exists() or not _csv_has_data(csv_path):
        clear_active()
        return None
    # The GUI/export worker acts on the canonical CSV; expose it after the
    # directory-level marker has been validated.
    return {**data, "csv_path": str(csv_path)}
