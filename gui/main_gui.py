"""Primary user interface for KayakFit, built with customtkinter.

During a workout the window shows a live metric dashboard fed by the worker's
per-second data; a compact, collapsible log keeps the detailed output
available.

The heavy lifting (BLE, CSV, FIT export, Strava) runs in background workers via
WorkerManager, which pushes structured UI events back onto the GUI thread.

The window's behaviour is split across focused mixin modules
(``program_panel``, ``device_status``, ``recording_lifecycle``,
``export_upload``); this file keeps construction (``__init__`` + the
layout-building methods) and the small cross-cutting helpers (log panel,
banner, appearance, history/settings windows, closing). The attributes and
methods those mixins share are declared once for the type checker in
``gui._mixin_base.GuiSharedState`` (a ``TYPE_CHECKING``-only base, no runtime
effect).
"""

import contextlib
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from typing import Any

import customtkinter as ctk

from app import program as program_mod
from app.config import get_bool
from app.keep_awake import ScreenAwake
from app.logger import Logger
from app.sound import SoundPlayer
from app.speed_series import LiveSpeedTracker

from .config_manager import ConfigManager
from .config_window import open_config_window
from .controllers import ExportController, ProgramController, RecordingController
from .dashboard_constants import (
    CHIP_COLORS,
    DASHBOARD_COLS,
    HR_COLOR,
    SUBTEXT_COLOR,
    TILES,
)
from .device_status import DeviceStatusMixin
from .export_upload import ExportUploadMixin
from .history_window import open_history_window
from .program_panel import ProgramPanelMixin
from .recording_lifecycle import RecordingLifecycleMixin
from .summary_window import open_summary_window
from .window_utils import center_on_screen, open_in_file_manager
from .worker_manager import WorkerManager


class KayakFitGUI(
    ProgramPanelMixin,
    DeviceStatusMixin,
    RecordingLifecycleMixin,
    ExportUploadMixin,
):
    """Main application window."""

    MAX_LOG_LINES = 2000

    def __init__(
        self, root: ctk.CTk, workout_worker: Callable[..., Any], export_worker: Callable[..., Any]
    ) -> None:
        self.root = root
        self.root.title("KayakFit · KayakFirst Bull Workout Companion")

        min_width, min_height = 860, 640
        center_on_screen(self.root, min_width, min_height)
        self.root.minsize(min_width, min_height)

        self.config_mgr = ConfigManager()
        self.cfg: dict[str, Any] = self.config_mgr.load_config()
        appearance = str(self.cfg.get("appearance_mode", "system")).lower()
        if appearance in ("system", "light", "dark"):
            ctk.set_appearance_mode(appearance)

        self.recording = RecordingController()
        self.program = ProgramController()
        self.export = ExportController()
        self._log_visible = bool(self.cfg.get("activity_log_visible", True))

        self.boat_weight_var = tk.StringVar(
            value=str(self.cfg.get("boat_weight_default", 12))
        )
        self.person_weight_var = tk.StringVar(
            value=str(self.cfg.get("person_weight_default", 75))
        )
        self.metric_vars = {key: tk.StringVar(value="—") for key, _, _, _ in TILES}
        self.metric_sub_vars = {
            key: tk.StringVar(value="") for key, _, _, stats in TILES if stats
        }
        # Current HR zone label (e.g. "Z3 Aerobic · 140-157 bpm") shown on the HR tile.
        self.hr_zone_var = tk.StringVar(value="")
        self.hr_legend_labels: dict[int, ctk.CTkLabel] = {}

        # Per-tile widget refs (for HR-zone recoloring) and running stats.
        self.tile_frames: dict[str, ctk.CTkFrame] = {}
        self.tile_defaults: dict[str, Any] = {}
        self.tile_value_labels: dict[str, ctk.CTkLabel] = {}
        self.tile_value_colors: dict[str, Any] = {}
        self._tiles_dimmed = False
        self._stats: dict[str, dict[str, Any]] = {}
        # Active time/distance tracker; finalized speed remains available for
        # reconciliation but dashboard speed cards use averaged device fields.
        self._live_speed = LiveSpeedTracker()

        # Liveness / staleness timing (monotonic seconds).
        self._last_data_ts: float | None = None
        self._last_hr_ts: float | None = None
        self._hr_dimmed = False

        # Live autopause / lap state (driven by the session's segmenter).
        # Whether the current recording is driven by a structured program. Set
        # when a workout starts. A program workout's laps are step-based in the
        # post-workout summary and FIT file, so the live movement-based lap
        # counter is suppressed for it (see _apply_segment) to avoid showing a
        # lap count that later contradicts the summary.

        # Latest HRM battery level (%), shown on the HRM chip when available.
        self._hrm_battery: int | None = None

        # Current program step's intensity target and adherence tracking.
        # The badge only changes once a new status has held for the grace period,
        # so second-to-second fluctuation doesn't make it flicker.
        # Fires an audible heads-up once, near the end of the current step, so
        # the athlete knows to prepare to stop without watching the screen.

        # Training programs: name -> Program, plus the current selection.
        # The bundled example plans are copied into ~/KayakFit/programs once
        # (first run) so they live alongside — and are editable like — the
        # user's own plans.
        self._free_label = "Free workout"
        # Collect per-file load issues so they can be surfaced in the activity
        # log once the log panel exists (it is built later, in _build_layout).
        self._pending_program_warnings: list[str] = []
        self._program_warn_sig: tuple[Any, ...] = ()
        try:
            program_mod.install_examples()
            warnings: list[str] = []
            self._programs = {
                p.name: p for p in program_mod.list_programs(warnings)
            }
            self._pending_program_warnings = warnings
            self._program_warn_sig = tuple(warnings)
        except Exception:
            self._programs = {}
        self._program_labels = self._build_program_labels()
        self.program_var = ctk.StringVar(value=self._free_label)

        # Two-tap stop confirmation guards against an accidental single tap or
        # spacebar press ending (and saving) the workout.

        appearance_label = appearance.capitalize()
        self.appearance_var = ctk.StringVar(value=appearance_label)

        # Pace tile: switchable competition distance (200 / 500 / 1000 m).
        pace_d = self.cfg.get("pace_distance", 500)
        self._pace_distance = pace_d if pace_d in (200, 500, 1000) else 500
        self.pace_distance_var = ctk.StringVar(value=f"{self._pace_distance} m")
        self._latest_data: dict[str, Any] = {}

        self.worker_mgr: WorkerManager | None = None

        # Keeps the display from sleeping while a workout is recording.
        self._screen_awake = ScreenAwake(log=self.log_message)

        # Audible cues on workout phase changes.
        self._sound = SoundPlayer(enabled=get_bool(self.cfg, "sound_cues", default=True))

        self._build_layout()

        # Now that the activity log exists, surface any plan-load issues from
        # startup (the detailed per-file reasons are in the logs).
        self._flush_program_warnings(self._pending_program_warnings)
        self._pending_program_warnings = []

        self.worker_mgr = WorkerManager(self.log_message, self.log_box)
        self.worker_mgr.set_workers(workout_worker, export_worker)
        self.worker_mgr.set_token_update_callback(self._on_tokens_updated)
        self.worker_mgr.set_event_callback(self._on_event)

        self._init_chips()
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        self.root.bind("<space>", self._on_space)
        self.root.bind("<Right>", self._on_advance_key)
        # Pick up newly added ~/KayakFit/programs/*.json without a restart.
        self.root.bind("<FocusIn>", self._refresh_programs)
        self.root.bind("<Configure>", self._on_root_resize)
        self.root.after(1000, self._tick)
        # First-run onboarding, then offer to recover an interrupted workout.
        self.root.after(500, self._maybe_show_wizard)
        self.root.after(800, self._check_recovery)
        if self.config_mgr.config_warning:
            warning = self.config_mgr.config_warning
            self.root.after(100, lambda: self._show_banner(warning, "warn"))

    def _build_layout(self) -> None:
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(4, weight=1)  # dashboard expands

        self._build_toolbar()  # row 0
        self._build_chips()  # row 1
        self._build_banner()  # row 2 (shown on demand)
        self._build_program_panel()  # row 3 (shown on demand)
        self._build_dashboard()  # row 4
        self._build_summary_card()  # row 5 (shown on demand)
        self._build_log_panel()  # row 6

    def _build_toolbar(self) -> None:
        bar = ctk.CTkFrame(self.root, corner_radius=0)
        bar.grid(row=0, column=0, sticky="ew")

        # A clean app-actions toolbar, a thin divider, then a separate setup bar
        # for the per-workout controls (program + weights). Both bands live in
        # the header so they always fit the minimum window width.
        actions = ctk.CTkFrame(bar, fg_color="transparent")
        actions.pack(fill="x", padx=14, pady=(12, 8))

        # ---- app actions --------------------------------------------------
        self.primary_btn = ctk.CTkButton(
            actions, text="Start workout", width=150, command=self._toggle_workout
        )
        self.primary_btn.pack(side="left", padx=(0, 8))
        # Remember theme defaults so we can restore them after the red "Stop" state.
        self._primary_fg = self.primary_btn.cget("fg_color")
        self._primary_hover = self.primary_btn.cget("hover_color")

        # One place for everything about past workouts: browse, save as FIT,
        # upload to Strava (replaces the former separate Export/Upload picker).
        self.workouts_btn = ctk.CTkButton(
            actions, text="Workouts", width=110, command=self.open_history
        )
        self.workouts_btn.pack(side="left")

        ctk.CTkButton(actions, text="Settings", width=100, command=self.open_config).pack(
            side="right"
        )
        ctk.CTkSegmentedButton(
            actions, values=["System", "Light", "Dark"], variable=self.appearance_var,
            width=180, command=self._set_appearance,
        ).pack(side="right", padx=(0, 8))

        # Recording indicators (shown on demand) sit on the actions row.
        self.recording_pill = ctk.CTkLabel(
            actions, text="● Recording", fg_color=CHIP_COLORS["warn"][0],
            text_color=HR_COLOR, corner_radius=8, padx=10, pady=2,
        )
        self.live_label = ctk.CTkLabel(
            actions, text="", text_color=SUBTEXT_COLOR, font=ctk.CTkFont(size=12)
        )
        self.lap_label = ctk.CTkLabel(
            actions, text="", fg_color=CHIP_COLORS["info"][0],
            text_color=CHIP_COLORS["info"][1], corner_radius=8, padx=10, pady=2,
            font=ctk.CTkFont(size=12),
        )

        # ---- divider ------------------------------------------------------
        ctk.CTkFrame(bar, height=1, fg_color=("#d4d4d4", "#3a3a3a")).pack(
            fill="x", padx=14
        )

        # ---- setup bar: program + weights (centered) ----------------------
        setup = ctk.CTkFrame(bar, fg_color="transparent")
        setup.pack(fill="x", padx=14, pady=(8, 12))
        setup.grid_columnconfigure(0, weight=1)
        self._setup_container = setup
        self._setup_compact = False
        controls = ctk.CTkFrame(setup, fg_color="transparent")
        controls.grid(row=0, column=0)
        row = ctk.CTkFrame(controls, fg_color="transparent")
        row.pack(side="left", padx=(0, 10))
        self._program_setup = row

        ctk.CTkLabel(
            row, text="Program", text_color=SUBTEXT_COLOR,
            font=ctk.CTkFont(size=12),
        ).pack(side="left", padx=(0, 8))
        self.program_menu = ctk.CTkOptionMenu(
            row, width=210, variable=self.program_var,
            values=[self._free_label, *list(self._program_labels)],
            command=self._on_program_selected,
        )
        self.program_menu.pack(side="left", padx=(0, 4))
        # Subtle shortcut to the programs folder: drop a JSON file in there and
        # it appears in the dropdown (refreshed on window focus).
        ctk.CTkButton(
            row, text="📂", width=30, height=28, fg_color="transparent",
            text_color=SUBTEXT_COLOR, hover_color=("#e8e8e8", "#333333"),
            command=self._open_programs_folder,
        ).pack(side="left")

        weights = ctk.CTkFrame(controls, fg_color="transparent")
        weights.pack(side="left")
        self._weights_setup = weights
        self.boat_entry = self._weight_stepper(
            weights, "Boat (kg)", self.boat_weight_var, 1, 25
        )
        self.person_entry = self._weight_stepper(
            weights, "Athlete (kg)", self.person_weight_var, 1, 200
        )
        self.program_preview_var = tk.StringVar(value="Free workout · no planned steps")
        self.program_preview_label = ctk.CTkLabel(
            setup, textvariable=self.program_preview_var, text_color=SUBTEXT_COLOR,
            font=ctk.CTkFont(size=11),
        )
        self.program_preview_label.grid(row=1, column=0, pady=(4, 0))
        self.input_error_var = tk.StringVar(value="")
        self.input_error_label = ctk.CTkLabel(
            setup, textvariable=self.input_error_var, text_color=HR_COLOR,
            font=ctk.CTkFont(size=11, weight="bold"),
        )
        self.input_error_label.grid(row=2, column=0)

    def _weight_stepper(
        self, parent: Any, label: str, var: tk.StringVar, lo: int, hi: int
    ) -> ctk.CTkEntry:
        """Build a labelled -/+ stepper around a numeric entry with live validation."""
        box = ctk.CTkFrame(parent, fg_color="transparent")
        box.pack(side="left", padx=(0, 10))
        ctk.CTkLabel(
            box, text=label, text_color=SUBTEXT_COLOR, font=ctk.CTkFont(size=12)
        ).pack(side="left", padx=(0, 8))

        def step(delta: int) -> None:
            try:
                current = int(var.get())
            except ValueError:
                current = lo
            var.set(str(min(hi, max(lo, current + delta))))

        # A connected -/value/+ unit: same height, minimal gaps, subtle buttons.
        ctk.CTkButton(
            box, text="-", width=30, height=28, fg_color=("#e4e4e4", "#333333"),
            text_color=("#1a1a1a", "#dddddd"), hover_color=("#d4d4d4", "#404040"),
            command=lambda: step(-1),
        ).pack(side="left")
        entry = ctk.CTkEntry(box, textvariable=var, width=44, height=28, justify="center")
        entry.pack(side="left", padx=1)
        ctk.CTkButton(
            box, text="+", width=30, height=28, fg_color=("#e4e4e4", "#333333"),
            text_color=("#1a1a1a", "#dddddd"), hover_color=("#d4d4d4", "#404040"),
            command=lambda: step(1),
        ).pack(side="left")

        self._entry_defaults = getattr(
            self, "_entry_defaults", entry.cget("border_color")
        )

        # Only recolor the border when validity actually flips — reconfiguring on
        # every keystroke/step forces a redraw and made the steppers "hop".
        last_ok: dict[str, bool | None] = {"value": None}

        def validate(*_a: Any) -> None:
            try:
                value = int(var.get())
                ok = lo <= value <= hi
            except ValueError:
                ok = False
            if ok == last_ok["value"]:
                return
            last_ok["value"] = ok
            entry.configure(border_color="#c0392b" if not ok else self._entry_defaults)

        var.trace_add("write", validate)
        validate()
        return entry

    def _open_programs_folder(self) -> None:
        """Open ~/KayakFit/programs in the OS file manager (best-effort)."""
        path = program_mod.programs_dir()
        try:
            path.mkdir(parents=True, exist_ok=True)
            open_in_file_manager(path)
        except Exception as e:
            self.log_message(f"Could not open programs folder: {e}\n")

    def _build_banner(self) -> None:
        self.banner = ctk.CTkFrame(self.root, corner_radius=8)
        self.banner_label = ctk.CTkLabel(
            self.banner, text="", anchor="w", font=ctk.CTkFont(size=13, weight="bold")
        )
        self.banner_label.pack(side="left", padx=12, pady=8)
        # Explicit text color: the theme's default button text is near-white,
        # which would be invisible on the banner's light background.
        ctk.CTkButton(
            self.banner,
            text="✕",
            width=28,
            fg_color="transparent",
            text_color=("#1a1a1a", "#eaeaea"),
            hover=False,
            command=self._hide_banner,
        ).pack(side="right", padx=8, pady=6)

    def _build_dashboard(self) -> None:
        panel = ctk.CTkFrame(self.root, fg_color="transparent")
        panel.grid(row=4, column=0, sticky="nsew", padx=14, pady=10)
        self._dashboard_panel = panel

        for idx, (key, label, unit, show_stats) in enumerate(TILES):
            tile = ctk.CTkFrame(panel, corner_radius=12)
            tile.grid(
                row=idx // DASHBOARD_COLS,
                column=idx % DASHBOARD_COLS,
                sticky="nsew",
                padx=6,
                pady=6,
            )
            self.tile_frames[key] = tile
            self.tile_defaults[key] = tile.cget("fg_color")
            if key == "pace":
                # Header is a distance switch rather than a static label.
                head = ctk.CTkFrame(tile, fg_color="transparent")
                head.pack(anchor="w", fill="x", padx=16, pady=(12, 0))
                ctk.CTkLabel(
                    head,
                    text="Pace",
                    text_color=SUBTEXT_COLOR,
                    font=ctk.CTkFont(size=13),
                ).pack(side="left")
                ctk.CTkSegmentedButton(
                    head,
                    values=["200 m", "500 m", "1000 m"],
                    variable=self.pace_distance_var,
                    command=self._on_pace_distance,
                    height=22,
                ).pack(side="right")
            else:
                ctk.CTkLabel(
                    tile,
                    text=label,
                    text_color=SUBTEXT_COLOR,
                    font=ctk.CTkFont(size=13),
                ).pack(anchor="w", padx=16, pady=(14, 0))
            value = ctk.CTkLabel(
                tile,
                textvariable=self.metric_vars[key],
                font=ctk.CTkFont(size=44, weight="bold"),
                text_color=HR_COLOR if key == "hr" else None,
            )
            value.pack(anchor="w", padx=16, pady=(0, 0))
            # Keep refs + original colors so tiles can be dimmed during pauses.
            self.tile_value_labels[key] = value
            self.tile_value_colors[key] = value.cget("text_color")
            if key == "hr":
                # Current zone + its bpm range, updated live from the HR reading.
                ctk.CTkLabel(
                    tile,
                    textvariable=self.hr_zone_var,
                    text_color=SUBTEXT_COLOR,
                    font=ctk.CTkFont(size=12, weight="bold"),
                ).pack(anchor="w", padx=16, pady=(0, 0))
            if unit:
                ctk.CTkLabel(
                    tile,
                    text=unit,
                    text_color=SUBTEXT_COLOR,
                    font=ctk.CTkFont(size=12),
                ).pack(anchor="w", padx=16, pady=(0, 0))
            if show_stats:
                ctk.CTkLabel(
                    tile,
                    textvariable=self.metric_sub_vars[key],
                    text_color=SUBTEXT_COLOR,
                    font=ctk.CTkFont(size=12),
                ).pack(anchor="w", padx=16, pady=(2, 12))
            else:
                ctk.CTkLabel(tile, text="").pack(anchor="w", padx=16, pady=(2, 12))
        self._layout_dashboard(DASHBOARD_COLS)

    def _layout_dashboard(self, columns: int) -> None:
        """Reflow the existing metric tiles for the normal or compact width."""
        panel = self._dashboard_panel
        for index in range(DASHBOARD_COLS):
            panel.grid_columnconfigure(index, weight=0, uniform="")
        for row in range(len(TILES) // 2):
            panel.grid_rowconfigure(row, weight=0, uniform="")
        rows = (len(TILES) + columns - 1) // columns
        for column in range(columns):
            panel.grid_columnconfigure(column, weight=1, uniform="tiles")
        for row in range(rows):
            panel.grid_rowconfigure(row, weight=1, uniform="tiles")
        for index, (key, _label, _unit, _stats) in enumerate(TILES):
            self.tile_frames[key].grid_configure(
                row=index // columns, column=index % columns
            )

    def _build_summary_card(self) -> None:
        self.summary_card = ctk.CTkFrame(self.root, corner_radius=12)
        # gridded on demand in _show_summary()
        self.summary_label = ctk.CTkLabel(
            self.summary_card, text="", font=ctk.CTkFont(size=14), justify="left"
        )
        self.summary_label.pack(side="left", padx=16, pady=12)
        # Dismiss (✕) closes the card without affecting the saved workout.
        # Explicit text color: the theme's default button text is near-white,
        # which would be invisible on the card's light background.
        ctk.CTkButton(
            self.summary_card,
            text="✕",
            width=28,
            fg_color="transparent",
            text_color=("#1a1a1a", "#eaeaea"),
            hover_color=("#d9d9d9", "#3a3a3a"),
            command=self._dismiss_summary,
        ).pack(side="right", padx=(0, 12), pady=12)
        self.strava_btn = ctk.CTkButton(
            self.summary_card,
            text="View on Strava",
            width=140,
            command=self._open_strava_activity,
        )
        # One-click actions for the just-finished workout (no file picker).
        self.upload_btn = ctk.CTkButton(
            self.summary_card,
            text="Upload to Strava",
            width=150,
            command=lambda: self._start_export(self.recording.csv_path, upload=True),
        )
        self.savefit_btn = ctk.CTkButton(
            self.summary_card,
            text="Save FIT",
            width=110,
            command=lambda: self._start_export(self.recording.csv_path, upload=False),
        )
        # Opens the detailed summary screen (charts, splits, zones) for the
        # workout that was just saved.
        self.view_summary_btn = ctk.CTkButton(
            self.summary_card,
            text="View summary",
            width=130,
            command=lambda: open_summary_window(
                self.root, self.recording.csv_path, self.cfg
            ),
        )
        # Permanently deletes the owned directory for the just-saved workout.
        self.discard_btn = ctk.CTkButton(
            self.summary_card,
            text="Discard",
            width=100,
            fg_color=("#c0392b", "#7a2018"),
            hover_color=("#a93226", "#5e150f"),
            command=self._discard_workout,
        )

    def _build_log_panel(self) -> None:
        panel = ctk.CTkFrame(self.root, corner_radius=12)
        panel.grid(row=6, column=0, sticky="ew", padx=14, pady=(0, 12))
        panel.grid_columnconfigure(0, weight=1)

        header = ctk.CTkFrame(panel, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=8, pady=(6, 0))
        ctk.CTkLabel(header, text="Activity log", font=ctk.CTkFont(size=13)).pack(
            side="left"
        )
        self.log_toggle = ctk.CTkButton(
            header, text="Hide", width=64, height=24, command=self._toggle_log
        )
        self.log_toggle.pack(side="right")
        ctk.CTkButton(
            header, text="Clear", width=64, height=24, command=self._clear_activity
        ).pack(side="right", padx=(0, 6))

        self.log_container = ctk.CTkFrame(panel, fg_color="transparent")
        self.log_container.grid(row=1, column=0, sticky="ew", padx=6, pady=6)
        self.log_container.grid_columnconfigure(0, weight=1)

        self.log_box = tk.Text(
            self.log_container,
            wrap="word",
            state="disabled",
            height=8,
            relief="flat",
            borderwidth=0,
            padx=10,
            pady=6,
        )
        self.log_box.grid(row=0, column=0, sticky="ew")
        scrollbar = ctk.CTkScrollbar(self.log_container, command=self.log_box.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_box.configure(yscrollcommand=scrollbar.set)
        self._apply_log_theme()
        if not self._log_visible:
            self.log_container.grid_remove()
            self.log_toggle.configure(text="Show")

    def _apply_log_theme(self) -> None:
        """Recolor the raw tk.Text log to match the current appearance mode."""
        dark = ctk.get_appearance_mode() == "Dark"
        bg, fg = ("#1d1e1e", "#dce4e8") if dark else ("#f7f7f5", "#1a1a1a")
        self.log_box.configure(bg=bg, fg=fg, insertbackground=fg)

    def log_message(self, message: str) -> None:
        self.log_box.config(state="normal")
        self.log_box.insert(tk.END, message)
        line_count = int(self.log_box.index("end-1c").split(".")[0])
        if line_count > self.MAX_LOG_LINES:
            self.log_box.delete("1.0", f"{line_count - self.MAX_LOG_LINES + 1}.0")
        self.log_box.see(tk.END)
        self.log_box.config(state="disabled")

        # Make sure failures are never hidden behind a collapsed log.
        if ("✗" in message or "ERROR" in message) and not self._log_visible:
            self._toggle_log()

    def _clear_activity(self) -> None:
        """Clear the activity log and reset the dashboard tiles.

        While a workout is recording, only the log text is cleared so live
        averages/maxes are not wiped mid-session; the tiles repopulate from the
        next data point regardless.
        """
        self.log_box.config(state="normal")
        self.log_box.delete("1.0", tk.END)
        self.log_box.config(state="disabled")

        if self.recording.active:
            self.log_message("Activity log cleared.\n")
            return

        for var in self.metric_vars.values():
            var.set("—")
        for var in self.metric_sub_vars.values():
            var.set("")
        self._stats.clear()
        self._live_speed = LiveSpeedTracker()
        self._latest_data = {}
        self._clear_hr_zone()
        self.lap_label.configure(text="")
        self.lap_label.pack_forget()
        self.recording.paused = False
        self.recording.lap = 0
        self._reset_device_chips()
        self._dismiss_summary()

    def _toggle_log(self) -> None:
        self._log_visible = not self._log_visible
        if self._log_visible:
            self.log_container.grid()
            self.log_toggle.configure(text="Hide")
        else:
            self.log_container.grid_remove()
            self.log_toggle.configure(text="Show")

    def _show_banner(self, text: str, kind: str = "warn") -> None:
        fg, _txt = CHIP_COLORS.get(kind, CHIP_COLORS["warn"])
        self.banner.configure(fg_color=fg)
        self.banner_label.configure(text=text)
        self.banner.grid(row=2, column=0, sticky="ew", padx=14, pady=(2, 0))

    def _hide_banner(self) -> None:
        self.banner.grid_remove()

    def _set_appearance(self, mode: str) -> None:
        ctk.set_appearance_mode(mode.lower())
        self.cfg["appearance_mode"] = mode.lower()
        # CTk resolves "system" to Light/Dark asynchronously.  The activity
        # log is a raw tk.Text, so recolor it after that resolution completes.
        # CustomTkinter polls the OS theme every 30 ms in system mode.
        self.root.after(50, self._apply_log_theme)

    def _on_root_resize(self, event: Any) -> None:
        """Apply one compact breakpoint without introducing a layout system."""
        if event.widget is not self.root:
            return
        compact = event.width < 980
        if compact == self._setup_compact:
            return
        self._setup_compact = compact
        if compact:
            self.hr_zone_legend.pack_forget()
        else:
            self.hr_zone_legend.pack(side="right", padx=(8, 0))
        self._layout_dashboard(2 if compact else DASHBOARD_COLS)

    def open_history(self) -> None:
        """Open the Workouts window (browse, save FIT, upload — one place)."""
        workouts_dir = Path.home() / "KayakFit" / self.cfg.get("output_dir", "workouts")
        workouts_dir.mkdir(parents=True, exist_ok=True)
        open_history_window(
            self.root,
            workouts_dir,
            start_export=lambda path, upload: self._start_export(path, upload),
            strava_connected=self._strava_connected(),
            open_summary=lambda path: open_summary_window(self.root, path, self.cfg),
        )

    def open_config(self) -> None:
        # Settings is a persisted-configuration view. Reload first so values
        # saved by onboarding or another worker are always reflected when the
        # window is created.
        self._reload_cfg()

        def on_config_saved() -> None:
            self._reload_cfg()
            self.boat_weight_var.set(str(self.cfg["boat_weight_default"]))
            self.person_weight_var.set(str(self.cfg["person_weight_default"]))
            self._init_chips()
            self._refresh_hr_legend()
            self._sound.set_enabled(get_bool(self.cfg, "sound_cues", default=True))
            # Apply a changed log level without requiring a restart.
            Logger.set_level(str(self.cfg.get("log_level", "info")))

        open_config_window(
            self.root, self.cfg, self.config_mgr, on_config_saved, self.log_message
        )

    def on_closing(self) -> None:
        # Persist lightweight UI preferences (e.g. pace distance) once, on exit.
        with contextlib.suppress(Exception):
            latest = self.config_mgr.load_config()
            latest["pace_distance"] = self._pace_distance
            latest["appearance_mode"] = str(
                self.cfg.get("appearance_mode", "system")
            )
            latest["activity_log_visible"] = self._log_visible
            self.config_mgr.save_config(latest)
        # Always release the stay-awake assertion on exit.
        self._screen_awake.stop()
        if self.worker_mgr and self.worker_mgr.is_running():
            self.worker_mgr.stop_worker()
            self.root.after(1000, self._finish_closing)
        else:
            self._finish_closing()

    def _finish_closing(self, attempts: int = 140) -> None:
        # Give recording teardown (CSV fsync + sidecars) and a cancellable
        # upload request enough time to reach a durable boundary before the
        # daemon worker is abandoned with the process. HTTP calls are bounded
        # at 30 s, so 35 s covers the longest normal in-flight operation.
        if self.worker_mgr and self.worker_mgr.is_running() and attempts > 0:
            self.root.after(250, lambda: self._finish_closing(attempts - 1))
            return
        if self.worker_mgr and self.worker_mgr.is_running():
            self.worker_mgr.cleanup()
        for action in (self.root.quit, self.root.destroy):
            with contextlib.suppress(Exception):
                action()
