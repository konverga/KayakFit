"""Workout start/stop lifecycle, live metrics, summary and recovery.

``RecordingLifecycleMixin`` is mixed into ``KayakFitGUI`` (see ``gui.main_gui``).
It defines no ``__init__`` and holds no state of its own; every ``self``
attribute it uses is created on the main window. Those shared attributes/methods
are declared in ``gui._mixin_base.GuiSharedState``, which this mixin inherits
under ``TYPE_CHECKING`` only (so mypy resolves them) — no runtime base class.
"""

import os
import shutil
import stat
import time
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import messagebox
from typing import TYPE_CHECKING, Any

import customtkinter as ctk

from app import recovery
from app.events import UiEvent
from app.workout_paths import WorkoutPaths

from .dashboard_constants import (
    DANGER_BTN,
    DANGER_BTN_HOVER,
    DATA_STALE_SECONDS,
    HR_COLOR,
    HR_STALE_SECONDS,
)
from .metrics_format import fmt_time, format_metrics, format_pace, metric_value
from .setup_wizard import open_setup_wizard

# See gui/_mixin_base.py: type-checking-only shared surface for the composed GUI.
if TYPE_CHECKING:
    from ._mixin_base import GuiSharedState as _MixinBase
else:
    _MixinBase = object


def _retry_readonly_removal(
    func: Callable[..., Any], path: str, exc: BaseException
) -> None:
    """Make a read-only path writable and retry its failed removal once."""
    if not isinstance(exc, PermissionError):
        raise exc
    os.chmod(path, os.stat(path).st_mode | stat.S_IWRITE)
    func(path)


def _remove_workout_directory(directory: Path) -> None:
    """Remove an owned workout directory, including read-only CSV files."""
    shutil.rmtree(directory, onexc=_retry_readonly_removal)


class RecordingLifecycleMixin(_MixinBase):
    """Event dispatch, metrics, start/stop, stop-confirm, summary, recovery."""

    def _on_event(self, ev: UiEvent) -> None:
        """Handle a structured event from the worker (runs on the GUI thread)."""
        kind = ev.get("type")
        if kind == "metrics":
            self._apply_metrics(ev.get("data", {}))
        elif kind == "status":
            self._apply_status(ev)
        elif kind == "workout_result":
            self._set_recording(False)
            outcome = ev.get("outcome")
            if outcome == "success":
                # The thread can still be tearing down; _auto_export waits until
                # WorkerManager releases ownership.
                self.root.after(300, self._auto_export)
            elif outcome in ("failed", "partial"):
                messagebox.showerror(
                    "Workout not completed",
                    str(ev.get("message") or "The workout did not complete."),
                )
        elif kind == "export_result":
            self._apply_upload(ev)
            self.export.running = False
            self._set_export_buttons(enabled=True)

    def _apply_metrics(self, data: dict[str, Any]) -> None:
        now = time.monotonic()
        self._last_data_ts = now
        self._latest_data = data

        data_points = str(self.cfg.get("data_points", "instant"))
        for key, value in format_metrics(
            data, data_points, self.cfg.get("pull_length_m")
        ).items():
            self.metric_vars[key].set(value)

        self._refresh_pace()

        # Running secondary stats are a preview of the final FIT domain: do not
        # include authoritative idle packets, zero/dropout readings, or ignore a
        # sample's represented duration.
        if data.get("active_paddling") != 0:
            try:
                weight = float(data.get("window_size__s", 1))
            except (TypeError, ValueError):
                weight = 1.0
            if weight <= 0:
                weight = 1.0
            for key in self.metric_sub_vars:
                raw = self._raw_metric(key, data)
                if raw is not None and raw > 0:
                    self._update_stat(key, raw, weight)

        hr = data.get("heart_rate__bpm")
        if hr is not None:
            self._last_hr_ts = now
            self._hr_dimmed = False
            bat = f" · {self._hrm_battery}%" if self._hrm_battery is not None else ""
            low = self._hrm_battery is not None and self._hrm_battery <= 15
            self._set_chip(
                self.hrm_chip, f"HRM · {int(hr)} bpm{bat}", "warn" if low else "ok"
            )
            self._set_hr_zone(int(hr))

        # Live target-adherence badge (only while a program step has a target).
        if self.program.target:
            self._update_target_indicator(data)

    def _refresh_pace(self) -> None:
        """Update the pace tile for the currently selected race distance."""
        pace = format_pace(
            self._latest_data,
            self._pace_distance,
            str(self.cfg.get("data_points", "instant")),
        )
        self.metric_vars["pace"].set(pace or "—")

    def _on_pace_distance(self, choice: str) -> None:
        """Switch the pace tile between 200 / 500 / 1000 m.

        Persistence is deferred to app exit so a UI tap never blocks on a
        keychain/file write (save_config now touches the OS credential vault).
        """
        try:
            self._pace_distance = int(str(choice).split()[0])
        except (TypeError, ValueError, IndexError):
            return
        self.cfg["pace_distance"] = self._pace_distance
        self._refresh_pace()

    def _raw_metric(self, key: str, data: dict[str, Any]) -> float | None:
        """Extract the numeric value for a stats tile, matching its display units."""
        return metric_value(
            key,
            data,
            str(self.cfg.get("data_points", "instant")),
            self.cfg.get("pull_length_m"),
        )

    def _update_stat(self, key: str, value: float, weight: float = 1.0) -> None:
        """Accumulate avg/max for a tile and refresh its secondary line."""
        st = self._stats.setdefault(key, {"sum": 0.0, "weight": 0.0, "max": None})
        st["sum"] += value * weight
        st["weight"] += weight
        st["max"] = value if st["max"] is None else max(st["max"], value)
        avg = st["sum"] / st["weight"]
        avg_label = "sample avg" if key == "speed" else "avg"
        self.metric_sub_vars[key].set(
            f"{avg_label} {self._fmt_stat(key, avg)}  ·  max {self._fmt_stat(key, st['max'])}"
        )

    @staticmethod
    def _fmt_stat(key: str, value: float) -> str:
        return f"{value:.1f}" if key == "speed" else f"{round(value)}"

    def _update_live_label(self) -> None:
        """Refresh the live indicator, giving the paused state priority."""
        if not self.recording.active:
            return
        if self.recording.paused:
            self.live_label.configure(text="⏸ Paused", text_color=HR_COLOR)
        else:
            self.live_label.configure(text="● live", text_color=("#1d7a4f", "#7ee0ab"))

    def _tick(self) -> None:
        """Once-a-second housekeeping: liveness indicator and HR staleness."""
        try:
            if self.recording.active and self._last_data_ts is not None:
                gap = time.monotonic() - self._last_data_ts
                if gap > DATA_STALE_SECONDS:
                    self.live_label.configure(
                        text=f"⚠ no data for {int(gap)}s", text_color=HR_COLOR
                    )
                else:
                    # Paused state takes priority over the generic "live" tag.
                    self._update_live_label()

            if (
                self.recording.active
                and not self._hr_dimmed
                and self._last_hr_ts is not None
                and (time.monotonic() - self._last_hr_ts) > HR_STALE_SECONDS
            ):
                self._hr_dimmed = True
                self._clear_hr_zone()
                self.metric_vars["hr"].set("—")
                self._set_chip(self.hrm_chip, "HRM · no signal", "muted")
        finally:
            self.root.after(1000, self._tick)

    def _focused_widget(self) -> Any | None:
        """Return the focused widget, or None.

        ``focus_get()`` raises KeyError when focus is in a dropdown's popdown
        window (a Tk widget with no Python wrapper — happens with option menus,
        especially on Windows), so it must be guarded.
        """
        try:
            return self.root.focus_get()
        except (KeyError, tk.TclError):
            return None

    def _on_space(self, event: Any) -> str | None:
        """Spacebar toggles the workout, unless typing or focused on a button.

        A button receiving space already fires its own command, so toggling as
        well would double-act (e.g. stop then immediately restart).
        """
        widget = self._focused_widget()
        if isinstance(widget, tk.Entry):
            return None
        node = widget
        while node is not None:
            if isinstance(node, ctk.CTkButton):
                return None
            node = getattr(node, "master", None)
        self._toggle_workout()
        return "break"

    def _on_advance_key(self, event: Any) -> str | None:
        """Right-arrow advances the current program step (not while typing)."""
        if isinstance(self._focused_widget(), tk.Entry):
            return None
        if self.recording.active and self.program_panel.winfo_ismapped():
            self._advance_program()
            return "break"
        return None

    def _parse_weights(self) -> tuple[int, int] | None:
        try:
            boat = int(self.boat_weight_var.get())
            person = int(self.person_weight_var.get())
        except ValueError:
            self.input_error_var.set("Boat and person weights must be whole numbers.")
            self.boat_entry.focus_set()
            return None
        if not (1 <= boat <= 25):
            self.input_error_var.set("Boat weight must be between 1 and 25 kg.")
            self.boat_entry.focus_set()
            return None
        if not (1 <= person <= 200):
            self.input_error_var.set("Person weight must be between 1 and 200 kg.")
            self.person_entry.focus_set()
            return None
        self.input_error_var.set("")
        return boat, person

    def _toggle_workout(self) -> None:
        if self.recording.active:
            # Two-tap confirm: the first tap arms, a second within the window
            # actually stops. Prevents a stray click/space ending the workout.
            if not self.recording.stop_armed:
                self._arm_stop()
            else:
                self._disarm_stop()
                if self.worker_mgr is not None:
                    self.worker_mgr.stop_worker()
            return

        weights = self._parse_weights()
        if weights is None:
            return
        boat, person = weights

        # ConfigManager already normalizes any "Name (address)" label on load.
        erg_mac = str(self.cfg.get("ergometer_mac", "")).strip()
        if not erg_mac:
            self.log_message(
                "No ergometer configured. Open Settings to scan and select one.\n"
            )
            if messagebox.askyesno(
                "No ergometer configured",
                "No ergometer is set up yet. Open Settings to scan for and select "
                "your ergometer now?",
            ):
                self.open_config()
            return

        selected_label = self.program_var.get()
        selected = self._selected_program_name(selected_label)
        program = self._programs.get(selected)  # None for "Free workout"
        # A program workout reports laps step-based afterwards (summary + FIT),
        # so its live lap chip is suppressed to keep the two consistent.
        self.program.active = program is not None
        if program is not None:
            self.log_message(f"Program: {program.name} ({len(program)} steps).\n")
        elif selected_label != self._free_label:
            # Selected plan's JSON was removed and the dropdown hasn't
            # refreshed yet: fall back explicitly rather than silently.
            self.log_message(
                f"Program '{selected}' no longer exists — starting a free workout.\n"
            )

        self._reset_dashboard()
        # Only flip to the recording state once the worker actually started;
        # start_worker refuses while the previous worker thread is still
        # tearing down, and a refused start must not wedge the UI.
        if self.worker_mgr is not None and self.worker_mgr.start_worker(
            worker_type="workout",
            args={
                "boat_weight": boat,
                "person_weight": person,
                "ergometer_mac": erg_mac,
                "program": program,
            },
        ):
            self._set_recording(True)

    def _set_recording(self, recording: bool) -> None:
        self.recording.active = recording
        if recording:
            self._screen_awake.start()
            self.primary_btn.configure(
                text="Stop & save", fg_color=DANGER_BTN, hover_color=DANGER_BTN_HOVER
            )
            self.workouts_btn.configure(state="disabled")
            self.program_menu.configure(state="disabled")
            self.recording_pill.pack(side="left", padx=(0, 6))
            self.live_label.configure(text="● live", text_color=("#1d7a4f", "#7ee0ab"))
            self.live_label.pack(side="left", padx=(0, 12))
        else:
            self._screen_awake.stop()
            self._disarm_stop()
            self.primary_btn.configure(
                text="Start workout",
                fg_color=self._primary_fg,
                hover_color=self._primary_hover,
            )
            self.workouts_btn.configure(state="normal")
            self.program_menu.configure(state="normal")
            self.recording_pill.pack_forget()
            self.live_label.pack_forget()
            self.lap_label.pack_forget()
            self.program.active = False
            self.recording.paused = False
            self._restore_tile_brightness()
            self._reset_device_chips()
            self._clear_hr_zone()
            self._clear_target_indicator()
            self._hide_program_panel()
            if self.recording.persistence_error is None:
                self._hide_banner()

    # Seconds the "Stop" button stays armed awaiting a confirming second tap.
    STOP_CONFIRM_SECONDS = 3

    def _arm_stop(self) -> None:
        """Arm the stop button; it reverts if not confirmed within the window."""
        self.recording.stop_armed = True
        self.primary_btn.configure(text="Tap again to stop")
        self.log_message("Tap Stop again (or press space) to end the workout.\n")
        if self.recording.stop_timer_id is not None:
            self.root.after_cancel(self.recording.stop_timer_id)
        self.recording.stop_timer_id = self.root.after(
            self.STOP_CONFIRM_SECONDS * 1000, self._disarm_stop
        )

    def _disarm_stop(self) -> None:
        """Cancel a pending stop confirmation and restore the button label."""
        if self.recording.stop_timer_id is not None:
            self.root.after_cancel(self.recording.stop_timer_id)
            self.recording.stop_timer_id = None
        if self.recording.stop_armed:
            self.recording.stop_armed = False
            if self.recording.active:
                self.primary_btn.configure(text="Stop & save")

    def _reset_dashboard(self) -> None:
        for var in self.metric_vars.values():
            var.set("—")
        for var in self.metric_sub_vars.values():
            var.set("")
        self._stats.clear()
        self._latest_data = {}
        self._clear_hr_zone()
        self._last_data_ts = None
        self._last_hr_ts = None
        self._hr_dimmed = False
        self.recording.paused = False
        self.recording.lap = 0
        self.program.ending_warned = False
        self._hrm_battery = None
        self.lap_label.configure(text="")
        self.lap_label.pack_forget()
        self._restore_tile_brightness()
        self._clear_target_indicator()
        self._hide_banner()
        if self.summary_card.winfo_ismapped():
            self.summary_card.grid_remove()
        self.strava_btn.pack_forget()
        self.upload_btn.pack_forget()
        self.savefit_btn.pack_forget()
        self.view_summary_btn.pack_forget()
        self.discard_btn.pack_forget()
        self.recording.reset_workout()
        self.export.strava_activity_id = None

    def _show_summary(self, ev: dict[str, Any]) -> None:
        dist = ev.get("distance_m", 0)
        dur = fmt_time(ev.get("duration_s", 0))
        points = ev.get("data_points", 0)
        self.recording.csv_path = ev.get("csv_path")
        self.recording.points = points
        self.recording.incomplete = bool(ev.get("incomplete"))
        outcome = (
            "Partial workout retained" if self.recording.incomplete else "Workout saved"
        )
        self.summary_label.configure(
            text=f"{outcome}   ·   {dist:,.0f} m   ·   {dur}   ·   {points} data points"
        )
        # Offer one-click actions for this workout when we have a data file.
        # Uniform right-pad on every button (they pack right-to-left) keeps the
        # gaps between them even.
        if self.recording.csv_path and points > 0:
            self.savefit_btn.pack(side="right", padx=(0, 12), pady=12)
            if self._strava_connected() and not self.recording.incomplete:
                self.upload_btn.pack(side="right", padx=(0, 12), pady=12)
            self.view_summary_btn.pack(side="right", padx=(0, 12), pady=12)
            self.discard_btn.pack(side="right", padx=(0, 12), pady=12)
        self.summary_card.grid(row=5, column=0, sticky="ew", padx=14, pady=(0, 6))

    def _dismiss_summary(self) -> None:
        """Hide the post-workout summary card and its action buttons.

        The saved CSV is untouched; the workout can still be re-exported later
        from the Workouts window.
        """
        if self.summary_card.winfo_ismapped():
            self.summary_card.grid_remove()
        self.strava_btn.pack_forget()
        self.upload_btn.pack_forget()
        self.savefit_btn.pack_forget()
        self.view_summary_btn.pack_forget()
        self.discard_btn.pack_forget()
        # The user has seen and closed the summary; the CSV remains in Workouts,
        # so this workout no longer needs the crash-recovery prompt.
        try:
            recovery.clear_active()
        except OSError as exc:
            self._show_banner(f"Could not clear workout recovery state — {exc}", "warn")

    def _discard_workout(self) -> None:
        """Permanently delete the just-finished workout after confirmation.

        Removes the owned workout directory so no associated artifact reaches
        the Workouts window, then clears the summary card and recovery marker.
        """
        if self.export.running or (self.worker_mgr and self.worker_mgr.is_running()):
            self._show_banner("Wait for the current export or upload to finish.", "warn")
            return
        csv_path = self.recording.csv_path
        if not csv_path:
            self._dismiss_summary()
            return
        if not messagebox.askyesno(
            "Discard workout",
            "Delete this workout permanently?\n\n"
            "All files for this workout will be removed and "
            "cannot be recovered.",
        ):
            return
        removed = False
        try:
            paths = WorkoutPaths.from_csv(csv_path)
            workouts_root = (
                Path.home() / "KayakFit" / self.cfg.get("output_dir", "workouts")
            )
            if not paths.is_owned_by(workouts_root):
                raise ValueError("refusing to delete a path outside the workout library")
            _remove_workout_directory(paths.directory)
            removed = True
        except Exception as e:  # never let a failed delete crash the UI
            self._show_banner(f"Could not discard workout — {e}", "warn")
            return
        try:
            recovery.clear_active()
        except OSError as exc:
            self._show_banner(f"Could not clear workout recovery state — {exc}", "warn")
        if self.summary_card.winfo_ismapped():
            self.summary_card.grid_remove()
        self.strava_btn.pack_forget()
        self.upload_btn.pack_forget()
        self.savefit_btn.pack_forget()
        self.view_summary_btn.pack_forget()
        self.discard_btn.pack_forget()
        self.recording.csv_path = None
        self.export.strava_activity_id = None
        self.log_message(
            "🗑 Workout discarded.\n" if removed else "Workout already removed.\n"
        )

    def _maybe_show_wizard(self) -> None:
        """Show the first-run setup wizard once (until it's completed/skipped)."""
        if self.cfg.get("wizard_seen"):
            return

        def on_complete() -> None:
            self._reload_cfg()
            self._init_chips()
            self._refresh_hr_legend()

        try:
            open_setup_wizard(
                self.root,
                self.cfg,
                self.config_mgr,
                self.log_message,
                on_complete=on_complete,
                open_settings=self.open_config,
            )
        except Exception as e:  # never let onboarding block startup
            self.log_message(f"Setup wizard could not open: {e}\n")

    def _check_recovery(self) -> None:
        """On startup, offer to export a workout interrupted before it finished."""
        try:
            info = recovery.pending_recovery()
        except OSError as exc:  # never let recovery bookkeeping block startup
            self._show_banner(f"Could not inspect workout recovery state — {exc}", "warn")
            info = None
        if not info or self.recording.active:
            return

        csv_path = info.get("csv_path")
        if not csv_path or not os.path.exists(csv_path):
            try:
                recovery.clear_active()
            except OSError as exc:
                self._show_banner(
                    f"Could not clear workout recovery state — {exc}", "warn"
                )
            return

        started = info.get("started_at", "")
        when = started.replace("T", " ") if isinstance(started, str) else ""
        detail = f" started {when}" if when else ""
        if not messagebox.askyesno(
            "Recover unfinished workout",
            f"A workout{detail} was interrupted before it finished exporting.\n\n"
            "Convert it to a FIT file now?",
        ):
            try:
                recovery.clear_active()
            except OSError as exc:
                self._show_banner(
                    f"Could not clear workout recovery state — {exc}", "warn"
                )
            return

        upload = False
        if self._strava_connected():
            upload = messagebox.askyesno(
                "Upload to Strava", "Also upload this recovered workout to Strava?"
            )
        self.recording.csv_path = csv_path
        self.log_message(f"Recovering interrupted workout: {csv_path}\n")
        self._start_export(csv_path, upload=upload, announce=True)
