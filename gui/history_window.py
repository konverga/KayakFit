"""Workouts Window.

The single place for everything about past workouts. It lists recorded
sessions from ``~/KayakFit/<output_dir>`` (newest first); clicking a row
selects it and a single action bar at the bottom offers **Summary**,
**Save FIT** and **Upload** for the selected file. "Choose file…" adds a
workout CSV or FIT stored elsewhere to the list (pre-selected), using the same
actions as a library workout.

Button rules:
    - Summary:  CSV only (the summary is computed from the raw CSV log).
    - Save FIT: CSV only (a .fit file needs no conversion).
    - Upload:   CSV or FIT; opens Strava's manual upload page when its API is
                not connected.

UI-toolkit code only; all export/upload work is delegated back to the caller
through the ``start_export`` callback so this window stays decoupled from the
worker plumbing. Cross-platform (Windows / macOS / Linux).
"""

import contextlib
import os
import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from tkinter import filedialog
from typing import Any, Literal

import customtkinter as ctk

from app.workout_metadata import load_workout_metadata
from app.workout_paths import WORKOUT_DIRECTORY_PREFIX, WorkoutPaths
from gui.window_utils import center_over_parent, open_in_file_manager

StartExport = Callable[[str, bool], None]
OpenSummary = Callable[[str], None]

# Row highlight colors (light mode, dark mode).
_ROW_SELECTED = ("#cfe3f5", "#1f4260")
_ROW_NORMAL = "transparent"
# Muted hint text, matching the main dashboard's subtext color (light, dark).
_MUTED = ("#5f5f5f", "#9a9a9a")
# Compact workout-mode badges. These are classifications, not success states,
# so both palettes remain restrained in light and dark appearance modes.
_BADGE_STYLE = {
    "planned": ("Plan", ("#dceafa", "#193c5a"), ("#245b8f", "#a9d2f5")),
    "free": ("Free", ("#e8e8e8", "#343434"), ("#555555", "#b6b6b6")),
    "fit": ("FIT", ("#ece8f3", "#393443"), ("#655879", "#c5b9d7")),
    "invalid": ("Invalid", ("#f3dddd", "#4a2929"), ("#8a3030", "#f0aaaa")),
}
_INITIAL_ROW_COUNT = 24
_ROW_BATCH_SIZE = 24
# Keep the library responsive even when a long-running installation has years
# of recordings. Files outside the library remain available through Choose file….
_MAX_LISTED_WORKOUTS = 200

WorkoutType = Literal["planned", "free", "fit", "invalid"]


@dataclass
class HistoryCatalog:
    """Selection and deduplication state independent of window construction."""

    selected_path: Path | None = None
    selected_row: Any | None = None
    rows_by_key: dict[str, Any] = field(default_factory=dict)
    paths_by_key: dict[str, Path] = field(default_factory=dict)
    types_by_key: dict[str, WorkoutType] = field(default_factory=dict)

    @staticmethod
    def key(path: Path) -> str:
        """Return a normalized key for path deduplication."""
        try:
            return os.path.normcase(str(path.resolve()))
        except OSError:
            return os.path.normcase(str(path))

    def register(self, path: Path, row: Any, workout_type: WorkoutType) -> None:
        """Register one rendered path and its view row."""
        key = self.key(path)
        self.rows_by_key[key] = row
        self.paths_by_key[key] = path
        self.types_by_key[key] = workout_type


def _open_in_file_manager(path: str) -> None:
    """Reveal a folder in the OS file manager (Finder / Explorer / xdg)."""
    with contextlib.suppress(Exception):
        open_in_file_manager(path)


def list_workouts(
    workouts_dir: Path, limit: int = _MAX_LISTED_WORKOUTS
) -> list[Path]:
    """Return a bounded newest-first list of canonical workout CSVs."""
    try:
        year_dirs = sorted(
            (
                path
                for path in workouts_dir.iterdir()
                if path.is_dir() and path.name.isdigit() and len(path.name) == 4
            ),
            key=lambda path: path.name,
            reverse=True,
        )
    except OSError:
        return []

    def _mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:  # deleted between listing and stat
            return 0.0

    files: list[Path] = []
    for year_dir in year_dirs:
        try:
            workout_dirs = [
                path for path in year_dir.iterdir()
                if path.is_dir() and path.name.startswith(WORKOUT_DIRECTORY_PREFIX)
            ]
        except OSError:
            continue
        for workout_dir in workout_dirs:
            csv_path = WorkoutPaths(workout_dir).csv
            if csv_path.is_file():
                files.append(csv_path)
    files.sort(key=_mtime, reverse=True)
    return files[:limit]


def _format_size(num_bytes: int) -> str:
    kb = num_bytes / 1024
    return f"{kb:,.0f} KB" if kb < 1024 else f"{kb / 1024:,.1f} MB"


def _file_details(path: Path) -> str:
    """'2026-07-03 09:31  ·  12 KB' (em-dashes when the file is unreadable)."""
    try:
        stat = path.stat()
        when = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
        return f"{when}   ·   {_format_size(stat.st_size)}"
    except OSError:
        return "—   ·   —"


def _display_name(path: Path) -> str:
    """Return the workout directory name, or an external file name."""
    return (
        path.parent.name
        if path.suffix.lower() == ".csv"
        and path.parent.name.startswith(WORKOUT_DIRECTORY_PREFIX)
        else path.name
    )


def _workout_type(path: Path) -> WorkoutType:
    """Classify a v1 history item from its required metadata."""
    if path.suffix.lower() != ".csv":
        return "fit"
    try:
        mode = load_workout_metadata(path)["workout_mode"]
    except ValueError:
        return "invalid"
    return "planned" if mode == "planned" else "free"


def open_history_window(
    root: ctk.CTk,
    workouts_dir: Path,
    start_export: StartExport,
    strava_connected: bool,
    open_summary: OpenSummary | None = None,
) -> None:
    """Open the Workouts window.

    Args:
        root: Parent window.
        workouts_dir: Directory containing recorded workouts.
        start_export: Callback ``(path, upload)`` that converts/uploads a file.
        strava_connected: Whether Upload uses the API or opens Strava's manual
            upload page.
        open_summary: Optional callback ``(path)`` that opens the workout
            summary screen for a CSV (the button is disabled when None).
    """
    win = ctk.CTkToplevel(master=root)
    # Building up to 200 CustomTkinter rows is not instantaneous. Keep the
    # toplevel unmapped until its first screenful has final geometry so users
    # never see badges appear at the left and jump into place one row at a time.
    win.withdraw()
    win.title("Workouts")
    # Preserve a practical list area and full-width action controls at the
    # default size.
    win.minsize(700, 540)
    win.transient(root)
    grab_job: str | None = None

    # ---- selection state ----------------------------------------------------
    catalog = HistoryCatalog()

    # ---- header ---------------------------------------------------------------
    header = ctk.CTkFrame(win, fg_color="transparent")
    header.pack(fill="x", padx=14, pady=(12, 4))
    ctk.CTkLabel(
        header, text="Workouts", font=ctk.CTkFont(size=16, weight="bold")
    ).pack(side="left")
    ctk.CTkButton(
        header, text="Open folder", width=110,
        command=lambda: _open_in_file_manager(str(workouts_dir)),
    ).pack(side="right")
    search_var = tk.StringVar(value="")
    search_entry = ctk.CTkEntry(
        header, textvariable=search_var, placeholder_text="Search workouts…", width=210
    )
    search_entry.pack(side="right", padx=8)

    # ---- action bar (packed before the list so it never scrolls away) --------
    actions = ctk.CTkFrame(win, fg_color="transparent")
    actions.pack(side="bottom", fill="x", padx=14, pady=(4, 12))
    action_info = ctk.CTkFrame(actions, fg_color="transparent")
    action_info.pack(fill="x")
    info_group = ctk.CTkFrame(action_info, fg_color="transparent")
    info_group.pack(anchor="center")
    action_buttons = ctk.CTkFrame(actions, fg_color="transparent")
    action_buttons.pack(fill="x", pady=(6, 0))
    button_group = ctk.CTkFrame(action_buttons, fg_color="transparent")
    button_group.pack(anchor="center")

    ctk.CTkLabel(
        info_group,
        text=f"Library: newest {_MAX_LISTED_WORKOUTS} workouts",
        text_color=_MUTED,
    ).pack(side="left", padx=(0, 12))
    hint = ctk.CTkLabel(
        info_group,
        text="Select a workout · ↑↓ select · Enter summary",
        text_color=_MUTED,
        anchor="center",
    )
    hint.pack(side="left")

    upload_btn = ctk.CTkButton(
        button_group, text="Upload", width=100, state="disabled"
    )
    upload_btn.pack(side="left")
    save_fit_btn = ctk.CTkButton(
        button_group, text="Save FIT", width=100, state="disabled"
    )
    save_fit_btn.pack(side="left", padx=8)
    summary_btn = ctk.CTkButton(
        button_group, text="Summary", width=100, state="disabled"
    )
    summary_btn.pack(side="left")

    # ---- list -----------------------------------------------------------------
    container = ctk.CTkScrollableFrame(win)
    container.pack(fill="both", expand=True, padx=14, pady=(4, 4))
    container.grid_columnconfigure(0, weight=1)

    # ---- actions ----------------------------------------------------------------
    def act(upload: bool) -> None:
        """Hand the selected file to the shared export path and close."""
        if catalog.selected_path is None:
            return
        start_export(str(catalog.selected_path), upload)
        win.destroy()

    def show_summary() -> None:
        """Open the summary screen for the selected workout.

        This window holds a modal grab; release it first or the summary
        window would not receive any mouse/keyboard events. The Workouts
        window stays open (non-modal) so more summaries can be opened.
        """
        path = catalog.selected_path
        if path is None or open_summary is None or path.suffix.lower() != ".csv":
            return  # also guards the double-click shortcut on a FIT row
        try:
            if grab_job is not None:
                win.after_cancel(grab_job)  # grab may not have been applied yet
            win.grab_release()
        except Exception:
            pass
        open_summary(str(path))

    summary_btn.configure(command=show_summary)
    save_fit_btn.configure(command=lambda: act(False))
    upload_btn.configure(command=lambda: act(True))

    def refresh_actions() -> None:
        """Enable each button according to the selected file type."""
        path = catalog.selected_path
        if path is None:
            for btn in (summary_btn, save_fit_btn, upload_btn):
                btn.configure(state="disabled")
            hint.configure(text="Select a workout · ↑↓ select · Enter summary")
            return
        is_csv = path.suffix.lower() == ".csv"
        summary_btn.configure(
            state="normal" if (is_csv and open_summary is not None) else "disabled"
        )
        save_fit_btn.configure(state="normal" if is_csv else "disabled")
        upload_btn.configure(state="normal")
        if not strava_connected:
            hint.configure(
                text=f"{_display_name(path)}   ·   opens Strava's manual upload page"
            )
        elif not is_csv:
            hint.configure(text=f"{_display_name(path)}   ·   already a FIT file")
        else:
            hint.configure(text=_display_name(path))

    def select(path: Path, row: ctk.CTkFrame) -> None:
        if catalog.selected_row is not None:
            catalog.selected_row.configure(fg_color=_ROW_NORMAL)
        catalog.selected_path, catalog.selected_row = path, row
        row.configure(fg_color=_ROW_SELECTED)
        refresh_actions()

    def visible_items() -> list[tuple[Path, ctk.CTkFrame]]:
        """Return rendered rows matching the current lightweight filter."""
        query = search_var.get().strip().casefold()
        items: list[tuple[Path, ctk.CTkFrame]] = []
        for key, row in catalog.rows_by_key.items():
            path = catalog.paths_by_key[key]
            workout_type = catalog.types_by_key[key]
            matched = (
                not query
                or query in _display_name(path).casefold()
                or query in workout_type
            )
            if matched:
                row.pack(fill="x", padx=4, pady=2)
                items.append((path, row))
            else:
                row.pack_forget()
        return items

    def apply_filter(*_args: Any) -> None:
        items = visible_items()
        if catalog.selected_row is not None and not catalog.selected_row.winfo_ismapped():
            catalog.selected_path, catalog.selected_row = None, None
        if catalog.selected_row is None and items:
            select(*items[0])
        else:
            refresh_actions()

    def add_row(path: Path, external: bool = False, at_top: bool = False) -> ctk.CTkFrame:
        """Add one selectable file row (no per-row buttons by design)."""
        row = ctk.CTkFrame(container, corner_radius=8, fg_color=_ROW_NORMAL)
        anchor_widget = _first_row() if at_top else None
        if anchor_widget is not None and anchor_widget is not row:
            row.pack(fill="x", padx=4, pady=2, before=anchor_widget)
        else:
            row.pack(fill="x", padx=4, pady=2)

        title = ctk.CTkFrame(row, fg_color="transparent")
        title.pack(fill="x", padx=10, pady=(6, 0))
        title.grid_columnconfigure(0, weight=1)
        name = ctk.CTkLabel(
            title,
            text=_display_name(path),
            font=ctk.CTkFont(size=13, weight="bold"),
            anchor="w",
        )
        name.grid(row=0, column=0, sticky="ew")
        workout_type = _workout_type(path)
        badge_text, badge_bg, badge_text_color = _BADGE_STYLE[workout_type]
        badge = ctk.CTkLabel(
            title,
            text=badge_text,
            width=48,
            height=20,
            corner_radius=7,
            fg_color=badge_bg,
            text_color=badge_text_color,
            font=ctk.CTkFont(size=11, weight="bold"),
        )
        badge.grid(row=0, column=1, sticky="e", padx=(8, 0))
        details = _file_details(path)
        if external:
            details += f"   ·   {path.parent}"  # show where an outside file lives
        sub = ctk.CTkLabel(
            row, text=details, text_color=_MUTED, font=ctk.CTkFont(size=12), anchor="w"
        )
        sub.pack(anchor="w", padx=10, pady=(0, 6))

        # Clicks on the frame or either label all select; double-click is a
        # shortcut for Summary. Children don't propagate events in Tk, so each
        # widget is bound explicitly.
        for widget in (row, title, name, badge, sub):
            widget.bind("<Button-1>", lambda _e, p=path, r=row: select(p, r))
            widget.bind("<Double-Button-1>", lambda _e: show_summary())
        catalog.register(path, row, workout_type)
        return row

    def _first_row() -> ctk.CTkFrame | None:
        children = container.winfo_children()
        return children[0] if children else None

    def choose_file() -> None:
        """Add an external workout CSV or FIT file."""
        chosen = filedialog.askopenfilename(
            parent=win,
            title="Select workout file",
            initialdir=str(workouts_dir),
            filetypes=[
                ("Workout files", "*.csv *.fit"),
                ("CSV files", "*.csv"),
                ("FIT files", "*.fit"),
            ],
        )
        if not chosen:
            return
        path = Path(chosen)
        if path.suffix.lower() not in (".csv", ".fit"):
            return
        # If this file is already listed, just select it instead of duplicating.
        existing = catalog.rows_by_key.get(catalog.key(path))
        row = existing if existing is not None else add_row(
            path, external=True, at_top=True
        )
        select(path, row)

    ctk.CTkButton(
        header, text="Choose file…", width=110, command=choose_file,
    ).pack(side="right", padx=(0, 8))

    def move_selection(delta: int) -> str:
        items = visible_items()
        if not items:
            return "break"
        paths = [path for path, _row in items]
        selected = catalog.selected_path
        try:
            index = paths.index(selected) if selected is not None else 0
        except ValueError:
            index = 0
        index = min(max(index + delta, 0), len(items) - 1)
        select(*items[index])
        return "break"

    search_var.trace_add("write", apply_filter)
    win.bind("<Escape>", lambda _event: win.destroy())
    win.bind("<Up>", lambda _event: move_selection(-1))
    win.bind("<Down>", lambda _event: move_selection(1))
    win.bind("<Return>", lambda _event: show_summary())

    # ---- populate ---------------------------------------------------------------
    workouts = list_workouts(workouts_dir)
    if not workouts:
        ctk.CTkLabel(
            container,
            text="No workouts recorded yet.\n"
            "Use “Choose file…” to work with a workout stored elsewhere.",
            text_color=_MUTED,
            justify="left",
        ).pack(anchor="w", padx=8, pady=12)
    else:
        for path in workouts[:_INITIAL_ROW_COUNT]:
            add_row(path)
        # Pre-select the most recent workout so the buttons are ready to use.
        first = _first_row()
        if first is not None:
            select(workouts[0], first)

    next_workout = min(len(workouts), _INITIAL_ROW_COUNT)

    def populate_older_rows() -> None:
        """Append older rows in bounded batches without blocking first paint."""
        nonlocal next_workout
        end = min(next_workout + _ROW_BATCH_SIZE, len(workouts))
        for path in workouts[next_workout:end]:
            # A user can choose a not-yet-rendered canonical file while batches
            # are pending. Do not add it again when its normal turn arrives.
            if catalog.key(path) not in catalog.rows_by_key:
                add_row(path)
        next_workout = end
        if next_workout < len(workouts) and win.winfo_exists():
            win.after(1, populate_older_rows)

    # A CTkScrollableFrame learns its true content width from a <Configure>
    # event on its internal Canvas, which only occurs after the toplevel is
    # mapped. Map at zero opacity first; revealing a withdrawn window directly
    # would briefly draw every badge against the rows' small requested width.
    center_over_parent(win, root, 700, 540)
    transparent_layout = True
    try:
        win.attributes("-alpha", 0.0)
    except Exception:
        # Alpha is supported on the packaged macOS/Windows targets and common
        # X11 window managers. The fallback still works, but cannot conceal a
        # window-manager-specific first mapped layout pass.
        transparent_layout = False
    win.deiconify()

    def finish_reveal() -> None:
        """Show the window after its mapped Canvas has propagated real width."""
        nonlocal grab_job
        if not win.winfo_exists():
            return
        win.update_idletasks()
        if transparent_layout:
            win.attributes("-alpha", 1.0)
        win.lift()
        if next_workout < len(workouts):
            win.after(1, populate_older_rows)
        # A Toplevel cannot grab before it is viewable; keep the id so the
        # summary action can cancel a not-yet-applied grab.
        grab_job = win.after(200, win.grab_set)

    # One short mapped frame lets Tk deliver the scrollable Canvas <Configure>
    # event before opacity changes. It is imperceptible but removes the jump.
    win.after(20, finish_reveal)
