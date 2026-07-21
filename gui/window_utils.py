"""Small window-placement helpers shared by all KayakFit windows.

Tk opens new windows wherever the window manager likes (often the top-left
corner, or stacked exactly over the previous window). For a polished feel —
and clean screenshots — the main window opens centered on the screen and
every dialog opens centered over its parent, clamped so it never runs off
the visible screen.

Known limitation: Tk reports only the primary display (Windows) or the
combined virtual desktop (X11), so clamping is skipped when the parent lies
outside those bounds (e.g. on a second monitor) rather than dragging the
dialog onto the wrong screen.
"""

import contextlib
import os
import subprocess
import sys
import tkinter as tk
from pathlib import Path

# Window types accepted by the placement helpers (covers CTk/CTkToplevel,
# which subclass tk.Tk/tk.Toplevel).
_Window = tk.Tk | tk.Toplevel

# Margins kept free at the screen edges (dock / taskbar at the bottom).
_SCREEN_MARGIN_BOTTOM = 60
_SCREEN_MARGIN_TOP = 30
_SCREEN_MARGIN_SIDE = 30

# Tk's point->pixel scaling is DPI/72. The norm is ~1.0 on macOS (Retina
# backing is handled by the OS, not by Tk) but 96/72 ~= 1.333 on Windows/X11
# at 100% OS scaling — and legitimately higher on HiDPI displays (~1.667 at
# 125%, ~2.667 at 200%), so those values must NOT be clamped. Some
# virtualized or headless displays report a bogus DPI, so Tk picks an absurd
# factor and every point-sized font/widget renders oversized — windows then
# overflow the screen. We only override clearly out-of-range values so a
# correctly detected host display is never touched.
_SCALING_RANGE_MAC = (0.7, 1.4)
_SCALING_RANGE_OTHER = (0.9, 4.0)


def open_in_file_manager(path: str | Path) -> None:
    """Open a path in Finder, Explorer, or the freedesktop file manager."""
    target = str(path)
    if sys.platform == "darwin":
        subprocess.Popen(["open", target])
    elif os.name == "nt":
        os.startfile(target)  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", target])


def normalize_tk_scaling(root: tk.Misc) -> None:
    """Reset Tk scaling when the detected value is implausible for the platform.

    Fixes oversized UI on VMs/remote displays that mis-report screen DPI while
    leaving correctly configured displays (including Windows/Linux HiDPI)
    unchanged. Best-effort: any failure leaves the default scaling in place.
    """
    if sys.platform == "darwin":
        baseline = 1.0
        lo, hi = _SCALING_RANGE_MAC
    else:
        baseline = 96 / 72  # Tk's Windows/X11 norm at 100% OS scaling
        lo, hi = _SCALING_RANGE_OTHER
    try:
        current = float(root.tk.call("tk", "scaling"))
    except Exception:
        return
    if current < lo or current > hi:
        with contextlib.suppress(Exception):
            root.tk.call("tk", "scaling", baseline)


def _clamp_to_screen(win: _Window, width: int, height: int) -> tuple[int, int, int, int]:
    """Shrink a requested size so it fits within the usable screen area."""
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    width = min(width, max(sw - 2 * _SCREEN_MARGIN_SIDE, 320))
    height = min(height, max(sh - _SCREEN_MARGIN_TOP - _SCREEN_MARGIN_BOTTOM, 320))
    return width, height, sw, sh


def center_on_screen(win: _Window, width: int, height: int) -> None:
    """Size ``win`` and center it on the screen (slightly above the middle)."""
    win.update_idletasks()
    width, height, sw, sh = _clamp_to_screen(win, width, height)
    x = max((sw - width) // 2, 0)
    y = max((sh - height) // 3, _SCREEN_MARGIN_TOP)  # optical center
    win.geometry(f"{width}x{height}+{x}+{y}")


def center_over_parent(win: _Window, parent: tk.Misc, width: int, height: int) -> None:
    """Size ``win`` and center it over ``parent``, kept fully on screen."""
    win.update_idletasks()
    width, height, sw, sh = _clamp_to_screen(win, width, height)
    try:
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        pw, ph = parent.winfo_width(), parent.winfo_height()
    except Exception:  # parent destroyed or in teardown
        pw = ph = 0
        px = py = 0
    if pw <= 1 or ph <= 1:
        # An unmapped window reports 1x1 (Tk never raises for this) — centering
        # over it would pin the dialog to the parent's top-left corner.
        center_on_screen(win, width, height)
        return
    x = px + (pw - width) // 2
    y = py + (ph - height) // 2
    if 0 <= px < sw and 0 <= py < sh:
        # Parent is on the screen Tk knows about — keep the dialog fully
        # visible. When the parent sits beyond the reported bounds (second
        # monitor), trust the parent-centered position instead.
        x = min(max(x, 0), max(sw - width, 0))
        y = min(max(y, _SCREEN_MARGIN_TOP), max(sh - height - _SCREEN_MARGIN_BOTTOM, 0))
    win.geometry(f"{width}x{height}+{x}+{y}")
