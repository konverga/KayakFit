"""Typed terminal outcomes shared by background workers and the GUI."""

from dataclasses import asdict, dataclass
from typing import Literal, cast

from .events import TerminalEventType, UiEvent

WorkerOutcome = Literal["success", "cancelled", "partial", "failed", "pending"]


@dataclass(frozen=True)
class WorkerResult:
    """Exactly one terminal description of a worker operation."""

    outcome: WorkerOutcome
    stage: str
    message: str
    csv_path: str | None = None
    fit_path: str | None = None
    durable_rows: int = 0
    retryable: bool = False
    activity_id: int | None = None
    upload_id: int | None = None
    tokens_updated: bool = False
    new_access_token: str | None = None
    new_refresh_token: str | None = None

    @property
    def success(self) -> bool:
        """Whether the requested operation completed successfully."""
        return self.outcome == "success"

    def to_event(self, event_type: TerminalEventType) -> UiEvent:
        """Return a GUI-event dictionary without duplicating result mapping."""
        return cast(UiEvent, {
            "type": event_type,
            **asdict(self),
            "success": self.success,
            "pending": self.outcome == "pending",
        })
