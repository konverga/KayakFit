"""Post-workout summary window.

Shows one recorded workout in detail: headline stats, speed / heart-rate /
power charts over time, time in each heart-rate zone, and a per-lap splits
table. Laps match the FIT export (same segmentation rules), while Strava may
recalculate or present imported workouts differently.

Charts are drawn on plain ``tkinter.Canvas`` — no plotting library, so the
packaged app stays small. All number crunching lives in :mod:`app.summary`;
this module is UI only.
"""

import bisect
import math
import threading
import tkinter as tk
from collections.abc import Callable, Sequence
from pathlib import Path
from tkinter import messagebox
from typing import Any, Literal

import customtkinter as ctk

from app.read_csv import CsvReader
from app.segmentation import load_program_steps
from app.speed_series import SPEED_SMOOTHING_WINDOW, smooth
from app.summary import WorkoutSummary, compute_summary
from app.workout_metadata import load_workout_config, load_workout_metadata

from .metrics_format import fmt_metres, fmt_time, fmt_time_precise
from .window_utils import center_over_parent

# (light, dark) theme colors, matching the main dashboard's palette.
_BG = ("#f3f3f3", "#1f1f1f")
_GRID = ("#e0e0e0", "#333333")
_TEXT = ("#5f5f5f", "#9a9a9a")
_SPEED = ("#1f6fd0", "#6aa8e8")
_HR = ("#c0392b", "#f0a098")
_POWER = ("#9a6a06", "#f0c068")
_CADENCE = ("#7d4fb0", "#b491dd")
_PULL = ("#1e8a6e", "#6fcbb2")
_STEP_FILLS = {
    "warmup": ("#e8f3ea", "#25352a"),
    "work": ("#e7eff9", "#243140"),
    "rest": ("#eeeeee", "#2a2a2a"),
    "cooldown": ("#eee9f5", "#302a38"),
}
_STEP_DEFAULT_FILL = ("#f0f0f0", "#292929")
# Solid zone colors for the time-in-zone bar (light, dark), Z1..Z5.
_ZONE_COLORS = [
    ("#b8b8b8", "#5a5a5a"),
    ("#7fb2e8", "#3f6ea8"),
    ("#77cf9d", "#2f8f5e"),
    ("#f0c068", "#a87f2f"),
    ("#e8867a", "#a84438"),
]
_ZONE_NAMES = ["Z1 Recovery", "Z2 Easy", "Z3 Aerobic", "Z4 Threshold", "Z5 Max"]
_NO_HR_COLOR = ("#d6d6d6", "#454545")
# Text drawn on top of the zone colors: dark on the light variants, light on
# the dark variants, so the labels stay readable in both modes.
_ZONE_TEXT = ("#1a1a1a", "#f2f2f2")

MPS_TO_KMH = 3.6
_SUMMARY_LOADS: dict[str, Any] = {}
# Let the summary window paint its headline metrics before constructing the
# Canvas charts, which are the most expensive widgets for a long recording.
_DETAIL_RENDER_DELAY_MS = 25
# A per-lap row owns 8-10 widgets. Batching avoids a long UI-thread pause for
# workouts with many autopause laps while still filling an ordinary table at a
# natural pace.
_INITIAL_SPLIT_ROWS = 24
_SPLIT_RENDER_BATCH_ROWS = 24
_SPLIT_RENDER_DELAY_MS = 16


def _theme(pair: tuple[str, str]) -> str:
    """Pick the light/dark variant of a color pair for the current mode."""
    return pair[1] if ctk.get_appearance_mode() == "Dark" else pair[0]


def _smooth(
    values: Sequence[float | None],
    breaks: Sequence[bool] | None = None,
    window: int = SPEED_SMOOTHING_WINDOW,
    preserve_max: bool = False,
) -> list[float | None]:
    """Centered moving average that never crosses missing data or a pause.

    Delegates to the shared :func:`app.speed_series.smooth`; reported
    maxima remain raw record peaks. ``preserve_max`` keeps that peak visible and
    hoverable instead of smoothing it below the value shown in the stat cards.
    """
    smoothed = smooth(values, breaks, window)
    if not preserve_max:
        return smoothed
    present = [
        (index, value)
        for index, value in enumerate(values)
        if value is not None and math.isfinite(value)
    ]
    if not present:
        return smoothed
    peak = max(value for _index, value in present)
    for index, value in present:
        if value == peak:
            smoothed[index] = value
    return smoothed


def _kmh(values: list[float | None]) -> list[float | None]:
    return [v * MPS_TO_KMH if v is not None else None for v in values]


def _downsample_indices(
    xs: Sequence[float | None],
    ys: Sequence[float | None],
    breaks: Sequence[bool],
    width: int,
) -> list[int]:
    """Keep visual extrema and boundaries while bounding Canvas point count."""
    if len(xs) <= max(width * 2, 200):
        return list(range(len(xs)))
    valid_x = [value for value in xs if value is not None]
    if not valid_x:
        return []
    x_min, x_max = min(valid_x), max(valid_x)
    span = max(x_max - x_min, 1.0)
    buckets: dict[int, list[int]] = {}
    forced: set[int] = {0, len(xs) - 1}
    for index, (x_value, y_value) in enumerate(zip(xs, ys, strict=False)):
        if (index < len(breaks) and breaks[index]) or x_value is None or y_value is None:
            forced.update((max(index - 1, 0), index, min(index + 1, len(xs) - 1)))
            continue
        pixel = min(int((x_value - x_min) / span * max(width - 1, 1)), width - 1)
        buckets.setdefault(pixel, []).append(index)
    kept = set(forced)
    for indices in buckets.values():
        kept.update((indices[0], indices[-1]))
        values: list[float] = []
        for index in indices:
            value = ys[index]
            if value is not None:
                values.append(value)
        kept.add(indices[min(range(len(values)), key=values.__getitem__)])
        kept.add(indices[max(range(len(values)), key=values.__getitem__)])
    return sorted(kept)


def _nice_step(span: float, target_ticks: int = 4) -> float:
    """A 'nice' value-axis step (1/2/2.5/5 x 10^k) giving ~target_ticks lines."""
    raw = max(span, 1e-9) / target_ticks
    magnitude = 10.0 ** math.floor(math.log10(raw))
    for mult in (1.0, 2.0, 2.5, 5.0, 10.0):
        if raw <= mult * magnitude:
            return mult * magnitude
    return 10.0 * magnitude


def _axis_bounds(
    values: list[float | None],
    floor: float | None = None,
    ceiling: float | None = None,
    margin_frac: float = 0.08,
) -> tuple[float, float]:
    """A value-axis ``(lo, hi)`` that is robust to sparse outliers.

    A single dropped-sample spike (e.g. a garbage 300 bpm reading) would
    otherwise stretch the whole panel and flatten the real trace. The range is
    taken from the 1st/99th percentiles when there are enough samples, but only
    when the true extreme sits well beyond them, so a genuine sprint peak is
    still shown in full. Optional physical ``floor``/``ceiling`` clamps (e.g.
    speed <= 25 km/h, HR <= 250 bpm) act as a final backstop for metrics with a
    known plausible range.

    Args:
        values: The series values; ``None`` entries are ignored.
        floor: Lowest plausible value; the axis never starts below it.
        ceiling: Highest plausible value; the axis never ends above it.
        margin_frac: Fraction of the range added as headroom top and bottom.

    Returns:
        The ``(lo, hi)`` axis bounds, margin included.
    """
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return 0.0, 1.0
    lo, hi = vals[0], vals[-1]
    n = len(vals)
    if n >= 12:
        p_hi = vals[math.ceil(0.99 * (n - 1))]
        p_lo = vals[int(0.01 * (n - 1))]
        if hi > p_hi * 1.25:  # isolated high spike, not a genuine peak
            hi = p_hi
        if p_lo > 0 and lo < p_lo * 0.8:  # isolated low dropout
            lo = p_lo
    if ceiling is not None:
        hi = min(hi, ceiling)
    if floor is not None:
        lo = max(lo, floor)
    if hi - lo < 1e-9:
        lo, hi = lo - 1.0, hi + 1.0
    margin = (hi - lo) * margin_frac
    lo_m, hi_m = lo - margin, hi + margin
    if floor is not None:  # keep the natural baseline (0 speed, 0 bpm) in view
        lo_m = max(lo_m, floor)
    if ceiling is not None:  # never let headroom push the axis past a hard cap
        hi_m = min(hi_m, ceiling)
    return lo_m, hi_m


# Candidate time-axis steps (seconds): sub-minute up to hours.
_TIME_STEPS = [10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200]


def _time_step(t_max: float, target_ticks: int = 5) -> int:
    for step in _TIME_STEPS:
        if t_max / step <= target_ticks:
            return step
    return _TIME_STEPS[-1]


# Candidate distance-axis steps (metres): fine indoor splits up to ultra range.
_DISTANCE_STEPS = [50, 100, 200, 250, 500, 1000, 2000, 2500, 5000, 10000, 20000, 50000, 100000]


def _distance_step(d_max: float, target_ticks: int = 5) -> int:
    for step in _DISTANCE_STEPS:
        if d_max / step <= target_ticks:
            return step
    return _DISTANCE_STEPS[-1]


def _fmt_distance(meters: float, in_km: bool) -> str:
    """Format a distance for the chart's x-axis label.

    Args:
        meters: A distance in metres.
        in_km: Format in kilometres. Chosen once from the axis maximum so the
            whole axis stays in one unit (never a mix of ``m`` and ``km``).

    Returns:
        A compact, self-labelling value such as ``"750 m"`` or ``"1.5 km"``.
    """
    if in_km:
        text = f"{meters / 1000.0:.2f}".rstrip("0").rstrip(".")
        return f"{text} km"
    return f"{meters:,.0f} m"


class _ChartSeries:
    """One selectable line on the combined chart."""

    def __init__(
        self,
        key: str,
        label: str,
        unit: str,
        ys: list[float | None],
        color: tuple[str, str],
        fmt: Callable[[float], str],
        default_on: bool = True,
        floor: float | None = None,
        ceiling: float | None = None,
        stats: tuple[float | None, float | None] | None = None,
    ) -> None:
        self.key = key
        self.label = label
        self.unit = unit
        self.ys = ys
        self.color = color
        self.fmt = fmt
        self.default_on = default_on
        # Plausible value range: the axis is clamped to these so a garbage
        # reading (e.g. HR > 250 bpm, speed > 25 km/h) can't distort the scale.
        self.floor = floor
        self.ceiling = ceiling
        self.has_data = any(v is not None for v in ys)
        self.stats = stats

    def legend_text(self) -> str:
        if not self.has_data:
            return f"{self.label} {self.unit}"
        if self.stats is None:
            return f"{self.label} {self.unit}"
        avg, maximum = self.stats
        if avg is None or maximum is None:
            return f"{self.label} {self.unit}"
        return (
            f"{self.label} {self.unit}   "
            f"avg {self.fmt(avg)} · max {self.fmt(maximum)}"
        )

    def hover_text(self, value: float) -> str:
        return f"{self.label}  {self.fmt(value)} {self.unit}"


class _MultiChart(ctk.CTkFrame):  # type: ignore[misc]  # customtkinter ships no stubs; base is Any
    """Stacked per-metric charts sharing one x axis (time or distance).

    The series have incompatible units (km/h, W, bpm, spm, N), so each
    selected series gets its own vertically isolated panel with its own real
    value axis, all aligned on the same x axis. Each panel's value axis is
    outlier-robust (see :func:`_axis_bounds`): a single garbage sample can't
    stretch the scale, and speed/HR are additionally capped to a plausible
    ceiling. The legend checkboxes choose which panels are shown and the
    canvas grows/shrinks to fit. A segmented toggle switches the shared x axis
    between elapsed time (default) and cumulative distance; it is only shown
    when the workout actually carries distance data. A hover crosshair spans
    every panel: it snaps to the nearest sample and shows the x position plus
    each visible series' exact value in its own color. None values break the
    lines so pauses show as gaps rather than misleading straight lines — a
    missing x coordinate (distance mode) is treated exactly like a missing y
    value.
    """

    _PAD_L, _PAD_R, _PAD_T, _PAD_B = 58, 18, 8, 26
    _PANEL_H, _PANEL_GAP = 118, 12
    _PHASE_H, _PHASE_GAP = 24, 6

    def __init__(
        self,
        master: Any,
        xs_time: list[float],
        xs_distance: list[float | None],
        series: list[_ChartSeries],
        steps: Sequence[tuple[float, float, str, str | None]] | None = None,
        breaks: Sequence[bool] | None = None,
    ) -> None:
        super().__init__(master, corner_radius=10)
        self._xs_time = xs_time
        self._xs_distance = xs_distance
        self._x_mode = "time"  # "time" | "distance"
        self._dist_km = False  # distance axis labelled in km (set per redraw)
        # Only offer the distance axis when there is at least one real reading;
        # otherwise the chart behaves exactly as the time-only version did.
        self._has_distance = any(d is not None for d in xs_distance)
        self._series = [s for s in series if s.has_data]
        self._steps = list(steps) if steps is not None else []
        self._breaks = list(breaks) if breaks is not None else []
        self._visible: dict[str, Any] = {}
        self._downsample_cache: dict[tuple[str, str, int], list[int]] = {}
        self._resize_job: Any | None = None

        # Legend = the selection UI: one colored checkbox per series. The
        # x-axis toggle (when available) sits on the right of the same row.
        legend = ctk.CTkFrame(self, fg_color="transparent")
        legend.pack(fill="x", padx=10, pady=(8, 0))
        if self._has_distance:
            self._x_mode_var = tk.StringVar(value="Time")
            ctk.CTkSegmentedButton(
                legend, values=["Time", "Distance"],
                variable=self._x_mode_var, command=self._set_x_mode,
                height=24, font=ctk.CTkFont(size=12),
            ).pack(side="right", padx=(6, 2), pady=2)
        for s in self._series:
            var = tk.BooleanVar(value=s.default_on)
            self._visible[s.key] = var
            ctk.CTkCheckBox(
                legend,
                text=s.legend_text(),
                variable=var,
                command=self._redraw,
                text_color=_theme(s.color),
                checkbox_width=18,
                checkbox_height=18,
                border_width=2,
                fg_color=_theme(s.color),
                hover_color=_theme(s.color),
                font=ctk.CTkFont(size=12),
            ).pack(side="left", padx=(2, 10), pady=2)

        n_default = sum(1 for s in self._series if s.default_on)
        self._canvas = tk.Canvas(
            self, height=self._canvas_height(n_default),
            highlightthickness=0, bg=_theme(_BG),
        )
        self._canvas.pack(fill="x", padx=8, pady=8)
        self._canvas.bind("<Configure>", self._schedule_redraw)
        # Hover crosshair: snap to the nearest sample and show exact values.
        self._plot: dict[str, Any] | None = None  # geometry of last redraw
        self._canvas.bind("<Motion>", self._on_motion)
        self._canvas.bind("<Leave>", lambda _e: self._canvas.delete("hover"))

    # ---- drawing ---------------------------------------------------------
    def _visible_series(self) -> list[_ChartSeries]:
        return [s for s in self._series if self._visible[s.key].get()]

    def _schedule_redraw(self, _event: Any = None) -> None:
        """Debounce resize-driven redraws while the window is being dragged."""
        if self._resize_job is not None:
            self.after_cancel(self._resize_job)
        self._resize_job = self.after(75, self._redraw)

    def _active_xs(self) -> Sequence[float | None]:
        """The x-array for the current mode (may contain None in distance mode).

        Returned as a read-only ``Sequence`` (covariant) so the ``list[float]``
        time axis and the ``list[float | None]`` distance axis share one return
        type; callers only iterate/index, never mutate.
        """
        return self._xs_distance if self._x_mode == "distance" else self._xs_time

    def _fmt_x(self, value: float) -> str:
        """Format an x-axis value for the current mode (time or distance)."""
        if self._x_mode == "distance":
            return _fmt_distance(value, self._dist_km)
        return fmt_time(value)

    def _set_x_mode(self, value: str) -> None:
        """Switch the shared x axis between time and distance and redraw.

        Args:
            value: The selected segmented-button label ("Time" or "Distance").
        """
        self._x_mode = "distance" if value == "Distance" else "time"
        self._canvas.delete("hover")
        self._redraw()

    @classmethod
    def _phase_space(cls, x_mode: str, has_steps: bool) -> int:
        """Vertical space used by the planned-phase strip, when applicable."""
        if x_mode != "time" or not has_steps:
            return 0
        return cls._PHASE_H + cls._PHASE_GAP

    def _canvas_height(self, n_panels: int) -> int:
        n = max(n_panels, 1)
        return (
            self._PAD_T
            + self._phase_space(self._x_mode, bool(self._steps))
            + n * self._PANEL_H
            + (n - 1) * self._PANEL_GAP
            + self._PAD_B
        )

    def _redraw(self) -> None:
        self._resize_job = None
        c = self._canvas
        c.delete("all")
        self._plot = None

        shown = self._visible_series()
        height = self._canvas_height(len(shown))
        # Resizing to fit the selected panels retriggers <Configure> once; the
        # second pass draws at the final size and stops (same height → no-op).
        c.configure(height=height)
        w = c.winfo_width()
        if w < 120:
            return
        x0, x1 = self._PAD_L, w - self._PAD_R
        grid, text = _theme(_GRID), _theme(_TEXT)

        xs = self._active_xs()
        if not shown or not xs:
            c.create_text(
                (x0 + x1) / 2, height / 2,
                text="no series selected" if self._series else "no data",
                fill=text,
            )
            return

        # Domain over the valid (non-None) x values only: in distance mode the
        # last record can legitimately have a missing reading, so xs[-1] is not
        # safe to use as the maximum.
        x_max = max((x for x in xs if x is not None), default=1.0)
        x_max = max(x_max, 1.0)

        def px(t: float) -> float:
            return x0 + (t / x_max) * (x1 - x0)

        # X ticks are shared by all panels; labels go under the last one.
        if self._x_mode == "distance":
            x_step: float = float(_distance_step(x_max))
            self._dist_km = x_max >= 1000.0  # keep the whole axis in one unit
        else:
            x_step = float(_time_step(x_max))
        ticks: list[float] = []
        t = 0.0
        while t <= x_max + 1e-9:
            ticks.append(t)
            t += x_step
        phase_space = self._phase_space(self._x_mode, bool(self._steps))
        panels_top = self._PAD_T + phase_space
        bottom = panels_top + len(shown) * self._PANEL_H + (
            len(shown) - 1
        ) * self._PANEL_GAP

        # Planned phases get one compact time-aligned strip instead of a
        # repeated background fill in every metric panel. Short steps remain
        # visible as color segments without forcing unreadable labels. Their
        # boundaries continue through all panels as shared alignment guides.
        if phase_space:
            phase_y0 = self._PAD_T
            phase_y1 = phase_y0 + self._PHASE_H
            boundaries: set[float] = set()
            for step_start, step_end, _label, kind in self._steps:
                left = max(min(step_start, x_max), 0.0)
                right = max(min(step_end, x_max), 0.0)
                if right <= left:
                    continue
                boundaries.update((left, right))
                fill = _theme(_STEP_FILLS.get(kind or "", _STEP_DEFAULT_FILL))
                c.create_rectangle(
                    px(left), phase_y0, px(right), phase_y1,
                    fill=fill, outline="",
                )
                if px(right) - px(left) >= 42:
                    c.create_text(
                        (px(left) + px(right)) / 2,
                        (phase_y0 + phase_y1) / 2,
                        text=(kind or "step").capitalize(),
                        fill=text,
                        font=("", 9),
                    )
            c.create_rectangle(x0, phase_y0, x1, phase_y1, outline=grid)
            for boundary in sorted(boundaries):
                x = px(boundary)
                c.create_line(x, phase_y0, x, phase_y1, fill=grid)
                c.create_line(x, panels_top, x, bottom, fill=grid)

        panels: list[tuple[_ChartSeries, float, float, float, float]] = []
        for k, s in enumerate(shown):
            py0 = panels_top + k * (self._PANEL_H + self._PANEL_GAP)
            py1 = py0 + self._PANEL_H

            lo, hi = _axis_bounds(s.ys, floor=s.floor, ceiling=s.ceiling)
            panels.append((s, lo, hi, py0, py1))
            line = _theme(s.color)

            def py(v: float, lo: float = lo, hi: float = hi,
                   py0: float = py0, py1: float = py1) -> float:
                # Clamp to the panel so a clipped outlier pins to the edge
                # instead of drawing outside its frame.
                y = py1 - (v - lo) / (hi - lo) * (py1 - py0)
                return min(py1, max(py0, y))

            # Panel frame + axis gridlines clipped to this panel.
            c.create_rectangle(x0, py0, x1, py1, outline=grid)
            for tick in ticks:
                x = px(tick)
                c.create_line(x, py0, x, py1, fill=grid)

            # This panel's own value axis (~3 gridlines at nice steps).
            step = _nice_step(hi - lo, target_ticks=3)
            v = math.ceil(lo / step) * step
            while v <= hi + 1e-9:
                y = py(v)
                c.create_line(x0, y, x1, y, fill=grid)
                c.create_text(
                    x0 - 8, y, text=s.fmt(v), anchor="e", fill=text,
                    font=("", 10),
                )
                v += step

            # Series name, and a dashed average line.
            c.create_text(
                x0 + 6, py0 + 9, text=f"{s.label}  ({s.unit})",
                anchor="w", fill=line, font=("", 10, "bold"),
            )
            authoritative_avg = s.stats[0] if s.stats is not None else None
            if authoritative_avg is not None and lo < authoritative_avg < hi:
                y = py(authoritative_avg)
                c.create_line(x0, y, x1, y, fill=line, dash=(4, 4))
                c.create_text(
                    x1 - 4, y - 8, text=f"avg {s.fmt(authoritative_avg)}",
                    anchor="e", fill=line, font=("", 10),
                )

            # Polyline, broken at None gaps. A missing x coordinate (possible
            # in distance mode) breaks the line exactly like a missing value.
            cache_key = (self._x_mode, s.key, max(int(x1 - x0), 1))
            indices = self._downsample_cache.get(cache_key)
            if indices is None:
                indices = _downsample_indices(
                    xs, s.ys, self._breaks, max(int(x1 - x0), 1)
                )
                self._downsample_cache[cache_key] = indices
            points: list[float] = []
            for i in indices:
                xt = xs[i]
                yv = s.ys[i]
                if i < len(self._breaks) and self._breaks[i] and points:
                    if len(points) >= 4:
                        c.create_line(*points, fill=line, width=2)
                    elif len(points) == 2:
                        x, y = points
                        c.create_oval(
                            x - 2, y - 2, x + 2, y + 2, fill=line, outline=line
                        )
                    points = []
                if xt is None or yv is None:
                    if len(points) >= 4:
                        c.create_line(*points, fill=line, width=2)
                    points = []
                    continue
                points.extend((px(xt), py(yv)))
            if len(points) >= 4:
                c.create_line(*points, fill=line, width=2)
            elif len(points) == 2:  # single isolated point
                x, y = points
                c.create_oval(x - 2, y - 2, x + 2, y + 2, fill=line, outline=line)

        # X labels once, under the bottom panel. Edge labels are anchored
        # inward so they are never clipped by the canvas border.
        for tick in ticks:
            x = px(tick)
            anchor: Literal["n", "nw", "ne"] = "n"
            if x < x0 + 24:
                anchor = "nw"
            elif x > x1 - 24:
                anchor = "ne"
            c.create_text(
                x, bottom + 6, text=self._fmt_x(tick), anchor=anchor,
                fill=text, font=("", 10),
            )

        # Non-None (index, x) pairs, in x order, for the hover crosshair to
        # bisect over — distance mode can contain None, which bisect can't
        # compare. In time mode this is every index with xs unchanged, so the
        # snapping is identical to the pre-toggle behavior.
        valid_pairs = [(i, x) for i, x in enumerate(xs) if x is not None]

        # Geometry for the hover crosshair.
        self._plot = {
            "x0": x0, "x1": x1, "top": panels_top, "bottom": bottom,
            "x_max": x_max, "panels": panels,
            "valid_pairs": valid_pairs,
            "valid_xs": [x for _, x in valid_pairs],
        }

    # ---- hover crosshair ---------------------------------------------------
    def _on_motion(self, event: Any) -> None:
        c = self._canvas
        c.delete("hover")
        plot = self._plot
        valid_xs: list[float] = plot["valid_xs"] if plot else []
        if plot is None or not valid_xs:
            return
        x0, x1 = plot["x0"], plot["x1"]
        top, bottom = plot["top"], plot["bottom"]
        if not (x0 - 4 <= event.x <= x1 + 4):
            return

        # Snap to the sample nearest to the cursor. Bisect only over the valid
        # (non-None) x values, then map back to the real record index; in time
        # mode valid_xs == xs so this is identical to the old direct bisect.
        t = (min(max(event.x, x0), x1) - x0) / (x1 - x0) * plot["x_max"]
        j = bisect.bisect_left(valid_xs, t)
        if j > 0 and (
            j >= len(valid_xs) or abs(valid_xs[j - 1] - t) <= abs(valid_xs[j] - t)
        ):
            j -= 1
        i, xi = plot["valid_pairs"][j]
        tx = x0 + (xi / plot["x_max"]) * (x1 - x0)

        grid, text = _theme(_GRID), _theme(_TEXT)
        # One crosshair spanning every panel, plus a dot per panel.
        c.create_line(tx, top, tx, bottom, fill=text, dash=(2, 3), tags="hover")

        rows: list[tuple[str, str]] = [(self._fmt_x(xi), text)]
        for s, lo, hi, py0, py1 in plot["panels"]:
            v = s.ys[i]
            if v is None:
                continue
            y = py1 - (v - lo) / (hi - lo) * (py1 - py0)
            y = min(py1, max(py0, y))  # match the clamped polyline
            color = _theme(s.color)
            c.create_oval(
                tx - 3, y - 3, tx + 3, y + 3,
                fill=color, outline=color, tags="hover",
            )
            rows.append((s.hover_text(v), color))

        # Tooltip box, flipped to the left near the right edge.
        line_h, pad = 16, 8
        box_w = 10 + max(7 * len(r[0]) for r in rows) + pad
        box_h = pad * 2 + line_h * len(rows)
        bx = tx + 12 if tx + 12 + box_w <= x1 + self._PAD_R else tx - 12 - box_w
        by = min(max(event.y - box_h / 2, top), max(bottom - box_h, top))
        c.create_rectangle(
            bx, by, bx + box_w, by + box_h,
            fill=_theme(_BG), outline=grid, tags="hover",
        )
        for n, (label, color) in enumerate(rows):
            c.create_text(
                bx + pad, by + pad + line_h * n + line_h / 2,
                text=label, anchor="w", fill=color,
                font=("", 10, "bold" if n == 0 else ""), tags="hover",
            )


def _fmt_opt(value: float | None, digits: int = 0) -> str:
    if value is None:
        return "—"
    return f"{value:,.{digits}f}"


def _fmt_kmh(speed_mps: float | None) -> str:
    if speed_mps is None:
        return "—"
    return f"{speed_mps * MPS_TO_KMH:,.1f}"


def _stat_block(master: Any, label: str, value: str, sub: str = "") -> None:
    box = ctk.CTkFrame(master, corner_radius=10)
    box.pack(side="left", expand=True, fill="both", padx=4)
    ctk.CTkLabel(
        box, text=value, font=ctk.CTkFont(size=20, weight="bold")
    ).pack(anchor="w", padx=12, pady=(10, 0))
    subtext = f"{label}  ·  {sub}" if sub else label
    ctk.CTkLabel(
        box, text=subtext, text_color=_TEXT, font=ctk.CTkFont(size=12)
    ).pack(anchor="w", padx=12, pady=(0, 10))


def _hr_zone_parts(
    summary: WorkoutSummary,
) -> tuple[list[tuple[str, float, tuple[str, str]]], float]:
    """Return zone/no-signal seconds normalized against total moving time."""
    measured_hr_s = sum(summary.zone_seconds)
    total = max(float(summary.active_time_s), measured_hr_s)
    no_hr_s = max(total - measured_hr_s, 0.0)
    parts = [
        (name, secs, color)
        for name, secs, color in zip(
            _ZONE_NAMES, summary.zone_seconds, _ZONE_COLORS, strict=True
        )
    ]
    if no_hr_s > 0:
        parts.append(("No HR signal", no_hr_s, _NO_HR_COLOR))
    return parts, total


def _build_zone_bar(master: Any, summary: WorkoutSummary) -> None:
    """Stacked horizontal time-in-zone bar with a legend underneath."""
    parts, total = _hr_zone_parts(summary)
    if total <= 0:
        return
    frame = ctk.CTkFrame(master, corner_radius=10)
    frame.pack(fill="x", padx=10, pady=(0, 8))
    ctk.CTkLabel(
        frame, text="Time in heart-rate zones",
        font=ctk.CTkFont(size=12, weight="bold"), text_color=_TEXT,
    ).pack(anchor="w", padx=14, pady=(10, 4))

    canvas = tk.Canvas(frame, height=26, highlightthickness=0, bg=_theme(_BG))
    canvas.pack(fill="x", padx=14, pady=(0, 6))

    def redraw(_e: Any = None) -> None:
        canvas.delete("all")
        w = canvas.winfo_width()
        if w < 50:
            return
        x = 0.0
        for _name, secs, color in parts:
            if secs <= 0:
                continue
            width = w * secs / total
            canvas.create_rectangle(
                x, 2, x + width, 24, fill=_theme(color), width=0
            )
            if width > 48:
                canvas.create_text(
                    x + width / 2, 13, text=fmt_time(secs),
                    fill=_theme(_ZONE_TEXT), font=("", 10),
                )
            x += width

    canvas.bind("<Configure>", redraw)

    legend = ctk.CTkFrame(frame, fg_color="transparent")
    legend.pack(anchor="w", padx=12, pady=(0, 10))
    for name, secs, color in parts:
        pct = 100.0 * secs / total
        ctk.CTkLabel(
            legend,
            text=f"{name}  {pct:,.0f}%",
            fg_color=color,
            text_color=_ZONE_TEXT,
            corner_radius=6, padx=8, pady=2,
            font=ctk.CTkFont(size=11),
        ).pack(side="left", padx=2)


def _build_splits_table(master: Any, summary: WorkoutSummary) -> None:
    """Build a splits table, appending rows in short UI-friendly batches."""
    frame = ctk.CTkFrame(master, corner_radius=10)
    frame.pack(fill="x", padx=10, pady=(0, 12))
    ctk.CTkLabel(
        frame, text="Splits", font=ctk.CTkFont(size=12, weight="bold"),
        text_color=_TEXT,
    ).grid(row=0, column=0, columnspan=2, sticky="w", padx=14, pady=(10, 4))

    # Program-driven workouts carry the step behind each lap; show it so a
    # rest lap the athlete paddled through is not an anonymous number.
    show_step = any(lap.label or lap.kind for lap in summary.laps)
    show_gap = any(lap.pause_after_s > 0 for lap in summary.laps)
    headers = ["Lap"]
    if show_step:
        headers.append("Step")
    headers.extend(["Start", "Elapsed", "Active"])
    if show_gap:
        headers.append("Gap after")
    headers.extend(["Distance", "Speed km/h", "Avg HR", "Est. W", "SPM"])
    left_cols = 2 if show_step else 1  # left-aligned leading columns
    header_font = ctk.CTkFont(size=12, weight="bold")
    cell_font = ctk.CTkFont(size=12)
    for col, text in enumerate(headers):
        ctk.CTkLabel(
            frame, text=text, anchor="w" if col < left_cols else "e",
            font=header_font, text_color=_TEXT,
        ).grid(
            row=1, column=col, sticky="ew",
            padx=(14 if col == 0 else 6, 14 if col == len(headers) - 1 else 6),
            pady=2,
        )
        frame.grid_columnconfigure(col, weight=1)

    def cells_for(lap: Any) -> list[str]:
        cells = [str(lap.index)]
        if show_step:
            step_text = lap.label or (lap.kind or "").capitalize() or "—"
            cells.append(step_text)
        cells.extend(
            [
                fmt_time_precise(lap.start_s),
                fmt_time_precise(lap.elapsed_time_s),
                fmt_time_precise(lap.active_time_s),
            ]
        )
        if show_gap:
            cells.append(fmt_time_precise(lap.pause_after_s))
        cells.extend(
            [
                fmt_metres(lap.distance_m),
                _fmt_kmh(lap.avg_speed_mps),
                _fmt_opt(lap.avg_hr),
                _fmt_opt(lap.avg_power),
                _fmt_opt(lap.avg_spm),
            ]
        )
        return cells

    next_row = 0
    progress = ctk.CTkLabel(
        frame, text="", text_color=_TEXT, font=ctk.CTkFont(size=11)
    )

    def render_rows(batch_size: int) -> None:
        """Append at most one small batch, yielding to Tk between batches."""
        nonlocal next_row
        if not frame.winfo_exists():
            return
        progress.grid_forget()
        end = min(next_row + batch_size, len(summary.laps))
        for index in range(next_row, end):
            lap = summary.laps[index]
            cells = cells_for(lap)
            row = index + 2
            is_last = index == len(summary.laps) - 1
            for col, text in enumerate(cells):
                ctk.CTkLabel(
                    frame, text=text, anchor="w" if col < left_cols else "e",
                    font=cell_font,
                ).grid(
                    row=row, column=col, sticky="ew",
                    padx=(14 if col == 0 else 6, 14 if col == len(cells) - 1 else 6),
                    pady=(1, 10 if is_last else 1),
                )
        next_row = end
        if next_row < len(summary.laps):
            progress.configure(text=f"Loading splits… {next_row}/{len(summary.laps)}")
            progress.grid(
                row=next_row + 2, column=0, columnspan=len(headers), pady=(4, 8)
            )
            frame.after(
                _SPLIT_RENDER_DELAY_MS,
                lambda: render_rows(_SPLIT_RENDER_BATCH_ROWS),
            )

    render_rows(_INITIAL_SPLIT_ROWS)


def _load_summary(csv_path: str) -> WorkoutSummary:
    """Load and compute a summary without touching Tk."""
    metadata = load_workout_metadata(csv_path)
    config = load_workout_config(csv_path)
    table = CsvReader(csv_path, config).read_all(
        data_points=str(config.get("data_points", "instant"))
    )
    zones = None
    if str(config.get("hr_zone_mode", "auto")).lower() == "manual":
        zones = list(config.get("hr_zones") or []) or None
    return compute_summary(
        table,
        max_hr=config.get("max_hr", 185),
        zones=zones,
        program_steps=(
            load_program_steps(csv_path)
            if metadata["workout_mode"] == "planned"
            else None
        ),
        config=config,
    )


def _render_summary_window(root: Any, csv_path: str, summary: WorkoutSummary) -> None:
    """Create Tk widgets for an already-computed summary."""
    win = ctk.CTkToplevel(master=root)
    when = (
        summary.start_time.strftime("%A %d %B %Y · %H:%M")
        if summary.start_time
        else Path(csv_path).name
    )
    win.title(f"Workout summary — {when}")
    center_over_parent(win, root, 900, 780)
    win.minsize(680, 480)
    win.transient(root)
    win.after(100, win.lift)
    win.after(120, win.focus_force)

    body = ctk.CTkScrollableFrame(win, fg_color="transparent")
    body.pack(fill="both", expand=True, padx=6, pady=6)

    ctk.CTkLabel(
        body, text=when, font=ctk.CTkFont(size=16, weight="bold")
    ).pack(anchor="w", padx=12, pady=(12, 8))

    stats = ctk.CTkFrame(body, fg_color="transparent")
    stats.pack(fill="x", padx=8, pady=(0, 8))
    _stat_block(stats, "Active distance", fmt_metres(summary.distance_m))
    _stat_block(
        stats,
        "Active time",
        fmt_time_precise(summary.active_time_s),
        f"elapsed {fmt_time_precise(summary.elapsed_time_s)}"
        f" · pause {fmt_time_precise(summary.pause_time_s)}",
    )
    _stat_block(
        stats, "Avg speed", _fmt_kmh(summary.avg_speed_mps),
        f"km/h · max {_fmt_kmh(summary.max_speed_mps)}",
    )
    _stat_block(
        stats, "Avg HR", _fmt_opt(summary.avg_hr),
        f"max {_fmt_opt(float(summary.max_hr) if summary.max_hr else None)}",
    )
    _stat_block(
        stats, "Avg est. power", _fmt_opt(summary.avg_power),
        f"max (1 s) {_fmt_opt(float(summary.max_power) if summary.max_power else None)}",
    )
    _stat_block(
        stats,
        "Avg stroke rate",
        _fmt_opt(summary.avg_spm),
        f"spm · max {_fmt_opt(float(summary.max_spm) if summary.max_spm else None)}",
    )

    details = ctk.CTkFrame(body, fg_color="transparent")
    details.pack(fill="x")
    preparing = ctk.CTkLabel(
        details,
        text="Preparing charts and splits…",
        text_color=_TEXT,
        font=ctk.CTkFont(size=12),
    )
    preparing.pack(anchor="w", padx=14, pady=(4, 12))

    def render_details() -> None:
        """Build expensive widgets after the headline window has painted."""
        if not win.winfo_exists() or not details.winfo_exists():
            return
        preparing.destroy()
        fmt1 = lambda v: f"{v:,.1f}"
        fmt0 = lambda v: f"{v:,.0f}"
        _MultiChart(
            details,
            summary.series_t,
            summary.series_distance,
            [
                _ChartSeries(
                    "speed", "Speed", "km/h",
                    _smooth(
                        _kmh(summary.series_speed),
                        summary.series_breaks,
                        preserve_max=True,
                    ),
                    _SPEED, fmt1, floor=0.0, ceiling=25.0,
                    stats=(
                        summary.avg_speed_mps * MPS_TO_KMH
                        if summary.avg_speed_mps is not None else None,
                        summary.max_speed_mps * MPS_TO_KMH
                        if summary.max_speed_mps is not None else None,
                    ),
                ),
                _ChartSeries(
                    "power", "Est. power", "W",
                    _smooth(
                        summary.series_power,
                        summary.series_breaks,
                        preserve_max=True,
                    ),
                    _POWER, fmt0, floor=0.0,
                    stats=(
                        summary.avg_power,
                        float(summary.max_power) if summary.max_power else None,
                    ),
                ),
                _ChartSeries(
                    "hr", "HR", "bpm",
                    _smooth(
                        summary.series_hr,
                        summary.series_breaks,
                        preserve_max=True,
                    ), _HR, fmt0,
                    floor=0.0, ceiling=250.0,
                    stats=(
                        summary.avg_hr,
                        float(summary.max_hr) if summary.max_hr else None,
                    ),
                ),
                _ChartSeries(
                    "spm", "Cadence", "spm",
                    _smooth(
                        summary.series_spm,
                        summary.series_breaks,
                        preserve_max=True,
                    ), _CADENCE,
                    fmt0, default_on=False, floor=0.0,
                    stats=(
                        summary.avg_spm,
                        float(summary.max_spm) if summary.max_spm else None,
                    ),
                ),
                _ChartSeries(
                    "pull", "Pull", "N",
                    _smooth(
                        summary.series_pull,
                        summary.series_breaks,
                        preserve_max=True,
                    ), _PULL,
                    fmt0, default_on=False, floor=0.0,
                ),
            ],
            steps=summary.series_steps,
            breaks=summary.series_breaks,
        ).pack(fill="x", padx=10, pady=(0, 8))

        if any(v is not None for v in summary.series_hr):
            _build_zone_bar(details, summary)
        if summary.laps:
            _build_splits_table(details, summary)

    win.after(_DETAIL_RENDER_DELAY_MS, render_details)


def open_summary_window(
    root: Any, csv_path: str | None, config: dict[str, Any]
) -> None:
    """Load a workout summary in the background, then open its window.

    Args:
        root: Parent window.
        csv_path: Path to the workout CSV file. ``None``/empty is a no-op (the
            summary button can be wired before a workout has been recorded).
        config: App configuration (used for HR zone bounds and data-point mode).
    """
    if not csv_path:
        return
    key = str(Path(csv_path).resolve())
    existing = _SUMMARY_LOADS.get(key)
    if existing is not None and existing.winfo_exists():
        existing.lift()
        return
    loading = ctk.CTkToplevel(master=root)
    _SUMMARY_LOADS[key] = loading
    loading.title("Workout summary")
    loading.transient(root)
    center_over_parent(loading, root, 360, 120)
    ctk.CTkLabel(loading, text="Loading workout summary…").pack(
        expand=True, padx=24, pady=(20, 8)
    )
    progress = ctk.CTkProgressBar(loading, mode="indeterminate")
    progress.pack(fill="x", padx=24, pady=(0, 20))
    progress.start()

    def run() -> None:
        try:
            summary = _load_summary(csv_path)
            error: Exception | None = None
        except Exception as exc:
            summary = None
            error = exc

        def finish() -> None:
            _SUMMARY_LOADS.pop(key, None)
            if loading.winfo_exists():
                progress.stop()
                loading.destroy()
            if error is not None:
                messagebox.showerror(
                    "Workout summary", f"Could not read workout:\n{error}", parent=root
                )
            elif summary is not None:
                _render_summary_window(root, csv_path, summary)

        root.after(0, finish)

    threading.Thread(target=run, daemon=True).start()
