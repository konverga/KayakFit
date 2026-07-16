"""Background worker-thread management for the GUI.

Runs the workout and export workers on daemon threads so BLE capture, CSV
writing, FIT export and Strava upload never block the interface, and marshals
every UI touch (log lines, structured events, token-refresh notifications,
blocking prompts) back onto the GUI thread via ``widget.after`` — tolerating
app shutdown mid-flight. Stop and step-advance requests are delivered to the
workers through shared ``threading.Event`` flags; ``cleanup()`` waits briefly
for a running thread on shutdown.
"""

import asyncio
import contextlib
import queue
import threading
import traceback
from collections.abc import Callable
from tkinter import messagebox
from typing import Any, cast

from app.events import UiEvent
from app.worker_result import WorkerResult


class WorkerManager:
    """Manages worker thread execution and output capture."""

    def __init__(self, log_callback: Callable[[str], None], widget: Any) -> None:
        """Initialize worker manager.

        Args:
            log_callback: Function to call for logging messages (handles memory limits).
            widget: Tkinter widget for scheduling GUI updates (via .after()).
        """
        self.log = log_callback
        self.widget = widget
        self.worker_thread: threading.Thread | None = None
        self.stop_flag = threading.Event()
        self.advance_flag = threading.Event()  # request to advance a program step
        self.workout_worker: Callable[..., Any] | None = None
        self.export_worker: Callable[..., Any] | None = None
        self.on_tokens_updated: Callable[[UiEvent], None] | None = None
        self.on_event: Callable[[UiEvent], None] | None = None
        self._event_lock = threading.Lock()
        self._replaceable_events: dict[str, UiEvent] = {}
        self._event_drain_scheduled = False

    def set_token_update_callback(
        self, callback: Callable[[UiEvent], None]
    ) -> None:
        """Register a callback invoked (on the GUI thread) when Strava tokens are refreshed.

        Args:
            callback: Function receiving the worker result dict with new tokens.
        """
        self.on_tokens_updated = callback

    def set_event_callback(self, callback: Callable[[UiEvent], None]) -> None:
        """Register a callback for structured UI events (metrics, status, upload).

        The callback is always invoked on the GUI thread.

        Args:
            callback: Function receiving an event dict with a ``type`` key.
        """
        self.on_event = callback

    def _ui(self, fn: Callable[[], None]) -> None:
        """Schedule a callable on the GUI thread, tolerating app shutdown.

        ``widget.after`` raises (Runtime/TclError) once the Tk interpreter is
        being destroyed; a worker thread finishing during shutdown must not
        crash on that.
        """
        with contextlib.suppress(Exception):
            self.widget.after(0, fn)

    def _threadsafe_log(self, message: str) -> None:
        """Log callback safe to invoke from any thread.

        Worker code (and its logging handlers) runs on background threads, but
        the log sink is a Tk text widget. Marshal every line onto the GUI
        thread instead of touching the widget from the worker thread.
        """
        # `message` is a plain parameter (not a loop variable), so capturing it
        # directly in a zero-arg lambda is safe and lets mypy infer the lambda's
        # type against _ui's Callable[[], None] (a defaulted `m=message` param
        # can't be matched to the zero-arg expected type).
        self._ui(lambda: self.log(message))

    def _emit(self, event: UiEvent) -> None:
        """Marshal a UI event onto the GUI thread."""
        replaceable_key: str | None = None
        if event.get("type") == "metrics":
            replaceable_key = "metrics"
        elif (
            event.get("type") == "status"
            and event.get("event") == "program"
            and event.get("transition") is None
        ):
            replaceable_key = "program"
        if replaceable_key is None:
            if self.on_event:
                on_event = self.on_event
                self._ui(lambda: on_event(event))
            return

        with self._event_lock:
            self._replaceable_events[replaceable_key] = event
            if self._event_drain_scheduled:
                return
            self._event_drain_scheduled = True
        self._ui(self._drain_replaceable_events)

    def _drain_replaceable_events(self) -> None:
        """Deliver only the newest high-frequency state of each kind."""
        with self._event_lock:
            events = list(self._replaceable_events.values())
            self._replaceable_events.clear()
            self._event_drain_scheduled = False
        if self.on_event:
            for event in events:
                self.on_event(event)

    def _prompt_user(self, title: str, message: str) -> bool:
        """Show a blocking yes/no dialog from a worker thread.

        The dialog is created on the GUI thread (via ``after``); the calling
        worker thread blocks on a queue until the user answers. Used by the
        workout session to ask whether to continue without a heart-rate monitor.
        """
        answer: queue.Queue[bool] = queue.Queue()

        def ask() -> None:
            try:
                answer.put(bool(messagebox.askyesno(title, message)))
            except Exception:  # pragma: no cover - dialog failure -> proceed
                answer.put(True)

        self._ui(ask)
        # Bounded wait so the worker thread can never hang forever if the GUI
        # goes away before the dialog is answered (proceed in that case).
        try:
            return answer.get(timeout=300)
        except queue.Empty:
            return True

    def set_workers(
        self, workout_worker: Callable[..., Any], export_worker: Callable[..., Any]
    ) -> None:
        """Set worker functions.

        Args:
            workout_worker: Function for workout worker
            export_worker: Function for export worker
        """
        self.workout_worker = workout_worker
        self.export_worker = export_worker

    def start_worker(
        self,
        worker_type: str,
        args: dict[str, Any] | None = None,
    ) -> bool:
        """Start a worker thread.

        Args:
            worker_type: Type of worker ('workout' or 'export_upload').
            args: Optional dictionary of arguments to pass to worker.

        Returns:
            True if the worker thread was started, False otherwise (so the
            caller never flips UI state for a worker that isn't running).
        """
        if self.worker_thread and self.worker_thread.is_alive():
            self.log("A task is already running; please wait.\n")
            return False

        labels = {"workout": "workout", "export_upload": "export"}
        self.log(f"Starting {labels.get(worker_type, worker_type)}...\n")

        # Reset stop / advance flags
        self.stop_flag.clear()
        self.advance_flag.clear()

        # Start worker thread
        try:
            if worker_type == "workout":
                self.worker_thread = threading.Thread(
                    target=self._run_workout_worker, args=(args,), daemon=True
                )
            elif worker_type == "export_upload":
                self.worker_thread = threading.Thread(
                    target=self._run_export_worker, args=(args,), daemon=True
                )
            else:
                self.log(f"Error: Unknown worker type: {worker_type}\n")
                return False

            self.worker_thread.start()
            return True

        except Exception as e:
            self.log(f"Error starting worker: {e}\n")
            return False

    def _run_workout_worker(self, args: dict[str, Any] | None) -> None:
        """Run workout worker in thread.

        Args:
            args: Dictionary with boat_weight, person_weight, ergometer_mac
        """
        if not self.workout_worker:
            self.log("Error: Workout worker not configured\n")
            return

        loop = None
        try:
            # Extract arguments
            boat_weight = args.get("boat_weight") if args else None
            person_weight = args.get("person_weight") if args else None
            ergometer_mac = args.get("ergometer_mac") if args else None
            program = args.get("program") if args else None

            # Create new event loop for this thread
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            # Run async worker. The log callback must be the thread-safe
            # wrapper: it is invoked from this worker thread (via logging
            # handlers) but ultimately writes to a Tk widget.
            result = loop.run_until_complete(
                self.workout_worker(
                    boat_weight=boat_weight,
                    person_weight=person_weight,
                    ergometer_mac=ergometer_mac,
                    log_callback=self._threadsafe_log,
                    stop_event=self.stop_flag,
                    data_callback=lambda d: self._emit(UiEvent(type="metrics", data=d)),
                    status_callback=lambda s: self._emit(
                        cast(UiEvent, {"type": "status", **s})
                    ),
                    prompt_callback=self._prompt_user,
                    program=program,
                    advance_event=self.advance_flag,
                )
            )

            if not isinstance(result, WorkerResult):
                raise TypeError("Workout worker returned no typed terminal result")
            self._threadsafe_log(
                "\n✓ Finished.\n" if result.success else f"\n✗ {result.message}\n"
            )
        except Exception as e:
            self._threadsafe_log(f"\n✗ Workout task failed: {e}\n")
            result = WorkerResult(
                outcome="failed",
                stage="worker",
                message=str(e),
                retryable=True,
            )
        finally:
            self._emit(result.to_event("workout_result"))
            # Clean up event loop aggressively
            if loop:
                try:
                    # Cancel all remaining tasks immediately
                    pending = asyncio.all_tasks(loop)
                    for task in pending:
                        task.cancel()
                    # Run loop briefly to allow cancellations (with timeout)
                    if pending:
                        with contextlib.suppress(asyncio.TimeoutError):  # Force close anyway
                            loop.run_until_complete(
                                asyncio.wait_for(
                                    asyncio.gather(*pending, return_exceptions=True),
                                    timeout=0.5,
                                )
                            )
                    loop.close()
                except Exception:
                    pass

    def _run_export_worker(self, args: dict[str, Any] | None) -> None:
        """Run export worker in thread.

        Args:
            args: Dictionary with file_path, access_token, etc.
        """
        if not self.export_worker:
            self.log("Error: Export worker not configured\n")
            return

        try:
            # Extract arguments
            file_path = args.get("file_path", "") if args else ""
            access_token = args.get("access_token", "") if args else ""
            refresh_token = args.get("refresh_token", "") if args else ""
            client_id = args.get("client_id", "") if args else ""
            client_secret = args.get("client_secret", "") if args else ""
            strava_upload = args.get("strava_upload", "yes") if args else "yes"

            # Run worker (thread-safe log wrapper; see _run_workout_worker).
            result = self.export_worker(
                file_path=file_path,
                access_token=access_token,
                refresh_token=refresh_token,
                client_id=client_id,
                client_secret=client_secret,
                strava_upload=strava_upload,
                log_callback=self._threadsafe_log,
                stop_event=self.stop_flag,
            )

            # Check if tokens were updated
            if not isinstance(result, WorkerResult):
                raise TypeError("Export worker returned no typed terminal result")
            if result.tokens_updated:
                # Notify main GUI to reload config
                self._ui(lambda: self._handle_token_update(result))

            if result.success:
                self._threadsafe_log("\n✓ Finished.\n")
            else:
                self._threadsafe_log("\n✗ Export failed.\n")

            self._emit(result.to_event("export_result"))
        except Exception as e:
            self._threadsafe_log(f"\n✗ Export failed: {e}\n{traceback.format_exc()}\n")
            self._emit(
                WorkerResult(
                    outcome="failed",
                    stage="worker",
                    message=str(e),
                    retryable=True,
                ).to_event("export_result")
            )

    def _handle_token_update(self, result: WorkerResult) -> None:
        """Handle token update on the GUI thread by notifying the registered callback."""
        if self.on_tokens_updated:
            self.on_tokens_updated(result.to_event("token_update"))

    def advance_program(self) -> None:
        """Request the running workout to advance to the next program step."""
        if self.worker_thread and self.worker_thread.is_alive():
            self.advance_flag.set()

    def stop_worker(self) -> None:
        """Stop the running worker thread."""
        if not self.worker_thread or not self.worker_thread.is_alive():
            self.log("Nothing is running.\n")
            return

        self.log("\nStopping...\n")

        # Threads cannot be force-killed; the workout worker polls this flag
        # and shuts the session down cleanly. (Exports run to completion,
        # except the Strava processing poll, which honours this flag.)
        self.stop_flag.set()

    def is_running(self) -> bool:
        """Check if worker is currently running.

        Returns:
            bool: True if worker is running.
        """
        return self.worker_thread is not None and self.worker_thread.is_alive()

    def cleanup(self) -> None:
        """Cleanup resources and ensure threads are stopped."""
        # Only do cleanup if there's actually a thread running
        if not self.worker_thread or not self.worker_thread.is_alive():
            self.worker_thread = None
            return

        # Set stop flag
        self.stop_flag.set()

        # Wait for thread to finish (with short timeout for quick shutdown)
        self.worker_thread.join(timeout=2.0)
        # Don't wait any longer - let daemon thread cleanup happen in background

        # Clear references
        self.worker_thread = None
