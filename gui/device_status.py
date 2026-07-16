"""Device connection chips, HR-zone display, and status indicators.

``DeviceStatusMixin`` is mixed into ``KayakFitGUI`` (see ``gui.main_gui``). It
defines no ``__init__`` and holds no state of its own; every ``self`` attribute
it uses is created on the main window. Those shared attributes/methods are
declared in ``gui._mixin_base.GuiSharedState``, which this mixin inherits under
``TYPE_CHECKING`` only (so mypy resolves them) — there is no runtime base class.
"""

import contextlib
from typing import TYPE_CHECKING, Any, ClassVar

import customtkinter as ctk

from app import recovery, secret_store
from app.events import UiEvent

from .dashboard_constants import (
    CHIP_COLORS,
    HR_ZONE_BG,
    SUBTEXT_COLOR,
    TILE_DIM_COLOR,
)
from .metrics_format import hr_zone, zone_range

# See gui/_mixin_base.py: type-checking-only shared surface for the composed GUI.
if TYPE_CHECKING:
    from ._mixin_base import GuiSharedState as _MixinBase
else:
    _MixinBase = object


class DeviceStatusMixin(_MixinBase):
    """Ergometer/HRM/Strava chips, HR-zone tinting, segment + upload status."""

    def _build_chips(self) -> None:
        bar = ctk.CTkFrame(self.root, fg_color="transparent")
        bar.grid(row=1, column=0, sticky="ew", padx=14, pady=(2, 0))

        def chip() -> ctk.CTkLabel:
            lbl = ctk.CTkLabel(bar, text="", corner_radius=8, padx=12, pady=4)
            lbl.pack(side="left", padx=(0, 8))
            return lbl

        self.erg_chip = chip()
        self.hrm_chip = chip()
        self.strava_chip = chip()

        self._build_hr_legend(bar)

    # Heart-rate zone legend labels, indexed to match HR_ZONE_BG (zone 0-4).
    HR_ZONE_LEGEND: ClassVar[list[tuple[str, str]]] = [
        ("Z1", "Recovery"),
        ("Z2", "Easy"),
        ("Z3", "Aerobic"),
        ("Z4", "Threshold"),
        ("Z5", "Max"),
    ]

    def _build_hr_legend(self, bar: ctk.CTkFrame) -> None:
        """Add a heart-rate zone legend (with bpm ranges) to the right of the chips.

        The tile keeps its full-background tint; this legend names each tint and
        shows the bpm range for every zone, highlighting the active one.
        """
        legend = ctk.CTkFrame(bar, fg_color="transparent")
        self.hr_zone_legend = legend
        legend.pack(side="right", padx=(8, 0))
        ctk.CTkLabel(
            legend,
            text="HR zones",
            text_color=SUBTEXT_COLOR,
            font=ctk.CTkFont(size=12),
        ).pack(side="left", padx=(0, 6))
        # Normal vs active fonts: the active zone is shown in bold (CTkLabel does
        # not support a border, so weight is used to mark the current zone).
        self._legend_font = ctk.CTkFont(size=11)
        self._legend_font_active = ctk.CTkFont(size=11, weight="bold")
        for zone, (short, _name) in enumerate(self.HR_ZONE_LEGEND):
            lbl = ctk.CTkLabel(
                legend,
                text=short,
                fg_color=HR_ZONE_BG[zone],
                corner_radius=6,
                padx=8,
                pady=2,
                font=self._legend_font,
            )
            lbl.pack(side="left", padx=2)
            self.hr_legend_labels[zone] = lbl
        self._refresh_hr_legend()

    def _zones_cfg(self) -> list[int] | None:
        """Return manual zone bounds when configured in manual mode, else None."""
        if str(self.cfg.get("hr_zone_mode", "auto")).lower() == "manual":
            zones = self.cfg.get("hr_zones") or []
            return list(zones) if zones else None
        return None

    def _refresh_hr_legend(self) -> None:
        """Relabel each legend chip with its bpm range for the current config."""
        max_hr = self.cfg.get("max_hr", 185)
        zones = self._zones_cfg()
        for zone, lbl in self.hr_legend_labels.items():
            short, _name = self.HR_ZONE_LEGEND[zone]
            low, high = zone_range(zone, max_hr, zones)
            rng = f"{low}-{high}" if high is not None else f"{low}+"
            lbl.configure(text=f"{short}  {rng}")

    def _highlight_hr_legend(self, active: int) -> None:
        """Mark the active zone chip in bold (active=-1 clears all)."""
        for zone, lbl in self.hr_legend_labels.items():
            lbl.configure(
                font=self._legend_font_active if zone == active else self._legend_font
            )

    def _set_chip(self, chip: ctk.CTkLabel, text: str, kind: str) -> None:
        fg, txt = CHIP_COLORS.get(kind, CHIP_COLORS["muted"])
        chip.configure(text=text, fg_color=fg, text_color=txt)

    def _reset_device_chips(self) -> None:
        """Return the ergometer and HRM chips to their idle (disconnected) state.

        Called when a workout ends or the dashboard is cleared so the chips stop
        showing the last "streaming" / heart-rate value after the devices have
        disconnected.
        """
        erg = self.cfg.get("ergometer_name") or "not set"
        self._set_chip(self.erg_chip, f"Ergometer · {erg}", "muted")
        self._set_chip(self.hrm_chip, "HRM · idle", "muted")

    def _init_chips(self) -> None:
        self._reset_device_chips()
        connected = secret_store.has_complete_credentials(self.cfg)
        self._set_chip(
            self.strava_chip,
            "Strava · connected" if connected else "Strava · not connected",
            "info" if connected else "muted",
        )

    def _set_hr_zone(self, hr: int) -> None:
        """Tint the HR tile, show the current zone + bpm range, mark the legend."""
        max_hr = self.cfg.get("max_hr", 185)
        zones = self._zones_cfg()
        zone = hr_zone(hr, max_hr, zones)
        if 0 <= zone < len(HR_ZONE_BG):
            self.tile_frames["hr"].configure(fg_color=HR_ZONE_BG[zone])
            _short, name = self.HR_ZONE_LEGEND[zone]
            low, high = zone_range(zone, max_hr, zones)
            rng = f"{low}-{high}" if high is not None else f"{low}+"
            self.hr_zone_var.set(f"Z{zone + 1} {name} · {rng} bpm")
            self._highlight_hr_legend(zone)

    def _clear_hr_zone(self) -> None:
        self.tile_frames["hr"].configure(fg_color=self.tile_defaults["hr"])
        self.hr_zone_var.set("")
        self._highlight_hr_legend(-1)

    def _apply_status(self, ev: UiEvent) -> None:
        event = ev.get("event")
        device = ev.get("device")
        if event == "summary":
            self._show_summary(ev)
            return
        if device == "session" and event == "segment":
            self._apply_segment(ev)
            return
        if device == "session" and event == "program":
            self._apply_program(ev)
            return
        if device == "session" and event == "inactivity_autostop":
            minutes = ev.get("minutes", 30)
            self._show_banner(
                f"Auto-stopped after {minutes} min of inactivity — workout saved.",
                "warn",
            )
            self.log_message(
                f"Workout auto-stopped after {minutes} min without movement.\n"
            )
            return
        if device == "session" and event == "persistence_error":
            saved_rows = ev.get("saved_rows", 0)
            self.recording.persistence_error = (
                f"Storage error — recording stopped after {saved_rows} saved rows. "
                "Check available disk space and file permissions."
            )
            self._show_banner(
                self.recording.persistence_error,
                "warn",
            )
            self.log_message(
                f"Workout storage failure after {saved_rows} saved rows: "
                f"{ev.get('message', 'unknown error')}\n"
            )
            return
        if device == "hrm" and event == "battery":
            self._apply_hrm_battery(ev.get("level"))
            return
        if device == "ergometer":
            mapping = {
                "connecting": ("connecting", "warn"),
                "connected": ("connected", "info"),
                "streaming": ("streaming", "ok"),
                "reconnecting": ("reconnecting…", "warn"),
                "stalled": ("no data", "warn"),
            }
            if event in mapping:
                text, kind = mapping[event]
                self._set_chip(self.erg_chip, f"Ergometer · {text}", kind)
            if event == "reconnecting":
                self._show_banner("Ergometer connection lost — reconnecting…", "warn")
            elif event == "stalled":
                self._show_banner("Ergometer connected but sending no data…", "warn")
            elif event in ("connected", "streaming"):
                self._hide_banner()
        elif device == "hrm":
            if event == "connecting":
                self._set_chip(self.hrm_chip, "HRM · connecting", "warn")
            elif event == "connected":
                self._set_chip(self.hrm_chip, "HRM · connected", "ok")
                self._hide_banner()
            elif event == "reconnecting":
                self._set_chip(self.hrm_chip, "HRM · reconnecting…", "warn")
                self._show_banner("Heart-rate strap lost — reconnecting…", "warn")
            elif event == "absent":
                self._set_chip(self.hrm_chip, "HRM · not used", "muted")

    def _apply_hrm_battery(self, level: Any) -> None:
        """Record the HRM battery level and flag a low strap once."""
        try:
            pct = int(level)
        except (TypeError, ValueError):
            return
        self._hrm_battery = pct
        self.log_message(f"HRM battery: {pct}%\n")
        if pct <= 15:
            self._show_banner(
                f"Heart-rate strap battery low ({pct}%) — consider replacing it.",
                "warn",
            )

    def _apply_segment(self, ev: UiEvent) -> None:
        """Reflect a live autopause / lap transition from the session."""
        self.recording.paused = bool(ev.get("paused"))
        # Auto-pause/resume is intentionally silent (dimmed tiles show it): the
        # athlete already knows when they stop and start paddling. Cues are
        # reserved for program changes the athlete can't otherwise anticipate.
        lap = ev.get("lap")
        if isinstance(lap, int) and lap > 0:
            self.recording.lap = lap
            # The live lap counter is movement-based (autopause segments). For a
            # program workout the summary screen and FIT file report laps
            # step-based instead, so a live "Lap N" chip could diverge from
            # (and appear to contradict) what the athlete sees afterwards — the
            # program panel already shows step-based live progress. Show the
            # chip only for free workouts, whose laps ARE these segments.
            if not self.program.active:
                self.lap_label.configure(text=f"Lap {lap}")
                if self.recording.active and not self.lap_label.winfo_ismapped():
                    self.lap_label.pack(side="left", padx=(0, 12))
        self._set_tiles_dimmed(self.recording.paused)
        self._update_live_label()

    def _set_tiles_dimmed(self, dim: bool) -> None:
        """Mute (or restore) the big tile numbers to signal an auto-pause."""
        if dim == self._tiles_dimmed:
            return
        self._tiles_dimmed = dim
        for key, label in self.tile_value_labels.items():
            target = TILE_DIM_COLOR if dim else self.tile_value_colors[key]
            with contextlib.suppress(Exception):  # pragma: no cover - widget teardown race
                label.configure(text_color=target)

    def _restore_tile_brightness(self) -> None:
        """Undo any pause dimming (used when recording stops or resets)."""
        self._set_tiles_dimmed(False)

    def _apply_upload(self, ev: UiEvent) -> None:
        if ev.get("pending"):
            self._show_banner(
                ev.get("message") or "Upload accepted and still processing on Strava.",
                "info",
            )
        elif ev.get("success"):
            # A FIT now exists (saved and/or uploaded), so the workout is safe;
            # drop any pending crash-recovery marker.
            try:
                recovery.clear_active()
            except OSError as exc:
                self._show_banner(
                    f"FIT saved, but recovery state could not be cleared — {exc}",
                    "warn",
                )
        elif ev.get("message"):
            # Surface a failed upload/conversion instead of hiding it in the log.
            self._show_banner(f"Export/upload failed — {ev['message']}", "warn")
        if ev.get("success") and ev.get("activity_id"):
            self.export.strava_activity_id = ev["activity_id"]
            # Upload done: the "Upload to Strava" button is now redundant, so
            # replace it in place with "View on Strava" (fall back to a plain
            # right-pack when the action row isn't shown, e.g. recovery export).
            self.upload_btn.pack_forget()
            if self.view_summary_btn.winfo_ismapped():
                self.strava_btn.pack(
                    side="right", padx=(0, 12), pady=12, before=self.view_summary_btn
                )
            else:
                self.strava_btn.pack(side="right", padx=(0, 12), pady=12)
            if not self.summary_card.winfo_ismapped():
                self.summary_label.configure(text="Uploaded to Strava.")
                self.summary_card.grid(
                    row=5, column=0, sticky="ew", padx=14, pady=(0, 6)
                )
