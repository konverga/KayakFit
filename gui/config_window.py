"""Settings dialog for KayakFit, built with customtkinter.

Covers device selection (with BLE scanning), weight defaults, ergometer
display layout, logging/processing options, and Strava connection.
"""

import queue
import threading
import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass
from tkinter import messagebox
from typing import Any

import customtkinter as ctk

from app import secret_store
from app.strava_auth import StravaAuth
from gui.config_manager import ConfigError, _extract_addr
from gui.device_scanner import scan_devices, test_connection
from gui.metrics_format import zone_lower_bounds
from gui.window_utils import center_over_parent

LogFn = Callable[[str], None]

# Muted hint text, matching the main dashboard's subtext color (light, dark).
_MUTED = ("#5f5f5f", "#9a9a9a")


@dataclass(frozen=True)
class SettingsFields:
    """Widget-variable boundary consumed by settings validation and saving."""

    erg_name: Any
    erg_mac: Any
    hrm_name: Any
    hrm_mac: Any
    hrm_enabled: Any
    person_weight: Any
    boat_weight: Any
    max_hr: Any
    hr_zone_mode: Any
    hr_zones: list[Any]
    display: list[Any]
    log_interval: Any
    log_level: Any
    auto_upload: Any
    sound_cues: Any
    target_grace: Any
    client_id: Any
    client_secret: Any
# Destructive-action red, matching the main window's Stop button.
_DANGER = "#c0392b"
_DANGER_HOVER = "#a02c20"


def open_config_window(
    root: ctk.CTk,
    cfg: dict[str, Any],
    config_mgr: Any,
    on_save_callback: Callable[[], None] | None,
    log: LogFn,
) -> None:
    """Open the configuration window.

    Args:
        root: Parent window.
        cfg: Current configuration dictionary.
        config_mgr: ConfigManager instance.
        on_save_callback: Called after the config is saved/closed.
        log: Callback to append a line to the main log.
    """
    win = ctk.CTkToplevel(master=root)
    win.title("Settings")
    # Wide enough that every hint label (e.g. the "Logging & processing"
    # descriptions) is fully readable without resizing; centered over the
    # main window and clamped to the screen height.
    center_over_parent(win, root, 780, 860)
    win.minsize(700, 480)
    win.transient(root)
    win.after(200, win.grab_set)  # grab after the window is mapped

    scan_stop_event = threading.Event()
    callback_needed = {"value": False}

    def on_close() -> None:
        scan_stop_event.set()
        if callback_needed["value"] and on_save_callback:
            on_save_callback()
        win.destroy()

    win.protocol("WM_DELETE_WINDOW", on_close)

    container = ctk.CTkScrollableFrame(win)
    container.pack(fill="both", expand=True, padx=14, pady=14)

    # ---- variables ------------------------------------------------------
    def _device_label(name: Any, mac: Any) -> str:
        """Build a 'Name (MAC)' dropdown label; fall back to MAC or a dash."""
        name = str(name or "").strip()
        mac = str(mac or "").strip()
        if name and mac:
            return f"{name} ({mac})"
        return mac or "—"

    erg_name_var = tk.StringVar(value=cfg.get("ergometer_name", ""))
    erg_mac_var = tk.StringVar(
        value=_device_label(cfg.get("ergometer_name"), cfg.get("ergometer_mac"))
    )
    # HRM name is no longer user-editable (HRMs are found by BLE service, not by
    # name), but we keep the discovered name to show "Name (MAC)" in the dropdown.
    hrm_name_var = tk.StringVar(value=cfg.get("hrm_name", ""))
    hrm_mac_var = tk.StringVar(
        value=_device_label(cfg.get("hrm_name"), cfg.get("hrm_mac"))
    )
    hrm_enabled_var = tk.StringVar(value=str(cfg.get("hrm_enabled", "yes")))
    person_weight_var = tk.StringVar(value=str(cfg.get("person_weight_default", 75)))
    boat_weight_var = tk.StringVar(value=str(cfg.get("boat_weight_default", 12)))
    max_hr_var = tk.StringVar(value=str(cfg.get("max_hr", 185)))
    hr_zone_mode_var = tk.StringVar(value=str(cfg.get("hr_zone_mode", "auto")))
    # Five zone lower-bound entries (Z1..Z5). Prefill from a manual config if
    # present, otherwise from the auto bounds derived from max HR.
    _existing_zones = cfg.get("hr_zones") or []
    if not (isinstance(_existing_zones, list) and len(_existing_zones) == 5):
        _existing_zones = zone_lower_bounds(max_hr=cfg.get("max_hr", 185))
    hr_zone_vars = [tk.StringVar(value=str(int(_existing_zones[i]))) for i in range(5)]
    client_id_var = tk.StringVar(value=cfg.get("strava_client_id", ""))
    client_secret_var = tk.StringVar(value=cfg.get("strava_client_secret", ""))

    # Display configuration: normalize to 5 valid, unique entries.
    numbers = [n for n in cfg.get("display_config_numbers", []) if n in config_mgr.DISPLAY_MAP]
    for key in sorted(config_mgr.DISPLAY_MAP):
        if len(numbers) >= 5:
            break
        if key not in numbers:
            numbers.append(key)
    numbers = numbers[:5]
    display_vars = [tk.StringVar(value=config_mgr.DISPLAY_MAP[numbers[i]]) for i in range(5)]

    log_interval_var = tk.StringVar(value=str(cfg.get("log_interval", 5)))
    log_level_var = tk.StringVar(value=cfg.get("log_level", "info"))
    auto_upload_var = tk.StringVar(value=str(cfg.get("auto_upload", "no")))
    sound_cues_var = tk.StringVar(value=str(cfg.get("sound_cues", "yes")))
    target_grace_var = tk.StringVar(value=str(cfg.get("target_grace_seconds", 3)))

    # ---- Devices --------------------------------------------------------
    devices = _section(container, "Devices")

    ctk.CTkLabel(devices, text="Ergometer name").grid(row=0, column=0, sticky="w", padx=8, pady=6)
    ctk.CTkEntry(devices, textvariable=erg_name_var, width=160).grid(
        row=0, column=1, sticky="ew", padx=8, pady=6
    )
    erg_menu = ctk.CTkOptionMenu(devices, values=[erg_mac_var.get()], variable=erg_mac_var)
    erg_menu.grid(row=1, column=0, columnspan=2, sticky="ew", padx=8, pady=(0, 6))

    # Off = the workout never tries to connect an HRM and never asks
    # "continue without HRM?" — for users without a strap.
    hrm_switch = ctk.CTkSwitch(
        devices, text="Use heart-rate monitor",
        variable=hrm_enabled_var, onvalue="yes", offvalue="no",
    )
    hrm_switch.grid(row=2, column=0, columnspan=2, sticky="w", padx=8, pady=(10, 2))

    # HRMs are discovered by their BLE Heart Rate Service, so there is no name
    # field to fill in — just scan and pick the strap from this dropdown, which
    # lists each match as "Name (MAC)".
    ctk.CTkLabel(devices, text="Heart-rate monitor").grid(
        row=3, column=0, sticky="w", padx=8, pady=6
    )
    hrm_menu = ctk.CTkOptionMenu(devices, values=[hrm_mac_var.get()], variable=hrm_mac_var)
    hrm_menu.grid(row=3, column=1, sticky="ew", padx=8, pady=6)

    erg_map: dict[str, tuple[str, str]] = {}
    hrm_map: dict[str, tuple[str, str]] = {}

    def handle_scan_result(result: dict[str, Any]) -> None:
        if result.get("cancelled"):
            log("Scan cancelled.\n")
            return
        if result.get("error"):
            log(f"Scan error: {result['error']}\n")
            log("Make sure Bluetooth is enabled and devices are nearby.\n")
            return

        erg_devices: list[tuple[str, str]] = result.get("erg_devices", [])
        hrm_devices: list[tuple[str, str]] = result.get("hrm_devices", [])

        erg_map.clear()
        hrm_map.clear()

        if erg_devices:
            for name, addr in erg_devices:
                erg_map[f"{name} ({addr})"] = (name, addr)
            erg_menu.configure(values=list(erg_map))
            first = erg_devices[0]
            erg_name_var.set(first[0])
            erg_menu.set(f"{first[0]} ({first[1]})")
            log(f"Found {len(erg_devices)} ergometer device(s).\n")
        else:
            log("No ergometer devices found.\n")

        if hrm_devices:
            for name, addr in hrm_devices:
                hrm_map[f"{name} ({addr})"] = (name, addr)
            hrm_menu.configure(values=list(hrm_map))
            first = hrm_devices[0]
            hrm_name_var.set(first[0])
            hrm_menu.set(f"{first[0]} ({first[1]})")
            log(f"Found {len(hrm_devices)} HRM device(s).\n")
        else:
            log("No HRM devices found.\n")

        log(f"Scan complete. {result.get('total', 0)} total devices seen.\n")

    def on_erg_selected(choice: str) -> None:
        # The menu already holds the "Name (MAC)" label; keep it for display and
        # just sync the name field. _extract_addr pulls the MAC out on save.
        if choice in erg_map:
            name, _ = erg_map[choice]
            erg_name_var.set(name)

    def on_hrm_selected(choice: str) -> None:
        # Keep the "Name (MAC)" label shown; record the name for persistence.
        if choice in hrm_map:
            name, _ = hrm_map[choice]
            hrm_name_var.set(name)

    erg_menu.configure(command=on_erg_selected)
    hrm_menu.configure(command=on_hrm_selected)

    def start_scan() -> None:
        scan_stop_event.clear()
        # A blank ergometer name would match nothing; fall back to the device
        # family name ("Bull"), mirroring the first-run wizard.
        scan_devices(
            erg_pattern=erg_name_var.get().strip() or "bull",
            on_log=log,
            on_result=handle_scan_result,
            schedule=lambda fn: win.after(0, fn),
            stop_event=scan_stop_event,
        )

    ctk.CTkButton(devices, text="Scan for devices", command=start_scan).grid(
        row=5, column=0, columnspan=2, sticky="ew", padx=8, pady=(6, 4)
    )

    def make_test_handler(
        btn: ctk.CTkButton, btn_text: str, label: str
    ) -> Callable[[dict[str, Any]], None]:
        """Build a result handler that re-enables ``btn`` and reports the outcome."""

        def handler(res: dict[str, Any]) -> None:
            btn.configure(state="normal", text=btn_text)
            if res.get("ok"):
                log(f"✓ {label} connection OK.\n")
                messagebox.showinfo("Connection test", f"{label} connected successfully.")
            else:
                log(f"✗ {label} connection failed: {res.get('error', 'could not connect')}\n")
                messagebox.showerror(
                    "Connection test",
                    f"Could not connect to the {label.lower()}:\n"
                    f"{res.get('error', 'unknown error')}\n\n"
                    f"Make sure the {label.lower()} is on, nearby, and not paired elsewhere.",
                )

        return handler

    def run_test(
        addr_var: tk.StringVar, btn: ctk.CTkButton, btn_text: str, label: str
    ) -> None:
        addr = _extract_addr(addr_var.get())
        if not addr:
            messagebox.showwarning("Connection test", f"Select an {label.lower()} first.")
            return
        btn.configure(state="disabled", text="Testing…")
        test_connection(
            address=addr,
            on_log=log,
            on_result=make_test_handler(btn, btn_text, label),
            schedule=lambda fn: win.after(0, fn),
        )

    # Both device tests sit side by side on one row as equal halves.
    test_row = ctk.CTkFrame(devices, fg_color="transparent")
    test_row.grid(row=6, column=0, columnspan=2, sticky="ew", padx=8, pady=(0, 4))
    test_row.grid_columnconfigure(0, weight=1, uniform="test")
    test_row.grid_columnconfigure(1, weight=1, uniform="test")

    # Forward-declared so the command lambda (which captures the button to
    # disable it during the test) has a known type despite referencing the
    # button inside its own initializer.
    test_btn: ctk.CTkButton
    test_btn = ctk.CTkButton(
        test_row, text="Test ergometer",
        command=lambda: run_test(erg_mac_var, test_btn, "Test ergometer", "Ergometer"),
    )
    test_btn.grid(row=0, column=0, sticky="ew", padx=(0, 4))

    test_hrm_btn: ctk.CTkButton
    test_hrm_btn = ctk.CTkButton(
        test_row, text="Test HRM",
        command=lambda: run_test(hrm_mac_var, test_hrm_btn, "Test HRM", "HRM"),
    )
    test_hrm_btn.grid(row=0, column=1, sticky="ew", padx=(4, 0))
    devices.grid_columnconfigure(1, weight=1)

    def apply_hrm_enabled() -> None:
        """Gray out the HRM fields while the HRM is disabled."""
        hrm_state = "normal" if hrm_enabled_var.get() == "yes" else "disabled"
        for widget in (hrm_menu, test_hrm_btn):
            widget.configure(state=hrm_state)

    hrm_switch.configure(command=apply_hrm_enabled)
    apply_hrm_enabled()

    # ---- Weights --------------------------------------------------------
    weights = _section(container, "Athlete & equipment")
    ctk.CTkLabel(weights, text="Boat (kg)").grid(row=0, column=0, sticky="w", padx=8, pady=6)
    ctk.CTkEntry(weights, textvariable=boat_weight_var, width=80).grid(
        row=0, column=1, sticky="w", padx=8
    )
    ctk.CTkLabel(weights, text="Person (kg)").grid(row=0, column=2, sticky="w", padx=(20, 8))
    ctk.CTkEntry(weights, textvariable=person_weight_var, width=80).grid(
        row=0, column=3, sticky="w", padx=8
    )

    # ---- Heart-rate zones ----------------------------------------------
    hrz = _section(container, "Heart-rate zones")
    ctk.CTkLabel(hrz, text="Max HR (bpm)").grid(row=0, column=0, sticky="w", padx=8, pady=6)
    ctk.CTkEntry(hrz, textvariable=max_hr_var, width=80).grid(row=0, column=1, sticky="w", padx=8)
    ctk.CTkLabel(
        hrz, text="Used to auto-derive heart-rate zones", text_color=_MUTED
    ).grid(row=0, column=2, columnspan=2, sticky="w", padx=(20, 8))
    ctk.CTkLabel(hrz, text="Mode").grid(row=1, column=0, sticky="w", padx=8, pady=6)
    zone_entries: list[ctk.CTkEntry] = []

    def _sync_zone_entry_state(*_a: Any) -> None:
        manual = hr_zone_mode_var.get() == "manual"
        for entry in zone_entries:
            entry.configure(state="normal" if manual else "disabled")

    ctk.CTkOptionMenu(
        hrz, values=["auto", "manual"], variable=hr_zone_mode_var, width=110,
        command=lambda *_: _sync_zone_entry_state(),
    ).grid(row=1, column=1, sticky="w", padx=8)
    ctk.CTkLabel(
        hrz, text="Auto = from Max HR · Manual = bpm lower bounds below",
        text_color=_MUTED,
    ).grid(row=1, column=2, columnspan=2, sticky="w", padx=(20, 8))

    zone_names = ["Z1 Recovery", "Z2 Easy", "Z3 Aerobic", "Z4 Threshold", "Z5 Max"]
    for i, zname in enumerate(zone_names):
        # One zone per row so all five entries are always visible regardless of
        # window width.
        ctk.CTkLabel(hrz, text=f"{zname}  ≥").grid(
            row=2 + i, column=0, sticky="w", padx=8, pady=4
        )
        entry = ctk.CTkEntry(hrz, textvariable=hr_zone_vars[i], width=80)
        entry.grid(row=2 + i, column=1, sticky="w", padx=8, pady=4)
        ctk.CTkLabel(hrz, text="bpm", text_color=_MUTED).grid(
            row=2 + i, column=2, sticky="w", padx=(0, 8)
        )
        zone_entries.append(entry)
    _sync_zone_entry_state()

    # ---- Display configuration -----------------------------------------
    display = _section(container, "Display layout")
    display_options = [name for _, name in sorted(config_mgr.DISPLAY_MAP.items())]
    display_menus: list[ctk.CTkOptionMenu] = []

    def refresh_display_options(*_args: Any) -> None:
        """Hide already-chosen metrics from the other slot dropdowns."""
        chosen = [v.get() for v in display_vars]
        for idx, menu in enumerate(display_menus):
            allowed = [
                opt for opt in display_options
                if opt == display_vars[idx].get() or opt not in chosen
            ]
            menu.configure(values=allowed)

    for i in range(5):
        ctk.CTkLabel(display, text=f"Slot {i + 1}").grid(
            row=i, column=0, sticky="w", padx=8, pady=4
        )
        menu = ctk.CTkOptionMenu(
            display, values=display_options, variable=display_vars[i], width=260,
            command=lambda *_: refresh_display_options(),
        )
        menu.grid(row=i, column=1, sticky="ew", padx=8, pady=4)
        display_menus.append(menu)
    refresh_display_options()
    display.grid_columnconfigure(1, weight=1)

    # ---- Settings -------------------------------------------------------
    settings = _section(container, "Workout & processing")
    ctk.CTkLabel(settings, text="Log interval (s)").grid(
        row=0, column=0, sticky="w", padx=8, pady=6
    )
    ctk.CTkOptionMenu(settings, values=["1", "5", "10"], variable=log_interval_var, width=90).grid(
        row=0, column=1, sticky="w", padx=8
    )
    ctk.CTkLabel(settings, text="Log level").grid(row=0, column=2, sticky="w", padx=(20, 8))
    ctk.CTkOptionMenu(
        settings, values=["debug", "info", "warning", "error"], variable=log_level_var, width=110
    ).grid(row=0, column=3, sticky="w", padx=8)

    ctk.CTkLabel(settings, text="Auto-upload to Strava").grid(
        row=1, column=0, sticky="w", padx=8, pady=6
    )
    ctk.CTkSwitch(
        settings, text="", variable=auto_upload_var, onvalue="yes", offvalue="no",
    ).grid(row=1, column=1, sticky="w", padx=8)
    ctk.CTkLabel(
        settings, text="Upload automatically when a workout ends", text_color=_MUTED
    ).grid(row=1, column=2, columnspan=2, sticky="w", padx=(20, 8))

    ctk.CTkLabel(settings, text="Sound cues").grid(row=2, column=0, sticky="w", padx=8, pady=6)
    ctk.CTkSwitch(
        settings, text="", variable=sound_cues_var, onvalue="yes", offvalue="no",
    ).grid(row=2, column=1, sticky="w", padx=8)
    ctk.CTkLabel(
        settings, text="Play a sound on program phase changes", text_color=_MUTED
    ).grid(row=2, column=2, columnspan=2, sticky="w", padx=(20, 8))

    ctk.CTkLabel(settings, text="Target grace (s)").grid(
        row=3, column=0, sticky="w", padx=8, pady=6
    )
    ctk.CTkOptionMenu(
        settings, values=["1", "2", "3", "5", "8"], variable=target_grace_var, width=90
    ).grid(row=3, column=1, sticky="w", padx=8)
    ctk.CTkLabel(
        settings, text="Seconds out of range before the target badge changes",
        text_color=_MUTED,
    ).grid(row=3, column=2, columnspan=2, sticky="w", padx=(20, 8))

    # ---- Strava ---------------------------------------------------------
    strava = _section(container, "Strava")

    def rebuild_strava() -> None:
        for child in strava.winfo_children():
            child.destroy()
        if secret_store.has_complete_credentials(cfg):
            _build_strava_connected(strava, cfg, config_mgr, rebuild_strava, callback_needed)
        else:
            _build_strava_connect(
                strava, client_id_var, client_secret_var, win, cfg,
                config_mgr, rebuild_strava, callback_needed,
            )

    rebuild_strava()

    fields = SettingsFields(
        erg_name=erg_name_var, erg_mac=erg_mac_var,
        hrm_name=hrm_name_var, hrm_mac=hrm_mac_var,
        hrm_enabled=hrm_enabled_var,
        person_weight=person_weight_var, boat_weight=boat_weight_var,
        max_hr=max_hr_var, hr_zone_mode=hr_zone_mode_var, hr_zones=hr_zone_vars,
        display=display_vars, log_interval=log_interval_var,
        log_level=log_level_var,
        auto_upload=auto_upload_var, sound_cues=sound_cues_var,
        target_grace=target_grace_var, client_id=client_id_var,
        client_secret=client_secret_var,
    )

    # ---- Save -----------------------------------------------------------
    ctk.CTkButton(
        container,
        text="Save settings",
        height=40,
        command=lambda: _submit(
            cfg, config_mgr, win, fields, on_save_callback,
        ),
    ).pack(fill="x", padx=4, pady=(8, 4))


def _section(parent: Any, title: str) -> ctk.CTkFrame:
    """Create a titled section frame and return its body frame."""
    ctk.CTkLabel(
        parent, text=title, font=ctk.CTkFont(size=14, weight="bold")
    ).pack(anchor="w", padx=4, pady=(10, 2))
    frame = ctk.CTkFrame(parent)
    frame.pack(fill="x", padx=4, pady=(0, 6))
    return frame


def _build_strava_connect(
    strava: Any, client_id_var: Any, client_secret_var: Any, win: ctk.CTkToplevel,
    cfg: dict[str, Any], config_mgr: Any, rebuild: Callable[[], None],
    callback_needed: dict[str, bool],
) -> None:
    ctk.CTkLabel(
        strava,
        text=(
            "Create an API app at developers.strava.com and set the Authorization "
            "Callback Domain to 127.0.0.1 (the app uses ports 8000-8009)."
        ),
        text_color=_MUTED,
        wraplength=640,
        justify="left",
    ).pack(anchor="w", padx=8, pady=(8, 6))

    ctk.CTkLabel(strava, text="Client ID").pack(anchor="w", padx=8)
    ctk.CTkEntry(strava, textvariable=client_id_var).pack(fill="x", padx=8, pady=(0, 6))
    ctk.CTkLabel(strava, text="Client secret").pack(anchor="w", padx=8)
    ctk.CTkEntry(strava, textvariable=client_secret_var, show="*").pack(
        fill="x", padx=8, pady=(0, 8)
    )

    status = ctk.CTkLabel(strava, text="Not connected", text_color=_MUTED)
    status.pack(anchor="w", padx=8, pady=(0, 6))

    connect_btn = ctk.CTkButton(strava, text="Connect to Strava")
    connect_btn.configure(
        command=lambda: _connect_strava(
            client_id_var, client_secret_var, status, connect_btn, win,
            cfg, config_mgr, rebuild, callback_needed,
        )
    )
    connect_btn.pack(fill="x", padx=8, pady=(0, 8))


def _build_strava_connected(
    strava: Any, cfg: dict[str, Any], config_mgr: Any,
    rebuild: Callable[[], None], callback_needed: dict[str, bool],
) -> None:
    ctk.CTkLabel(
        strava, text="Connected", text_color="#4caf50",
        font=ctk.CTkFont(size=14, weight="bold"),
    ).pack(anchor="w", padx=8, pady=(8, 4))
    ctk.CTkLabel(
        strava, text=f"Client ID: {cfg.get('strava_client_id', 'N/A')}",
        text_color=_MUTED,
    ).pack(anchor="w", padx=8, pady=(0, 8))
    ctk.CTkButton(
        strava, text="Disconnect", fg_color=_DANGER, hover_color=_DANGER_HOVER,
        command=lambda: _disconnect_strava(cfg, config_mgr, rebuild, callback_needed),
    ).pack(fill="x", padx=8, pady=(0, 8))


def _connect_strava(
    client_id_var: Any, client_secret_var: Any, status: Any, connect_btn: Any,
    win: ctk.CTkToplevel, cfg: dict[str, Any], config_mgr: Any,
    rebuild: Callable[[], None], callback_needed: dict[str, bool],
) -> None:
    client_id = client_id_var.get().strip()
    client_secret = client_secret_var.get().strip()
    if not client_id or not client_secret:
        messagebox.showerror("Error", "Enter both Client ID and Client Secret.")
        return

    status.configure(text="Opening browser… finish the authorization there.", text_color="#e0a23d")
    connect_btn.configure(state="disabled")

    def manual_code_provider(auth_url: str) -> str | None:
        """Prompt (on the GUI thread) for a pasted redirect URL / code, blocking the auth thread."""
        result_q: queue.Queue[str | None] = queue.Queue()

        def ask() -> None:
            dialog = ctk.CTkInputDialog(
                title="Finish connecting to Strava",
                text=(
                    "Your browser didn't return automatically.\n\n"
                    "In the browser address bar after you authorized, copy the full URL "
                    "(or just the code= value) and paste it here:"
                ),
            )
            result_q.put(dialog.get_input())

        win.after(0, ask)
        return result_q.get()

    def auth_thread() -> None:
        tokens = None
        error_msg = None
        try:
            auth = StravaAuth(client_id, client_secret, log_level=cfg.get("log_level", "info"))
            tokens = auth.start_auth_flow(manual_code_provider=manual_code_provider)
        except Exception as e:
            error_msg = str(e)

        def update_ui() -> None:
            if tokens:
                cfg.update({
                    "strava_access_token": tokens["access_token"],
                    "strava_refresh_token": tokens["refresh_token"],
                    "strava_expires_at": tokens["expires_at"],
                    "strava_client_id": client_id,
                    "strava_client_secret": client_secret,
                })
                config_mgr.save_config(cfg)
                callback_needed["value"] = True
                rebuild()
                messagebox.showinfo("Success", "Connected to Strava.")
            else:
                status.configure(text="Connection failed", text_color="#e05a4d")
                connect_btn.configure(state="normal")
                messagebox.showerror(
                    "Error",
                    f"Authorization failed:\n{error_msg or 'Unknown error'}\n\n"
                    "Verify your Client ID and Secret at developers.strava.com.",
                )

        win.after(0, update_ui)

    threading.Thread(target=auth_thread, daemon=True).start()


def _disconnect_strava(
    cfg: dict[str, Any], config_mgr: Any,
    rebuild: Callable[[], None], callback_needed: dict[str, bool],
) -> None:
    if not messagebox.askyesno("Confirm", "Disconnect from Strava and remove saved credentials?"):
        return
    cfg.update({
        "strava_access_token": "", "strava_refresh_token": "", "strava_expires_at": 0,
        "strava_client_id": "", "strava_client_secret": "",
    })
    try:
        config_mgr.save_config(cfg)
    except (ConfigError, OSError) as exc:
        messagebox.showerror("Could not save settings", str(exc))
        return
    callback_needed["value"] = True
    rebuild()
    messagebox.showinfo("Success", "Disconnected from Strava.")


def _submit(
    cfg: dict[str, Any], config_mgr: Any, win: ctk.CTkToplevel,
    fields: SettingsFields, on_save_callback: Callable[[], None] | None,
) -> None:
    words_to_num = {v: k for k, v in config_mgr.DISPLAY_MAP.items()}
    try:
        numbers = [words_to_num[v.get()] for v in fields.display]
    except KeyError:
        messagebox.showerror("Error", "Invalid display layout selection.")
        return
    if len(numbers) != len(set(numbers)):
        messagebox.showerror("Error", "Each display slot must be unique.")
        return

    try:
        boat = int(fields.boat_weight.get())
        person = int(fields.person_weight.get())
    except ValueError:
        messagebox.showerror("Error", "Weights must be whole numbers.")
        return

    try:
        max_hr = int(fields.max_hr.get())
    except ValueError:
        messagebox.showerror("Error", "Max HR must be a whole number.")
        return
    if not (100 <= max_hr <= 240):
        messagebox.showerror("Error", "Max HR must be between 100 and 240 bpm.")
        return

    try:
        log_interval = int(fields.log_interval.get())
        target_grace = int(fields.target_grace.get())
    except ValueError:
        messagebox.showerror(
            "Error", "Log interval and target grace must be whole numbers."
        )
        return

    hr_zone_mode = fields.hr_zone_mode.get()
    try:
        zone_bounds = [int(v.get()) for v in fields.hr_zones]
    except ValueError:
        messagebox.showerror("Error", "Zone bounds must be whole numbers.")
        return
    # Only enforce ordering/range when the manual zones are actually in use.
    if hr_zone_mode == "manual":
        if not all(zone_bounds[i] < zone_bounds[i + 1] for i in range(4)):
            messagebox.showerror("Error", "Zone bounds must increase from Z1 to Z5.")
            return
        if not all(0 <= b <= 240 for b in zone_bounds):
            messagebox.showerror("Error", "Zone bounds must be between 0 and 240 bpm.")
            return

    def clean_mac(value: str) -> str:
        return _extract_addr(value)

    cfg.update({
        "ergometer_name": fields.erg_name.get().strip(),
        "ergometer_mac": clean_mac(fields.erg_mac.get()),
        "hrm_name": fields.hrm_name.get().strip(),
        "hrm_mac": clean_mac(fields.hrm_mac.get()),
        "hrm_enabled": fields.hrm_enabled.get(),
        "person_weight_default": person,
        "boat_weight_default": boat,
        "max_hr": max_hr,
        "hr_zone_mode": hr_zone_mode,
        "hr_zones": zone_bounds,
        "display_config_numbers": numbers,
        "log_interval": log_interval,
        "log_level": fields.log_level.get(),
        "auto_upload": fields.auto_upload.get(),
        "sound_cues": fields.sound_cues.get(),
        "target_grace_seconds": target_grace,
    })

    if not cfg.get("strava_refresh_token"):
        cfg["strava_client_id"] = fields.client_id.get().strip()
        cfg["strava_client_secret"] = fields.client_secret.get().strip()

    config_mgr.save_config(cfg)
    win.destroy()
    if on_save_callback:
        on_save_callback()
