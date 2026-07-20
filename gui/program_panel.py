"""Live training-program panel and target-adherence indicator.

``ProgramPanelMixin`` is mixed into ``KayakFitGUI`` (see ``gui.main_gui``). It
defines no ``__init__`` and holds no state of its own; every ``self`` attribute
it uses is created on the main window, and it only ever runs as part of that
class via multiple inheritance. Those shared attributes/methods are declared in
``gui._mixin_base.GuiSharedState``, which this mixin inherits under
``TYPE_CHECKING`` only (so mypy resolves them) — there is no runtime base class.
"""

import tkinter as tk
from typing import TYPE_CHECKING, Any

import customtkinter as ctk

from app import program as program_mod

from .dashboard_constants import (
    STEP_COLOR_DEFAULT,
    STEP_KIND_COLORS,
    STEP_KIND_CUES,
    SUBTEXT_COLOR,
    TARGET_COLORS,
    TARGET_LABELS,
)
from .metrics_format import fmt_time, target_status, target_text, target_value

# Attributes/methods this mixin reads from sibling mixins and KayakFitGUI live on
# the composed class, not here; inherit the shared declarations for type checking
# only (no runtime base change). See gui/_mixin_base.py.
if TYPE_CHECKING:
    from ._mixin_base import GuiSharedState as _MixinBase
else:
    _MixinBase = object


class ProgramPanelMixin(_MixinBase):
    """Program panel, step-ending cues, and target-adherence badge."""

    def _build_program_panel(self) -> None:
        """Live training-program panel (gridded on demand at row 3)."""
        self.prog_step_var = tk.StringVar(value="")
        self.prog_remaining_var = tk.StringVar(value="")
        self.prog_next_var = tk.StringVar(value="")
        self.prog_count_var = tk.StringVar(value="")
        self.prog_target_var = tk.StringVar(value="")

        self.program_panel = ctk.CTkFrame(self.root, corner_radius=10)
        self.program_panel.grid_columnconfigure(0, weight=1)

        top = ctk.CTkFrame(self.program_panel, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=14, pady=(10, 0))
        top.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            top,
            textvariable=self.prog_step_var,
            anchor="w",
            font=ctk.CTkFont(size=16, weight="bold"),
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            top,
            textvariable=self.prog_remaining_var,
            anchor="e",
            font=ctk.CTkFont(size=22, weight="bold"),
        ).grid(row=0, column=1, sticky="e")

        # Progress within the current step (primary — pairs with the countdown).
        self.program_step_progress = ctk.CTkProgressBar(self.program_panel, height=14)
        self.program_step_progress.set(0)
        self.program_step_progress.grid(row=1, column=0, sticky="ew", padx=14, pady=(6, 0))

        # Overall progress across the whole workout (thin, secondary).
        self.program_progress = ctk.CTkProgressBar(
            self.program_panel, height=5, progress_color=SUBTEXT_COLOR
        )
        self.program_progress.set(0)
        self.program_progress.grid(row=2, column=0, sticky="ew", padx=14, pady=(3, 0))

        # Target-adherence badge (centered; shown only for steps that have a target).
        self.prog_target_label = ctk.CTkLabel(
            self.program_panel, textvariable=self.prog_target_var,
            fg_color="transparent", corner_radius=8, padx=12, pady=2,
            font=ctk.CTkFont(size=13, weight="bold"),
        )
        self.prog_target_label.grid(row=3, column=0, pady=(6, 0))

        bottom = ctk.CTkFrame(self.program_panel, fg_color="transparent")
        bottom.grid(row=4, column=0, sticky="ew", padx=14, pady=(4, 10))
        bottom.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            bottom,
            textvariable=self.prog_next_var,
            anchor="w",
            text_color=SUBTEXT_COLOR,
            font=ctk.CTkFont(size=12),
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            bottom,
            textvariable=self.prog_count_var,
            anchor="e",
            text_color=SUBTEXT_COLOR,
            font=ctk.CTkFont(size=12),
        ).grid(row=0, column=1, sticky="e", padx=(0, 10))
        # Advance the current step — required for manual ("open") steps, and
        # skips ahead on timed/distance steps.
        self.program_advance_btn = ctk.CTkButton(
            bottom, text="Next step  →", width=120, height=26,
            command=self._advance_program,
        )
        self.program_advance_btn.grid(row=0, column=2, sticky="e")

    def _on_program_selected(self, name: str) -> None:
        """Show a compact, calculation-free description of the selected plan."""
        program = self._programs.get(self._selected_program_id(name))
        if program is None:
            self.program_preview_var.set("Free workout · no planned steps")
            return
        time_s = sum(
            float(step.duration_value or 0)
            for step in program.steps
            if step.duration_kind == "time"
        )
        distance_m = sum(
            float(step.duration_value or 0)
            for step in program.steps
            if step.duration_kind == "distance"
        )
        open_count = sum(1 for step in program.steps if step.is_open())
        parts = [f"{len(program)} steps"]
        if time_s:
            parts.append(fmt_time(time_s))
        if distance_m:
            parts.append(f"{distance_m:,.0f} m")
        if open_count:
            parts.append(f"{open_count} open")
        self.program_preview_var.set(" · ".join(parts))

    def _refresh_programs(self, event: Any = None) -> None:
        """Reload the training-program list (e.g. on window focus).

        Lets a program JSON added or edited while the app is open show up in
        the dropdown without restarting. Skipped while recording so the menu
        never changes mid-workout.
        """
        if event is not None and event.widget is not self.root:
            return  # FocusIn also fires for child widgets; only act for the window
        if self.recording.active:
            return
        try:
            warnings: list[str] = []
            programs = {p.identifier: p for p in program_mod.list_programs(warnings)}
        except Exception:  # never let a bad program file break the UI
            return
        # Announce load issues only when they change, so a persistently bad
        # file doesn't spam the log on every window focus.
        sig = tuple(warnings)
        if sig != self._program_warn_sig:
            self._program_warn_sig = sig
            self._flush_program_warnings(warnings)
        old_menu = [
            (identifier, program.name)
            for identifier, program in self._programs.items()
        ]
        new_menu = [
            (identifier, program.name)
            for identifier, program in programs.items()
        ]
        if new_menu == old_menu:
            self._programs = programs  # refresh contents, menu unchanged
            self._on_program_selected(self.program_var.get())
            return
        selected_id = self._selected_program_id()
        self._programs = programs
        self._program_labels = self._build_program_labels()
        values = [self._free_label, *list(self._program_labels)]
        self.program_menu.configure(values=values)
        selected_label = next(
            (
                label
                for label, identifier in self._program_labels.items()
                if identifier == selected_id
            ),
            self._free_label,
        )
        if selected_label not in values:
            self.program_var.set(self._free_label)
        else:
            self.program_var.set(selected_label)
        self._on_program_selected(self.program_var.get())

    def _advance_program(self) -> None:
        """Advance the running program to the next step (open steps / skip)."""
        if self.worker_mgr is not None and self.recording.active:
            self.worker_mgr.advance_program()

    def _apply_program(self, ev: dict[str, Any]) -> None:
        """Update the live program panel from a program status event."""
        if not self.program_panel.winfo_ismapped() and self.recording.active:
            self.program_panel.grid(row=3, column=0, sticky="ew", padx=14, pady=(2, 0))

        # Once complete, hold the finished state (later ticks carry no step).
        if ev.get("done"):
            self.prog_step_var.set("Program complete")
            self.prog_remaining_var.set("Done")
            self.prog_next_var.set("")
            self.prog_count_var.set("")
            self.program_progress.set(1.0)
            self.program_step_progress.set(1.0)
            self.program_step_progress.configure(progress_color=STEP_COLOR_DEFAULT)
            self._clear_target_indicator()
            if ev.get("transition") == "program_complete":
                self.log_message("✓ Program complete.\n")
                self._sound.cue("complete")
            return

        step_kind = (ev.get("step_kind") or "").lower()
        is_start = ev.get("transition") == "step_start"

        label = ev.get("step_label") or "—"
        kind = step_kind.capitalize()
        self.prog_step_var.set(
            f"{kind} · {label}" if kind and kind not in label else label
        )

        rem = ev.get("remaining")
        kind_r = ev.get("remaining_kind")
        self.program_advance_btn.configure(
            text="Complete step  →" if kind_r not in ("time", "distance") else "Skip step  →"
        )
        if kind_r == "time" and rem is not None:
            self.prog_remaining_var.set(fmt_time(rem))
        elif kind_r == "distance" and rem is not None:
            self.prog_remaining_var.set(f"{int(rem)} m")
        else:
            self.prog_remaining_var.set("—")

        # Heads-up cue as the step nears its end, so you know to stop without
        # looking. Fires once per step; open-ended steps have no defined end.
        if is_start:
            self.program.ending_warned = False
        self._maybe_warn_step_ending(kind_r, rem, ev.get("fraction"))

        nxt = ev.get("next_label")
        self.prog_next_var.set(f"Next: {nxt}" if nxt else "Last step")

        idx = int(ev.get("step_index", 0))
        total = int(ev.get("total", 0))
        if total:
            self.prog_count_var.set(f"Step {idx + 1} / {total}")
            # Overall = completed steps + how far through the current one.
            frac = ev.get("fraction")
            step_frac = float(frac) if isinstance(frac, (int, float)) else 0.0
            self.program_progress.set(min((idx + step_frac) / total, 1.0))
            self.program_step_progress.set(step_frac)

        self.program.target = ev.get("target")
        if is_start:
            # New step: reset the adherence badge, announce it, color the
            # step-progress bar by phase, and play its distinct cue.
            self.program.shown_target = None
            self.program.pending_target = None
            self.program.pending_count = 0
            self._show_initial_target()
            self.log_message(f"▶ {self.prog_step_var.get()}\n")
            self.program_step_progress.configure(
                progress_color=STEP_KIND_COLORS.get(step_kind, STEP_COLOR_DEFAULT)
            )
            self._sound.cue(STEP_KIND_CUES.get(step_kind, "phase"))

    # The heads-up aims for a fixed lead time before a step ends, so a distance
    # step feels the same whether you're sprinting or cruising. STEP_WARN_METERS
    # is only the fallback used when there's no usable speed reading.
    STEP_WARN_SECONDS = 3.0
    STEP_WARN_METERS = 10.0

    def _maybe_warn_step_ending(
        self, kind_r: Any, rem: Any, fraction: Any
    ) -> None:
        """Play a one-shot countdown cue a fixed lead time before a step ends."""
        if self.program.ending_warned or not isinstance(rem, (int, float)):
            return
        warn_s = self.STEP_WARN_SECONDS
        if warn_s <= 0:
            return
        if kind_r == "time":
            threshold = warn_s
        elif kind_r == "distance":
            # Convert the lead time to metres using current speed, so the cue
            # always lands ~warn_s before the finish; fall back to a fixed
            # distance when we have no usable speed reading.
            try:
                speed = float(self._latest_data.get("speed_instant__mps"))
            except (TypeError, ValueError):
                speed = 0.0
            threshold = (
                speed * warn_s
                if speed > 0.3
                else self.STEP_WARN_METERS
            )
        else:
            return  # open-ended step: no defined end to warn about
        if rem > threshold:
            return
        # On a step so short the warning window covers most of it, the countdown
        # would land right on top of the step's own start cue — skip it until the
        # step is at least half done so the two cues stay distinct.
        if isinstance(fraction, (int, float)) and fraction < 0.5:
            return
        self.program.ending_warned = True
        self._sound.cue("countdown")

    def _show_initial_target(self) -> None:
        """Show the step's target range (no status yet) or clear the badge."""
        if not self.program.target:
            self._clear_target_indicator()
            return
        rng = target_text(
            self.program.target, self.cfg.get("max_hr", 185), self._zones_cfg()
        )
        self.prog_target_var.set(f"Target {rng}" if rng else "")
        self.prog_target_label.configure(
            fg_color="transparent", text_color=SUBTEXT_COLOR
        )

    def _clear_target_indicator(self) -> None:
        self.program.target = None
        self.program.shown_target = None
        self.program.pending_target = None
        self.program.pending_count = 0
        self.prog_target_var.set("")
        self.prog_target_label.configure(fg_color="transparent")

    def _target_grace(self) -> int:
        """Seconds a new adherence status must hold before the badge changes."""
        try:
            return max(int(self.cfg.get("target_grace_seconds", 3)), 1)
        except (TypeError, ValueError):
            return 3

    def _update_target_indicator(self, data: dict[str, Any]) -> None:
        """Compare the live metric to the step target.

        A grace period ensures a brief fluctuation across the boundary doesn't
        flip the badge.
        """
        target = self.program.target
        if not target:
            return
        metric = target.get("metric")
        if not isinstance(metric, str):
            return
        max_hr = self.cfg.get("max_hr", 185)
        zones = self._zones_cfg()
        value = target_value(
            metric,
            data,
            max_hr,
            zones,
            self.cfg.get("pull_length_m"),
        )
        raw = target_status(metric, value, target.get("low"), target.get("high"))
        if raw is None:
            # No reading for this metric (e.g. an hr_zone target with no HRM).
            rng = target_text(target, max_hr, zones)
            self.prog_target_var.set(f"Target {rng} · no reading" if rng else "")
            self.prog_target_label.configure(
                fg_color="transparent", text_color=SUBTEXT_COLOR
            )
            return

        if raw == self.program.shown_target:
            # Back to the shown state — cancel any pending change.
            self.program.pending_target = None
            self.program.pending_count = 0
            return

        # A different status: only commit it once it has held long enough.
        if raw == self.program.pending_target:
            self.program.pending_count += 1
        else:
            self.program.pending_target = raw
            self.program.pending_count = 1
        if self.program.pending_count >= self._target_grace():
            self._commit_target_status(raw, target, max_hr, zones)
            self.program.pending_target = None
            self.program.pending_count = 0

    def _commit_target_status(
        self, status: str, target: dict[str, Any], max_hr: Any, zones: Any
    ) -> None:
        """Apply a confirmed adherence status to the badge (and alert on drift)."""
        previous = self.program.shown_target
        self.program.shown_target = status
        rng = target_text(target, max_hr, zones)
        self.prog_target_var.set(f"{TARGET_LABELS[status]} · {rng}")
        bg, fg = TARGET_COLORS[status]
        self.prog_target_label.configure(fg_color=bg, text_color=fg)
        # Sound only when newly drifting out of range (the grace period already
        # debounces this, so no extra cooldown is needed).
        if status in ("hard", "easy") and previous not in ("hard", "easy"):
            self._sound.cue("alert")

    def _hide_program_panel(self) -> None:
        if self.program_panel.winfo_ismapped():
            self.program_panel.grid_remove()
        # Reset so a re-shown panel doesn't flash the previous run's color/fill
        # before the first step sets them.
        self.program_step_progress.set(0)
        self.program_step_progress.configure(progress_color=STEP_COLOR_DEFAULT)

    def _flush_program_warnings(self, warnings: list[str]) -> None:
        """Surface a one-line summary of skipped training plans.

        Keeps the activity log uncluttered — the per-file reasons are logged
        (and land in ~/KayakFit/logs/kayakfit.log) by app.program itself.
        """
        if not warnings:
            return
        n = len(warnings)
        self.log_message(
            f"⚠ {n} invalid training plan{'s' if n != 1 else ''} skipped — "
            "see the log for details.\n"
        )

    _PROGRAM_LABEL_LIMIT = 34

    def _build_program_labels(self) -> dict[str, str]:
        """Map bounded, unique dropdown labels to stable file identifiers."""
        labels: dict[str, str] = {}
        for identifier, program in self._programs.items():
            label = program.name
            if len(label) > self._PROGRAM_LABEL_LIMIT:
                label = label[: self._PROGRAM_LABEL_LIMIT - 1].rstrip() + "…"
            if label in labels:
                suffix = 2
                base = label[: self._PROGRAM_LABEL_LIMIT - 4].rstrip(" …")
                while f"{base}… {suffix}" in labels:
                    suffix += 1
                label = f"{base}… {suffix}"
            labels[label] = identifier
        return labels

    def _selected_program_id(self, label: str | None = None) -> str:
        """Resolve a bounded dropdown label to its stable file identifier."""
        selected = str(self.program_var.get()) if label is None else label
        return str(self._program_labels.get(selected, selected))
