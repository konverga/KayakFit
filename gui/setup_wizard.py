"""First-run setup wizard.

A lightweight, one-window onboarding flow shown the first time KayakFit runs:
scan for Bluetooth devices, pick the ergometer (and optionally a heart-rate
monitor), and point the user to Strava setup. It reuses the same background BLE
scanner as the Settings window and stays decoupled from the worker plumbing.
"""

import contextlib
import threading
from collections.abc import Callable
from tkinter import messagebox
from typing import Any

import customtkinter as ctk

from gui.config_manager import ConfigError
from gui.device_scanner import scan_devices
from gui.window_utils import center_over_parent

LogFn = Callable[[str], None]

_ERG_PLACEHOLDER = "— scan to find devices —"
_HRM_NONE = "None (no heart-rate monitor)"

# Muted hint text, matching the main dashboard's subtext color (light, dark).
_MUTED = ("#5f5f5f", "#9a9a9a")

# Secondary (outlined) button style: the theme's default button text is
# near-white, which would be invisible on a transparent button in light mode.
_SECONDARY_BTN = {
    "fg_color": "transparent",
    "border_width": 1,
    "border_color": ("#b0b0b0", "#565656"),
    "text_color": ("#1a1a1a", "#dddddd"),
    "hover_color": ("#e8e8e8", "#333333"),
}


def open_setup_wizard(
    root: ctk.CTk,
    cfg: dict[str, Any],
    config_mgr: Any,
    log: LogFn,
    on_complete: Callable[[], None] | None = None,
    open_settings: Callable[[], None] | None = None,
) -> None:
    """Open the first-run setup wizard.

    Args:
        root: Parent window.
        cfg: Current configuration dictionary.
        config_mgr: ConfigManager instance (used to persist the result).
        log: Callback to append a line to the main log.
        on_complete: Called after the wizard finishes or is skipped.
        open_settings: Optional callback to open the Settings window (Strava).
    """
    win = ctk.CTkToplevel(master=root)
    win.title("Welcome to KayakFit")
    center_over_parent(win, root, 520, 460)
    win.minsize(480, 420)
    win.transient(root)
    win.after(200, win.grab_set)

    scan_stop_event = threading.Event()
    erg_map: dict[str, tuple[str, str]] = {}
    hrm_map: dict[str, tuple[str, str]] = {}

    erg_var = ctk.StringVar(value=_ERG_PLACEHOLDER)
    erg_name_var = ctk.StringVar(value=str(cfg.get("ergometer_name", "")))
    hrm_var = ctk.StringVar(value=_HRM_NONE)
    strava_var = ctk.BooleanVar(value=False)

    container = ctk.CTkFrame(win, fg_color="transparent")
    container.pack(fill="both", expand=True, padx=20, pady=20)

    def _finish(skipped: bool) -> None:
        scan_stop_event.set()
        try:
            if not skipped:
                _save_devices(
                    cfg,
                    erg_map,
                    erg_var.get(),
                    hrm_map,
                    hrm_var.get(),
                )
            cfg["wizard_seen"] = True
            config_mgr.save_config(cfg)
        except (ConfigError, OSError, ValueError) as exc:
            messagebox.showerror("Could not save devices", str(exc), parent=win)
            return
        want_strava = strava_var.get()
        with contextlib.suppress(Exception):
            win.destroy()
        if on_complete:
            on_complete()
        if not skipped and want_strava and open_settings:
            open_settings()

    win.protocol("WM_DELETE_WINDOW", lambda: _finish(skipped=True))

    # ---- Step 2 (devices) -------------------------------------------------
    def show_devices_step() -> None:
        for child in container.winfo_children():
            child.destroy()

        ctk.CTkLabel(
            container, text="Choose your devices",
            font=ctk.CTkFont(size=20, weight="bold"),
        ).pack(anchor="w", pady=(0, 4))
        ctk.CTkLabel(
            container,
            text=(
                "Pick your ergometer below. A heart-rate monitor is optional."
                if erg_map
                else f'No ergometer named "{erg_name_var.get()}" was found. '
                     "Choose Rescan to check or correct the device name."
            ),
            text_color=_MUTED, wraplength=460, justify="left",
        ).pack(anchor="w", pady=(0, 12))

        ctk.CTkLabel(container, text="Ergometer").pack(anchor="w")
        erg_menu = ctk.CTkOptionMenu(
            container, values=list(erg_map) or [_ERG_PLACEHOLDER], variable=erg_var,
        )
        erg_menu.pack(fill="x", pady=(0, 10))

        ctk.CTkLabel(container, text="Heart-rate monitor (optional)").pack(anchor="w")
        hrm_menu = ctk.CTkOptionMenu(
            container, values=[_HRM_NONE, *list(hrm_map)], variable=hrm_var,
        )
        hrm_menu.pack(fill="x", pady=(0, 10))

        ctk.CTkCheckBox(
            container, text="Set up Strava after finishing", variable=strava_var,
        ).pack(anchor="w", pady=(4, 0))

        row = ctk.CTkFrame(container, fg_color="transparent")
        row.pack(side="bottom", fill="x", pady=(16, 0))

        def _rescan() -> None:
            show_welcome_step()

        ctk.CTkButton(
            row, text="Rescan", width=100, **_SECONDARY_BTN,
            command=_rescan,
        ).pack(side="left")
        ctk.CTkButton(row, text="Finish", command=lambda: _finish(skipped=False)).pack(
            side="right"
        )

    def handle_scan_result(result: dict[str, Any]) -> None:
        if result.get("cancelled"):
            return
        if result.get("error"):
            log(f"Scan error: {result['error']}\n")
            log("Make sure Bluetooth is on and devices are nearby.\n")
            show_welcome_step(error="Bluetooth scan failed. Check Bluetooth and try again.")
            return

        erg_devices: list[tuple[str, str]] = result.get("erg_devices", [])
        hrm_devices: list[tuple[str, str]] = result.get("hrm_devices", [])
        erg_map.clear()
        hrm_map.clear()
        for name, addr in erg_devices:
            erg_map[f"{name} ({addr})"] = (name, addr)
        for name, addr in hrm_devices:
            hrm_map[f"{name} ({addr})"] = (name, addr)

        if erg_map:
            erg_var.set(next(iter(erg_map)))
        else:
            erg_var.set(_ERG_PLACEHOLDER)
        hrm_var.set(next(iter(hrm_map)) if hrm_map else _HRM_NONE)
        log(f"Scan complete: {len(erg_devices)} ergometer(s), {len(hrm_devices)} HRM(s).\n")
        show_devices_step()

    def start_scan() -> None:
        ergometer_name = erg_name_var.get().strip()
        if not ergometer_name:
            show_welcome_step(error="Enter the device name printed on the ergometer.")
            return
        scan_stop_event.clear()
        show_welcome_step(scanning=True)
        scan_devices(
            erg_pattern=ergometer_name,
            on_log=log,
            on_result=handle_scan_result,
            schedule=lambda fn: win.after(0, fn),
            stop_event=scan_stop_event,
        )

    # ---- Step 1 (welcome) -------------------------------------------------
    def show_welcome_step(scanning: bool = False, error: str | None = None) -> None:
        for child in container.winfo_children():
            child.destroy()
        ctk.CTkLabel(
            container, text="Welcome to KayakFit",
            font=ctk.CTkFont(size=22, weight="bold"),
        ).pack(anchor="w", pady=(0, 6))
        ctk.CTkLabel(
            container,
            text=(
                "Let's connect your KayakFirst ergometer so you can start logging "
                "workouts. Turn the ergometer on and keep it nearby. Enter the "
                "device name printed in large letters on the ergometer."
            ),
            text_color=_MUTED, wraplength=460, justify="left",
        ).pack(anchor="w", pady=(0, 14))

        ctk.CTkLabel(container, text="Ergometer device name").pack(anchor="w")
        name_entry = ctk.CTkEntry(
            container,
            textvariable=erg_name_var,
            placeholder_text="Name printed on the ergometer",
        )
        name_entry.pack(fill="x", pady=(4, 6))
        name_entry.configure(state="disabled" if scanning else "normal")

        if error:
            ctk.CTkLabel(
                container,
                text=error,
                text_color=("#a02c20", "#ff8a80"),
                wraplength=460,
                justify="left",
            ).pack(anchor="w", pady=(2, 4))

        if scanning:
            status = ctk.CTkLabel(
                container, text="Scanning for Bluetooth devices (~10s)…",
                text_color=_MUTED,
            )
            status.pack(anchor="w", pady=(6, 0))
            bar = ctk.CTkProgressBar(container, mode="indeterminate")
            bar.pack(fill="x", pady=(8, 0))
            bar.start()

        row = ctk.CTkFrame(container, fg_color="transparent")
        row.pack(side="bottom", fill="x", pady=(16, 0))
        ctk.CTkButton(
            row, text="Skip for now", width=110, **_SECONDARY_BTN,
            command=lambda: _finish(skipped=True),
        ).pack(side="left")
        if not scanning:
            ctk.CTkButton(row, text="Scan for devices", command=start_scan).pack(
                side="right"
            )
            name_entry.bind("<Return>", lambda _event: start_scan())
            name_entry.focus_set()

    show_welcome_step()


def _save_devices(
    cfg: dict[str, Any],
    erg_map: dict[str, tuple[str, str]],
    erg_label: str,
    hrm_map: dict[str, tuple[str, str]],
    hrm_label: str,
) -> None:
    """Persist the chosen ergometer / HRM into the config."""
    if erg_label not in erg_map:
        raise ValueError("Select an ergometer before finishing setup.")
    erg_name, erg_address = erg_map[erg_label]
    cfg["ergometer_name"] = str(erg_name)
    cfg["ergometer_mac"] = str(erg_address)

    if hrm_label in hrm_map:
        name, addr = hrm_map[hrm_label]
        cfg["hrm_name"] = str(name)
        cfg["hrm_mac"] = str(addr)
    elif hrm_label == _HRM_NONE:
        # Explicitly chose no strap: clear any previously stored address so
        # the session doesn't keep trying to connect to it.
        cfg["hrm_name"] = ""
        cfg["hrm_mac"] = ""
    else:
        raise ValueError("Select a heart-rate monitor or choose None.")
