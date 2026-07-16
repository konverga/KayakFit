"""Typed event contracts crossing worker, session, BLE, and GUI boundaries."""

from typing import Any, Literal, Required, TypedDict

UiEventType = Literal[
    "metrics", "status", "workout_result", "export_result", "token_update"
]
TerminalEventType = Literal["workout_result", "export_result", "token_update"]


class StatusPayload(TypedDict, total=False):
    """Device/session status before WorkerManager adds the top-level type."""

    event: Required[str]
    device: Required[str]
    transition: str | None
    message: str
    level: int
    saved_rows: int
    minutes: float
    paused: bool
    lap: int
    step_index: int
    total: int
    step_label: str | None
    step_kind: str | None
    target: dict[str, Any] | None
    remaining: Any
    remaining_kind: Any
    fraction: Any
    next_label: str | None
    done: bool
    csv_path: str
    points: int
    incomplete: bool


class UiEvent(TypedDict, total=False):
    """Complete event delivered on the GUI thread."""

    type: Required[UiEventType]
    data: dict[str, Any]
    event: str
    device: str
    transition: str | None
    message: str
    level: int
    saved_rows: int
    minutes: float
    paused: bool
    lap: int
    step_index: int
    total: int
    step_label: str | None
    step_kind: str | None
    target: dict[str, Any] | None
    remaining: Any
    remaining_kind: Any
    fraction: Any
    next_label: str | None
    done: bool
    outcome: str
    stage: str
    csv_path: str | None
    fit_path: str | None
    durable_rows: int
    retryable: bool
    activity_id: int | None
    upload_id: int | None
    tokens_updated: bool
    new_access_token: str | None
    new_refresh_token: str | None
    success: bool
    pending: bool
    points: int
    incomplete: bool
