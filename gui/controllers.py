"""Small typed state owners used by the composed KayakFit GUI."""

from dataclasses import dataclass
from typing import Any


@dataclass
class RecordingController:
    """Mutable state for one recording and its stop-confirm lifecycle."""

    active: bool = False
    csv_path: str | None = None
    points: int = 0
    incomplete: bool = False
    persistence_error: str | None = None
    paused: bool = False
    lap: int = 0
    stop_armed: bool = False
    stop_timer_id: str | None = None

    def reset_workout(self) -> None:
        """Clear state derived from the current or most recent workout."""
        self.csv_path = None
        self.points = 0
        self.incomplete = False
        self.persistence_error = None
        self.paused = False
        self.lap = 0


@dataclass
class ProgramController:
    """State for program execution and target-adherence presentation."""

    active: bool = False
    target: dict[str, Any] | None = None
    shown_target: str | None = None
    pending_target: str | None = None
    pending_count: int = 0
    ending_warned: bool = False


@dataclass
class ExportController:
    """State for the single export/upload task and resulting activity."""

    running: bool = False
    strava_activity_id: int | None = None
