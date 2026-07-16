"""Workout worker: the live data-collection loop.

Runs the recording side of a session as a worker coordinated by the main GUI:
loads config (YAML), discovers and connects the ergometer (and optional HRM)
over BLE, streams real-time samples into a :class:`app.workout_session.
WorkoutSession` that writes the CSV, and shuts down cleanly on request. The
companion :mod:`export_worker` handles the CSV-to-FIT / upload side.
"""

import asyncio
import contextlib
import threading
from collections.abc import Callable
from typing import Any

from app.events import StatusPayload
from app.program import Program
from app.worker_result import WorkerResult
from app.workout_session import WorkoutSession
from gui.config_manager import ConfigError, ConfigManager


def _log_wrapper(log_callback: Callable[[str], None] | None, msg: str) -> None:
    """Helper to log messages either to callback or print."""
    if log_callback:
        log_callback(msg + "\n")
    else:
        print(msg)


async def run_workout_worker(
    boat_weight: int | None = None,
    person_weight: int | None = None,
    ergometer_mac: str | None = None,
    log_callback: Callable[[str], None] | None = None,
    stop_event: threading.Event | None = None,
    data_callback: Callable[[dict[str, Any]], None] | None = None,
    status_callback: Callable[[StatusPayload], None] | None = None,
    prompt_callback: Callable[[str, str], bool] | None = None,
    program: Program | None = None,
    advance_event: threading.Event | None = None,
) -> WorkerResult:
    """Run workout worker with optional overrides.

    Args:
        boat_weight: Optional boat weight override (kg)
        person_weight: Optional person weight override (kg)
        ergometer_mac: Optional ergometer MAC address override
        log_callback: Optional callback function for logging
        stop_event: Optional threading.Event to signal stop request
        data_callback: Optional callback receiving each per-second data dict
        status_callback: Optional callback receiving connection/session status events
        prompt_callback: Optional blocking yes/no prompt ``(title, message) -> bool``
        program: Optional structured training Program to drive the session
        advance_event: Optional threading.Event to manually advance program steps
    """
    def log(msg: str) -> None:
        _log_wrapper(log_callback, msg)

    # Load and validate config (single source of truth).
    try:
        config = ConfigManager().load_runtime_config()
    except ConfigError as e:
        log(f"ERROR: {e}")
        return WorkerResult(
            outcome="failed", stage="configuration", message=str(e), retryable=True
        )

    # Override ergometer MAC if provided
    if ergometer_mac:
        config['ergometer_mac'] = ergometer_mac
        log(f"Using ergometer MAC from arguments: {ergometer_mac}")

    # Display configuration only if values differ from config
    overrides = []
    if boat_weight is not None and boat_weight != config['boat_weight_default']:
        overrides.append(
            f"  Boat weight: {boat_weight}kg "
            f"(overriding config: {config['boat_weight_default']}kg)"
        )
    if person_weight is not None and person_weight != config['person_weight_default']:
        overrides.append(
            f"  Person weight: {person_weight}kg "
            f"(overriding config: {config['person_weight_default']}kg)"
        )

    if overrides:
        log("Starting workout with overrides:")
        for override in overrides:
            log(override)

    # Create and run session with overrides, with 6-hour timeout
    session = WorkoutSession(
        config,
        boat_weight=boat_weight,
        person_weight=person_weight,
        log_callback=log_callback,
        data_callback=data_callback,
        status_callback=status_callback,
        prompt_callback=prompt_callback,
        program=program,
        advance_event=advance_event,
    )

    # If stop_event provided, create a task to monitor it
    stop_task = None
    if stop_event:
        async def check_stop_flag() -> None:
            while not stop_event.is_set():
                await asyncio.sleep(0.1)
            log("\nStop signal received from GUI...")
            session.stop_event.set()
        stop_task = asyncio.create_task(check_stop_flag())

    try:
        return await asyncio.wait_for(session.run(), timeout=6 * 3600)
    except TimeoutError:
        # wait_for cancelled session.run(); its finally block has already
        # disconnected the devices, closed the CSV and emitted the summary.
        log("\nWorkout session exceeded maximum duration of 6 hours. Stopped.")
        return WorkerResult(
            outcome="partial" if session.data_point_count > 0 else "failed",
            stage="timeout",
            message="Workout exceeded the six-hour safety limit",
            csv_path=str(session.csv_path) if session.csv_path else None,
            durable_rows=(
                session.csv_writer.records_written if session.csv_writer else 0
            ),
            retryable=True,
        )
    except Exception as exc:
        log(f"\nWorkout worker failed: {exc}")
        return WorkerResult(
            outcome="partial" if session.data_point_count > 0 else "failed",
            stage="worker",
            message=str(exc),
            csv_path=str(session.csv_path) if session.csv_path else None,
            durable_rows=(
                session.csv_writer.records_written if session.csv_writer else 0
            ),
            retryable=True,
        )
    finally:
        if stop_task:
            stop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stop_task
