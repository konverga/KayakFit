"""Typed GUI state-owner tests without constructing Tk widgets."""

from pathlib import Path

from gui.controllers import RecordingController
from gui.history_window import HistoryCatalog


def test_recording_controller_resets_workout_state_only() -> None:
    state = RecordingController(
        active=True,
        csv_path="workout.csv",
        points=42,
        incomplete=True,
        persistence_error="disk full",
        paused=True,
        lap=3,
        stop_armed=True,
    )

    state.reset_workout()

    assert state.active is True
    assert state.stop_armed is True
    assert state.csv_path is None
    assert state.points == 0
    assert state.incomplete is False
    assert state.persistence_error is None
    assert state.paused is False
    assert state.lap == 0


def test_history_catalog_owns_deduplication_and_selection() -> None:
    catalog = HistoryCatalog()
    path = Path("workout.csv")
    row = object()

    catalog.register(path, row, "free")
    key = catalog.key(path)

    assert catalog.rows_by_key[key] is row
    assert catalog.paths_by_key[key] == path
    assert catalog.types_by_key[key] == "free"
