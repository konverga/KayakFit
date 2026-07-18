"""Live workout recording orchestrator.

Coordinates a complete session: connects the ergometer and (optionally) a
heart-rate monitor over BLE, merges their async callbacks into per-second rows
written straight to CSV, and feeds the live UI through data/status callbacks.
Runs the shared ``LiveSegmenter`` for real-time autopause/lap indication, an
optional ``ProgramRunner`` for structured workouts (recording each step's real
start/end boundary and writing ``steps.json`` in the workout directory at the
end), a data-stall watchdog, and an inactivity auto-stop. Teardown
is guarded step by step so a recording always reaches disk even when a device
errors on the way down.
"""

import asyncio
import contextlib
import time
import traceback
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from bleak import BleakScanner
from bleak.backends.device import BLEDevice

from . import recovery
from .atomic_json import write_atomic_json
from .coerce import to_float
from .config import get_bool
from .events import StatusPayload
from .heart_rate_monitor_bluetooth import HeartRateMonitor
from .kayakfirst_ergometer_bluetooth import (
    ErgometerConnectionError,
    KayakFirstErgometer,
)
from .logger import Logger
from .program import Program
from .program_runner import ProgramRunner
from .recording_timeline import RecordingTimeline
from .read_csv import CsvReader
from .segmentation import (
    LiveSegmenter,
    load_program_steps,
)
from .summary import compute_summary
from .table import Table
from .worker_result import WorkerResult
from .workout_metadata import save_workout_metadata
from .workout_paths import WorkoutPaths
from .write_csv import CsvWriteError, CsvWriter

_HRM_DISCOVERY_ATTEMPTS = 2
_HRM_DISCOVERY_RETRY_DELAY_S = 1.0


class WorkoutAbortedError(Exception):
    """Raised to cancel a workout before it starts.

    For example, the user declined to continue after a heart-rate monitor
    could not be reached.
    """


class WorkoutSession:
    """Manages a complete workout session with data collection."""

    def __init__(
        self,
        config: dict[str, Any],
        boat_weight: int | None = None,
        person_weight: int | None = None,
        log_callback: Callable[[str], None] | None = None,
        data_callback: Callable[[dict[str, Any]], None] | None = None,
        status_callback: Callable[[StatusPayload], None] | None = None,
        prompt_callback: Callable[[str, str], bool] | None = None,
        program: Program | None = None,
        advance_event: Any | None = None,
    ) -> None:
        """Initialize workout session.

        Args:
            config: Configuration dictionary from YAML.
            boat_weight: Override boat weight (kg), uses config default if None.
            person_weight: Override person weight (kg), uses config default if None.
            log_callback: Optional callback function for logging.
            data_callback: Optional callback receiving each per-second data dict
                (for a live UI readout).
            status_callback: Optional callback receiving connection/session status
                events (dicts with an ``event`` key).
            prompt_callback: Optional blocking callback ``(title, message) -> bool``
                used to ask the user a yes/no question (e.g. whether to continue
                without a heart-rate monitor). Returns True to proceed.
            program: Optional structured training program to drive live off
                elapsed time / distance; None runs a free-training workout.
            advance_event: Optional event the GUI sets to manually advance the
                current program step (open/manual steps, or to skip ahead).
        """
        self.config = config
        self.data_callback = data_callback
        self.status_callback = status_callback
        self.prompt_callback = prompt_callback
        self._streaming_announced = False

        # Every activity-flag transition is reflected immediately so the live
        # pause state and free-lap count match finalization.
        self.segmenter = LiveSegmenter()

        # Optional structured training program driven off elapsed time / distance.
        self.program_runner: ProgramRunner | None = (
            ProgramRunner(program) if program is not None else None
        )
        # Set by the GUI to manually advance the current program step (used for
        # open/manual steps, or to skip ahead).
        self.advance_event = advance_event

        # Program step timeline (real-time boundaries), written next to the CSV
        # so the FIT exporter can build one lap per step.
        self._program_step_log: list[dict[str, Any]] = []
        self._current_step_entry: dict[str, Any] | None = None
        self._last_session_elapsed_s: float | None = None
        self._last_program_checkpoint_monotonic = 0.0
        self._program_checkpoint_interval_s = max(
            float(self.config.get("program_checkpoint_interval_s", 15.0)), 1.0
        )

        if boat_weight is not None:
            self.config["boat_weight_default"] = boat_weight
        if person_weight is not None:
            self.config["person_weight_default"] = person_weight

        self.ergometer: KayakFirstErgometer | None = None
        self.hrm: HeartRateMonitor | None = None

        self.session_start: datetime | None = None
        self._recording_timeline: RecordingTimeline | None = None
        self.stop_event = asyncio.Event()
        self.csv_writer: CsvWriter | None = None
        self.csv_path: str | None = None
        self._persistence_error: str | None = None
        self._summary_error: str | None = None

        # Data-stall watchdog: warn if the ergometer is connected but stops
        # sending packets (e.g. firmware hang) so the gap isn't silent.
        self._last_data_monotonic: float | None = None
        self._stalled = False
        self.data_stall_timeout: float = float(self.config.get("data_stall_timeout", 8))

        # Inactivity auto-stop: end (and save) the workout after this many
        # minutes without any forward movement, so a session left running does
        # not become a multi-hour activity. 0 disables it.
        self.inactivity_autostop_s: float = (
            float(self.config.get("inactivity_autostop_minutes", 30)) * 60.0
        )
        self._last_movement_monotonic: float | None = None
        self._auto_stopped_inactive = False

        self.latest_heart_rate: int | None = None
        self.latest_heart_rate_time: float | None = None
        # Heart-rate readings older than this (seconds) are treated as stale and
        # not attached to ergometer rows, so a dropped strap stops polluting the
        # CSV with a frozen value. 0 disables expiry.
        self.hrm_stale_timeout: float = float(
            self.config.get("hrm_stale_timeout", 10)
        )

        self.data_point_count = 0

        # (Re)attach the GUI activity-log callback; process signal handling is
        # owned by the entrypoint (kayakfit_gui.main), not by logging setup.
        Logger.setup(
            log_level=self.config.get("log_level", "INFO"),
            log_callback=log_callback,
        )
        self.logger = Logger.get_logger(name=__name__)

    def _emit_status(self, event: str, device: str, **payload: Any) -> None:
        """Send a status event to the UI if a status callback is registered."""
        if self.status_callback:
            # Never let UI plumbing break the session.
            with contextlib.suppress(Exception):
                self.status_callback(
                    cast(StatusPayload, {"event": event, "device": device, **payload})
                )

    def _emit_program_status(self, state: dict[str, Any]) -> None:
        """Marshal a program state (with Step objects) into a UI-safe event."""
        step = state.get("step")
        nxt = state.get("next")
        self._emit_status(
            event="program",
            device="session",
            transition=state.get("transition"),
            step_index=int(state.get("step_index", 0)),
            total=int(state.get("total", 0)),
            step_label=step.label if step is not None else None,
            step_kind=step.kind if step is not None else None,
            target=step.target if step is not None else None,
            remaining=state.get("remaining"),
            remaining_kind=state.get("remaining_kind"),
            fraction=state.get("fraction"),
            next_label=nxt.label if nxt is not None else None,
            done=bool(state.get("done")),
        )

    def _record_step_boundary(self, state: dict[str, Any]) -> None:
        """Record canonical program step bounds for the FIT lap timeline."""
        if state.get("transition") not in ("step_start", "program_complete"):
            return
        boundary_elapsed = to_float(state.get("boundary_elapsed"))
        if boundary_elapsed is None:
            raise ValueError("Program transition is missing canonical elapsed time")
        if self._current_step_entry is not None:
            self._current_step_entry["end_elapsed_s"] = boundary_elapsed
            self._program_step_log.append(self._current_step_entry)
            self._current_step_entry = None
        step = state.get("step")
        if step is not None:  # None on program_complete
            self._current_step_entry = {
                "index": int(state.get("step_index", 0)),
                "type": getattr(step, "kind", ""),
                "label": getattr(step, "label", ""),
                "start_elapsed_s": boundary_elapsed,
            }

    @staticmethod
    def _program_transition_events(state: dict[str, Any]) -> list[dict[str, Any]]:
        """Return ordered transition snapshots from a runner result."""
        transitions = state.get("transitions")
        if isinstance(transitions, list):
            return [item for item in transitions if isinstance(item, dict)]
        return [state] if state.get("transition") is not None else []

    def _write_program_steps(self, close_open: bool = True) -> None:
        """Durably write the performed program timeline next to the CSV."""
        if self.program_runner is None or not self.csv_path:
            return
        # Close an open step (workout stopped before the program finished).
        if (
            close_open
            and self._current_step_entry is not None
            and self._last_session_elapsed_s is not None
        ):
            self._current_step_entry["end_elapsed_s"] = self._last_session_elapsed_s
            self._program_step_log.append(self._current_step_entry)
            self._current_step_entry = None
        steps = list(self._program_step_log)
        if (
            self._current_step_entry is not None
            and self._last_session_elapsed_s is not None
        ):
            current = dict(self._current_step_entry)
            current["end_elapsed_s"] = self._last_session_elapsed_s
            steps.append(current)
        if not steps:
            return
        if self.program_runner.done and self._last_session_elapsed_s is not None:
            steps[-1] = {
                **steps[-1],
                "end_elapsed_s": self._last_session_elapsed_s,
            }
        sidecar = WorkoutPaths.from_csv(self.csv_path).steps
        write_atomic_json(
            sidecar,
            {
                "version": 1,
                "complete": bool(self.program_runner.done),
                "steps": steps,
            },
        )
        self.logger.info(msg=f"Program step timeline saved to {sidecar.name}")

    def _persist_program_steps(
        self, close_open: bool = True, *, force: bool = True
    ) -> None:
        """Persist steps or stop with a visible partial-recording result."""
        now = time.monotonic()
        if (
            not force
            and now - self._last_program_checkpoint_monotonic
            < self._program_checkpoint_interval_s
        ):
            return
        try:
            self._write_program_steps(close_open=close_open)
            self._last_program_checkpoint_monotonic = now
        except OSError as exc:
            self._report_persistence_error(
                f"Could not save planned-workout timeline: {exc}"
            )

    def _hrm_enabled(self) -> bool:
        """Whether a heart-rate monitor should be used at all.

        Disabled ("no") means the session never tries to connect an HRM and
        never asks "continue without HRM?" — for users without a strap.
        """
        return get_bool(self.config, "hrm_enabled", default=True)

    def _ble_auto_reconnect(self) -> bool:
        """Whether BLE devices should auto-reconnect after a mid-session drop."""
        return get_bool(self.config, "ble_auto_reconnect", default=True)

    async def _run_until_stop(self, coro: Any, what: str) -> Any:
        """Await *coro*, aborting immediately when the user presses Stop.

        BLE connect/initialize can spend the better part of a minute in
        retries, timeouts and backoff sleeps. Racing that work against
        ``stop_event`` keeps the Stop button responsive even while the
        ergometer is unreachable — pressing Stop cancels the in-flight
        attempt instead of waiting for the whole retry budget.
        """
        work = asyncio.ensure_future(coro)
        stop = asyncio.ensure_future(self.stop_event.wait())
        try:
            done, _ = await asyncio.wait(
                {work, stop}, return_when=asyncio.FIRST_COMPLETED
            )
            if work in done:
                return work.result()  # re-raises the work's exception, if any
            work.cancel()
            with contextlib.suppress(BaseException):
                await work
            raise WorkoutAbortedError(f"Workout stopped while {what}.")
        finally:
            stop.cancel()
            with contextlib.suppress(BaseException):
                await stop

    async def run(self) -> WorkerResult:
        """Main session execution."""
        failure_stage: str | None = None
        failure_message = ""
        cancelled = False
        try:
            await self._run_until_stop(
                self._connect_devices(), "connecting to devices"
            )

            assert self.ergometer is not None, "Ergometer not connected"
            await self._run_until_stop(
                self.ergometer.initialize(), "initializing the ergometer"
            )

            self.session_start = datetime.now()
            monotonic_start = time.monotonic()
            self._recording_timeline = RecordingTimeline(
                epoch_start_ms=round(self.session_start.timestamp() * 1000),
                monotonic_start_s=monotonic_start,
            )

            self._setup_csv_writer()

            await self._run_until_stop(
                self.ergometer.start_workout(), "starting the workout"
            )

            self.logger.info(
                msg="Workout started! Press Stop button or Ctrl+C to end workout."
            )

            # Arm the watchdog now so a stream that never starts is also caught,
            # not just one that stops mid-session.
            self._last_data_monotonic = time.monotonic()
            self._last_movement_monotonic = time.monotonic()

            poll_task = asyncio.create_task(coro=self.ergometer.poll_data())
            watchdog_task = asyncio.create_task(coro=self._watchdog())

            await self.stop_event.wait()

            for task in (poll_task, watchdog_task):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        except WorkoutAbortedError as e:
            self.logger.info(msg=f"\n{e}")
            cancelled = True
            failure_message = str(e)

        except ErgometerConnectionError as e:
            self.logger.error(msg=f"\n✗ {e}")
            self.logger.error(msg="Cannot start workout without ergometer connection")
            failure_stage = "device_connection"
            failure_message = str(e)

        except CsvWriteError as e:
            self._report_persistence_error(str(e))
            failure_stage = "persistence"
            failure_message = str(e)

        except Exception as e:
            self.logger.error(msg=f"Unexpected workout failure: {e}")
            failure_stage = "recording"
            failure_message = str(e)

        finally:
            # Every teardown step is guarded: a BLE error while disconnecting
            # must never prevent the CSV from being closed and the summary
            # (which drives auto-export) from being emitted.
            self.logger.info(msg="\nStopping workout...")

            # disconnect() is called even when the link is down: it also
            # cancels a running auto-reconnect loop, so Stop works while the
            # device is unreachable instead of leaving retries running.
            if self.ergometer:
                try:
                    if self.ergometer.is_connected:
                        await self.ergometer.stop_workout()
                    await self.ergometer.disconnect()
                    self.logger.info(msg="✓ Disconnected from KayakFirst")
                except Exception as e:
                    self.logger.error(msg=f"Error disconnecting ergometer: {e}")

            if self.hrm:
                try:
                    await self.hrm.disconnect()
                    self.logger.info(msg="✓ Disconnected from HRM")
                except Exception as e:
                    self.logger.error(msg=f"Error disconnecting HRM: {e}")

            if self.csv_writer:
                try:
                    self.csv_path = self.csv_writer.close()
                    if self._persistence_error is None:
                        self.logger.info(msg=f"✓ Workout data saved to {self.csv_path}")
                except CsvWriteError as e:
                    self._report_persistence_error(str(e))
                    self.csv_path = self.csv_writer.output_path
                except Exception as e:
                    self.logger.error(msg=f"Error closing CSV writer: {e}")
                    failure_stage = "persistence"
                    failure_message = str(e)
                if self.csv_path:
                    self._persist_program_steps()

            if self.csv_path and self.data_point_count > 0:
                try:
                    reader = CsvReader(file_path=self.csv_path, config=self.config)
                    workout_df = reader.read_all()
                    self._print_workout_summary(workout_df=workout_df)
                    if self._summary_error is not None:
                        failure_stage = "finalization"
                        failure_message = self._summary_error
                    # FIT file creation removed - handled by export worker
                except Exception as e:
                    self.logger.error(msg=f"Error creating summary: {e}")
                    failure_stage = "finalization"
                    failure_message = str(e)
            else:
                # Nothing worth recovering; drop the active-workout marker.
                try:
                    recovery.clear_active()
                except OSError as e:
                    failure_stage = "persistence"
                    failure_message = f"Could not clear recovery state: {e}"
                self.logger.info(msg="No workout data recorded")

            self.logger.info(msg="Session completed")

        csv_path = str(self.csv_path) if self.csv_path else None
        if self._persistence_error is not None:
            return WorkerResult(
                outcome="partial" if self.data_point_count > 0 else "failed",
                stage="persistence",
                message=self._persistence_error,
                csv_path=csv_path,
                durable_rows=(
                    self.csv_writer.records_written if self.csv_writer else 0
                ),
                retryable=True,
            )
        if failure_stage is not None:
            return WorkerResult(
                outcome="partial" if self.data_point_count > 0 else "failed",
                stage=failure_stage,
                message=failure_message or "Workout failed",
                csv_path=csv_path,
                durable_rows=(
                    self.csv_writer.records_written if self.csv_writer else 0
                ),
                retryable=True,
            )
        if cancelled or self.data_point_count == 0:
            return WorkerResult(
                outcome="cancelled",
                stage="recording",
                message=failure_message or "Workout ended without recorded samples",
                csv_path=csv_path,
            )
        return WorkerResult(
            outcome="success",
            stage="recording",
            message="Workout recorded successfully",
            csv_path=csv_path,
            durable_rows=self.csv_writer.records_written if self.csv_writer else 0,
        )

    def _setup_csv_writer(self) -> None:
        """Setup CSV writer with output path."""
        base_dir = Path.home() / "KayakFit" / self.config.get("output_dir", "workouts")
        session_start = self.session_start or datetime.now()
        try:
            paths = WorkoutPaths.reserve(base_dir, session_start)
        except OSError as exc:
            raise CsvWriteError(str(exc)) from exc
        try:
            self.csv_writer = CsvWriter(output_path=str(paths.csv), config=self.config)
            self.csv_path = str(paths.csv)
            save_workout_metadata(
                paths.csv,
                self.config,
                "planned" if self.program_runner is not None else "free",
            )
            # Mark this workout active so an interrupted session can be recovered
            # on the next launch. The GUI clears the marker once it is safely exported.
            recovery.mark_active(paths.directory, started_at=session_start)
        except (OSError, ValueError) as exc:
            # The directory was reserved exclusively for this failed setup, so
            # clean it up rather than leaving a misleading empty workout behind.
            if self.csv_writer is not None:
                with contextlib.suppress(Exception):
                    self.csv_writer.close()
            with contextlib.suppress(OSError):
                paths.csv.unlink()
            with contextlib.suppress(OSError):
                paths.metadata.unlink()
            with contextlib.suppress(OSError):
                paths.directory.rmdir()
            self.csv_writer = None
            self.csv_path = None
            raise CsvWriteError(f"Could not initialize workout storage: {exc}") from exc

    async def _resolve_hrm_device(self, address: str) -> BLEDevice | None:
        """Find the configured HRM before another BLE connection is active."""
        timeout = float(self.config.get("ble_connect_timeout", 15))
        for attempt in range(1, _HRM_DISCOVERY_ATTEMPTS + 1):
            self.logger.info(
                msg=f"Locating HRM at {address} "
                f"(attempt {attempt}/{_HRM_DISCOVERY_ATTEMPTS})..."
            )
            try:
                device = await BleakScanner.find_device_by_address(
                    address, timeout=timeout
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger.warning(
                    msg=f"HRM discovery attempt "
                    f"{attempt}/{_HRM_DISCOVERY_ATTEMPTS} failed: {exc}"
                )
            else:
                if device is not None:
                    self.logger.info(msg=f"✓ Located HRM at {address}")
                    return device
                self.logger.warning(
                    msg=f"HRM discovery attempt "
                    f"{attempt}/{_HRM_DISCOVERY_ATTEMPTS} did not find {address}"
                )

            if attempt < _HRM_DISCOVERY_ATTEMPTS:
                await asyncio.sleep(_HRM_DISCOVERY_RETRY_DELAY_S)

        return None

    async def _connect_devices(self) -> None:
        """Resolve the optional HRM, then connect the ergometer and HRM."""
        hrm_address = str(self.config.get("hrm_mac", "")).strip()
        use_hrm = bool(hrm_address) and self._hrm_enabled()
        resolved_hrm: BLEDevice | None = None

        # Resolving an address inside BleakClient.connect() triggers an implicit
        # scan. Do that explicitly before the ergometer link is open: some macOS
        # and Windows adapters intermittently miss the strap while another BLE
        # connection is active.
        if use_hrm:
            self._emit_status(event="connecting", device="hrm")
            resolved_hrm = await self._resolve_hrm_device(hrm_address)
            if resolved_hrm is None:
                self.logger.warning(msg=f"✗ Failed to locate HRM at {hrm_address}")
                if not await self._confirm_continue_without_hrm():
                    self._emit_status(event="absent", device="hrm")
                    raise WorkoutAbortedError(
                        "Workout cancelled: heart-rate monitor not reachable."
                    )
                self.logger.info(msg="Will continue without HRM")
                self._emit_status(event="absent", device="hrm")
                use_hrm = False
        else:
            self._emit_status(event="absent", device="hrm")

        self.logger.info(
            msg=f"Connecting to KayakFirst at {self.config['ergometer_mac']}..."
        )
        self._emit_status(event="connecting", device="ergometer")

        self.ergometer = KayakFirstErgometer(
            address=self.config["ergometer_mac"],
            person_weight=self.config["person_weight_default"],
            boat_weight=self.config["boat_weight_default"],
            display_config=self.config["display_config_numbers"],
            log_level=self.config["log_level"],
            connect_timeout=float(self.config.get("ble_connect_timeout", 15)),
            connect_retries=int(self.config.get("ble_connect_retries", 3)),
            auto_reconnect=self._ble_auto_reconnect(),
            reconnect_delay=float(self.config.get("ble_reconnect_delay", 5)),
            status_callback=lambda ev: self._emit_status(**ev),
        )

        await self.ergometer.connect(callback=self._on_ergometer_data)
        self._emit_status(event="connected", device="ergometer")

        if use_hrm and resolved_hrm is not None:
            try:
                self.logger.info(
                    msg=f"Connecting to HRM at {hrm_address}..."
                )
                self._emit_status(event="connecting", device="hrm")
                self.hrm = HeartRateMonitor(
                    address=hrm_address,
                    ble_device=resolved_hrm,
                    log_level=self.config["log_level"],
                    connect_timeout=float(self.config.get("ble_connect_timeout", 15)),
                    # Fewer attempts at startup so a missing strap surfaces the
                    # "continue without HRM?" prompt quickly instead of hanging
                    # for the full retry budget. Mid-session auto-reconnect still
                    # keeps retrying once a workout is underway.
                    connect_retries=int(self.config.get("hrm_connect_retries", 1)),
                    auto_reconnect=self._ble_auto_reconnect(),
                    reconnect_delay=float(self.config.get("ble_reconnect_delay", 5)),
                    status_callback=lambda ev: self._emit_status(**ev),
                )
                await self.hrm.connect(callback=self._on_heart_rate_data)
                self.logger.info(msg="✓ Connected to HRM")
                self._emit_status(event="connected", device="hrm")
            except Exception as e:
                self.logger.warning(msg=f"✗ Failed to connect to HRM: {e}")
                self.hrm = None
                if not await self._confirm_continue_without_hrm():
                    self._emit_status(event="absent", device="hrm")
                    raise WorkoutAbortedError(
                        "Workout cancelled: heart-rate monitor not reachable."
                    ) from e
                self.logger.info(msg="Will continue without HRM")
                self._emit_status(event="absent", device="hrm")

    async def _confirm_continue_without_hrm(self) -> bool:
        """Ask the user whether to start the workout without the configured HRM.

        Returns True (proceed) when there is no prompt callback wired up, so
        headless/CLI runs keep their previous "continue without HRM" behaviour.
        The prompt is blocking, so it runs in a worker thread to keep the event
        loop responsive.
        """
        if self.prompt_callback is None:
            return True
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            self.prompt_callback,
            "Heart-rate monitor not found",
            "The configured heart-rate monitor could not be connected.\n\n"
            "Start the workout anyway without heart rate?",
        )

    async def _watchdog(self) -> None:
        """Warn on a data stall, and auto-stop after prolonged inactivity."""
        while True:
            await asyncio.sleep(delay=1)
            now = time.monotonic()

            if self._last_data_monotonic is not None and not self._stalled:
                gap = now - self._last_data_monotonic
                if gap > self.data_stall_timeout:
                    self._stalled = True
                    self.logger.warning(
                        msg=f"No ergometer data for {int(gap)}s while connected."
                    )
                    self._emit_status(event="stalled", device="ergometer")

            # Inactivity auto-stop: no forward movement for the configured time.
            if (
                self.inactivity_autostop_s > 0
                and self._last_movement_monotonic is not None
                and not self._auto_stopped_inactive
            ):
                idle = now - self._last_movement_monotonic
                if idle > self.inactivity_autostop_s:
                    self._auto_stopped_inactive = True
                    minutes = int(self.inactivity_autostop_s // 60)
                    self.logger.warning(
                        msg=f"No movement for {minutes} min — auto-stopping and saving."
                    )
                    self._emit_status(
                        event="inactivity_autostop", device="session", minutes=minutes
                    )
                    self.stop_event.set()
                    return

    def _on_heart_rate_data(self, heart_rate: int) -> None:
        """Callback for heart rate updates from HRM.

        Args:
            heart_rate: Heart rate in BPM.
        """
        self.latest_heart_rate = heart_rate
        self.latest_heart_rate_time = time.monotonic()
        self.logger.debug(msg=f"HR: {heart_rate} bpm")

    def _report_persistence_error(self, message: str) -> None:
        """Record and surface the first failure to persist workout data."""
        if self._persistence_error is not None:
            return
        self._persistence_error = message
        if self.csv_path:
            try:
                recovery.mark_partial(
                    WorkoutPaths.from_csv(self.csv_path).directory, message
                )
            except OSError as exc:
                self.logger.error(msg=f"Could not update recovery state: {exc}")
        saved_rows = self.csv_writer.records_written if self.csv_writer else 0
        self.logger.error(
            msg=f"Workout recording stopped after a storage failure: {message}"
        )
        self._emit_status(
            event="persistence_error",
            device="session",
            message=message,
            saved_rows=saved_rows,
        )

    def _on_ergometer_data(self, data: dict[str, Any]) -> None:
        """Callback for ergometer data updates.

        Args:
            data: Dictionary of ergometer values.
        """
        now_monotonic = time.monotonic()
        if self._recording_timeline is None:
            raise RuntimeError("Recording timeline is not initialized")
        timing = self._recording_timeline.advance(
            device_elapsed_s=data.get("elapsed_time__s"),
            device_window_s=data.get("window_size__s"),
            receipt_monotonic_s=now_monotonic,
        )
        data["timestamp"] = timing.timestamp_ms
        data["session_elapsed__s"] = timing.session_elapsed_s
        data["sample_duration__s"] = timing.sample_duration_s

        # Feed the stall watchdog; announce recovery if data had stopped.
        self._last_data_monotonic = time.monotonic()
        if self._stalled:
            self._stalled = False
            self.logger.info(msg="Ergometer data resumed.")
            self._emit_status(event="streaming", device="ergometer")

        # The same authoritative signal that owns FIT timer time also resets
        # inactivity. Cumulative distance can reset after a reconnect and must
        # not keep a genuinely active athlete below an old session maximum.
        try:
            active_paddling = float(data["active_paddling"]) > 0
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Ergometer packet is missing its activity signal") from exc
        if active_paddling:
            self._last_movement_monotonic = time.monotonic()

        # Only attach a heart-rate reading if it is recent. After the strap
        # drops, the last value would otherwise be written to every row forever.
        if self.latest_heart_rate is not None and (
            self.hrm_stale_timeout <= 0
            or self.latest_heart_rate_time is None
            or (time.monotonic() - self.latest_heart_rate_time)
            <= self.hrm_stale_timeout
        ):
            data["heart_rate__bpm"] = self.latest_heart_rate

        if self.csv_writer:
            try:
                self.csv_writer.write_record(data=data)
            except CsvWriteError as e:
                self._report_persistence_error(str(e))
                self.stop_event.set()
                return

        # Sidecars may only advance through the last durable CSV row. If the
        # write above fails, teardown keeps the preceding performed endpoint.
        self._last_session_elapsed_s = timing.session_elapsed_s
        self.data_point_count += 1

        if not self._streaming_announced:
            self._streaming_announced = True
            self._emit_status(event="streaming", device="ergometer")

        # Live autopause / lap detection, mirroring the FIT exporter's rules.
        seg = self.segmenter.update(
            active=data.get("active_paddling"),
            gap_before_s=timing.gap_before_s,
        )
        if seg["transition"] is not None:
            if seg["transition"] == "pause":
                self.logger.info(msg="⏸ Auto-paused (no movement).")
            elif seg["transition"] == "resume":
                self.logger.info(msg=f"▶ Resumed — lap {seg['lap']}.")
            self._emit_status(
                event="segment",
                device="session",
                paused=seg["paused"],
                lap=seg["lap"],
                transition=seg["transition"],
            )

        # Structured program progression (if a program is running).
        if self.program_runner is not None:
            prog = self.program_runner.update(
                elapsed_s=data.get("session_elapsed__s"),
                distance_m=data.get("distance__m"),
                sample_duration_s=data.get("sample_duration__s"),
            )
            transitions = self._program_transition_events(prog)
            if transitions:
                for transition_state in transitions:
                    self._emit_program_status(transition_state)
                    self._record_step_boundary(transition_state)
                self._persist_program_steps(close_open=False)
            else:
                self._emit_program_status(prog)
            program_done = any(
                item.get("transition") == "program_complete" for item in transitions
            )

            # Manual advance requested from the UI (open steps / skip)?
            if (
                not program_done
                and self.advance_event is not None
                and self.advance_event.is_set()
            ):
                self.advance_event.clear()
                stepped = self.program_runner.advance()
                stepped_events = self._program_transition_events(stepped)
                for transition_state in stepped_events:
                    self._emit_program_status(transition_state)
                    self._record_step_boundary(transition_state)
                if stepped_events:
                    self._persist_program_steps(close_open=False)
                program_done = any(
                    item.get("transition") == "program_complete"
                    for item in stepped_events
                )

            if program_done:
                self.logger.info(msg="Program complete — stopping and saving.")
                self.stop_event.set()
            else:
                # Keep the open step's end boundary current so an interrupted
                # session still has a complete, conservative lap timeline.
                self._persist_program_steps(close_open=False, force=False)

        # Push every data point to the live UI readout (if any).
        if self.data_callback:
            # Never let UI plumbing break data collection.
            with contextlib.suppress(Exception):
                self.data_callback(data)

        if self.data_point_count % self.config.get("log_interval", 5) == 0:
            self._log_current_data(data=data)

    def _log_current_data(self, data: dict[str, Any]) -> None:
        """Log current workout data to console.

        Args:
            data: Latest data dictionary.
        """
        metrics = []

        if data.get("session_elapsed__s") is not None:
            # int() guard: the field arrives as Decimal when the firmware sends
            # it with a fraction, and Decimal rejects the "d" format code.
            elapsed = int(data["session_elapsed__s"])
            mins = elapsed // 60
            secs = elapsed % 60
            metrics.append(f"Time: {mins:02d}:{secs:02d}")

        if data.get("distance__m"):
            metrics.append(f"Dist: {data['distance__m']:.1f}m")

        if data.get("speed_instant__mps"):
            speed_kmh = data["speed_instant__mps"] * Decimal(value="3.6")
            metrics.append(f"Speed: {speed_kmh:.1f}km/h")

        if data.get("cadence_instant__spm"):
            metrics.append(f"SPM: {data['cadence_instant__spm']}")

        if data.get("pull_force_instant__n"):
            metrics.append(f"Pull Force: {data['pull_force_instant__n']}N")

        if data.get("heart_rate__bpm"):
            metrics.append(f"HR: {data['heart_rate__bpm']}bpm")

        if metrics:
            self.logger.info(msg=" | ".join(metrics))

    def _print_workout_summary(self, workout_df: Table) -> None:
        """Print workout summary statistics from the recorded data.

        Args:
            workout_df: Pre-read workout data.
        """
        try:
            if workout_df.height == 0:
                self.logger.info(msg="No workout data to summarize")
                return

            self.logger.info(msg="\n" + "=" * 60)
            self.logger.info(msg="WORKOUT SUMMARY")
            self.logger.info(msg="=" * 60)

            summary = compute_summary(
                workout_df,
                max_hr=self.config.get("max_hr", 185),
                zones=(
                    list(self.config.get("hr_zones") or []) or None
                    if str(self.config.get("hr_zone_mode", "auto")).lower() == "manual"
                    else None
                ),
                program_steps=load_program_steps(self.csv_path) if self.csv_path else None,
                config=self.config,
            )
            total_distance = summary.distance_m
            active_time = summary.active_time_s
            elapsed_time = summary.elapsed_time_s

            mins = int(active_time // 60)
            secs = int(active_time % 60)

            self.logger.info(msg=f"Active time: {mins:02d}:{secs:02d}")
            self.logger.info(msg=f"Elapsed:     {elapsed_time:.1f} s")
            self.logger.info(msg=f"Distance:    {total_distance:.1f} m")
            self.logger.info(msg=f"Data Points: {workout_df.height}")
            self.logger.info(msg="=" * 60 + "\n")

            self._emit_status(
                event="summary",
                device="session",
                distance_m=total_distance,
                duration_s=int(active_time),
                active_time_s=active_time,
                elapsed_time_s=elapsed_time,
                pause_time_s=summary.pause_time_s,
                record_count=int(workout_df.height),
                csv_path=self.csv_path,
                incomplete=self._persistence_error is not None,
                persistence_error=self._persistence_error,
            )

        except Exception as e:
            self._summary_error = str(e)
            self.logger.error(msg=f"Failed to generate workout summary: {e}")
            traceback.print_exc()
