"""Shared visual constants for the KayakFit dashboard GUI.

Color sets, tile definitions, HR-zone backgrounds and timing thresholds shared
by ``main_gui`` and its mixin modules. Kept in their own module so the mixins
and the main window import them from one place instead of from each other.
"""

# (id, label, unit, show_stats) for each dashboard tile. show_stats adds an
# avg/max secondary line.
TILES = [
    ("time", "Time", "", False),
    ("distance", "Distance", "m", False),
    ("speed", "Speed", "km/h", True),
    ("pace", "Pace", "", False),
    ("stroke", "Stroke rate", "spm", True),
    ("hr", "Heart rate", "bpm", True),
    ("pull", "Pull force", "N", True),
    ("power", "Power", "W · est.", True),
]

DASHBOARD_COLS = 4

# Chip color sets as (light, dark) tuples for customtkinter.
CHIP_COLORS = {
    "muted": (("#e6e6e6", "#2b2b2b"), ("#6f6f6f", "#9a9a9a")),
    "info": (("#dceafc", "#11365e"), ("#1f6fd0", "#9fc6f5")),
    "ok": (("#d8f3e3", "#16432f"), ("#1d7a4f", "#7ee0ab")),
    "warn": (("#fbeccc", "#4a3508"), ("#9a6a06", "#f0c068")),
}
# Heart-rate zone tile backgrounds (light, dark), indexed by zone 0-4.
HR_ZONE_BG = [
    ("#e9e9e9", "#2b2b2b"),  # 0 recovery
    ("#dceafc", "#11365e"),  # 1 easy
    ("#d8f3e3", "#16432f"),  # 2 aerobic
    ("#fbeccc", "#4a3508"),  # 3 threshold
    ("#f7d7d2", "#5a1e16"),  # 4 max
]
HR_COLOR = ("#c0392b", "#f0a098")
SUBTEXT_COLOR = ("#5f5f5f", "#9a9a9a")
# Muted color applied to tile values while a workout is auto-paused.
TILE_DIM_COLOR = ("#b4b4b4", "#565656")

# Program step kind -> audible cue. Each kind sounds distinct so the athlete
# knows which change happened by ear; "effort" shares the "work" go-cue, and
# anything unmapped falls back to the generic "phase" tone.
STEP_KIND_CUES = {
    "warmup": "warmup",
    "work": "work",
    "effort": "work",
    "rest": "rest",
    "cooldown": "cooldown",
}

# Step-progress bar color per step kind, as (light, dark). Warm hues for effort,
# cool hues for easy phases, so the bar's color signals the phase at a glance.
# Unmapped kinds use STEP_COLOR_DEFAULT.
STEP_KIND_COLORS = {
    "warmup": ("#e08a1e", "#f0b45a"),    # amber — easing in
    "work": ("#c0392b", "#e0685a"),      # red — hard effort
    "effort": ("#c0392b", "#e0685a"),    # red — hard effort
    "rest": ("#1f6fd0", "#5a9be8"),      # blue — recovery
    "cooldown": ("#2a9d8f", "#5cc9bb"),  # teal — winding down
}
STEP_COLOR_DEFAULT = ("#1d7a4f", "#7ee0ab")  # green — generic segment / complete

# Program target-adherence badge colors: (background, text) as (light, dark).
TARGET_COLORS = {
    "in": (("#d8f3e3", "#16432f"), ("#1d7a4f", "#7ee0ab")),    # on target — green
    "hard": (("#f7d7d2", "#5a1e16"), ("#c0392b", "#f0a098")),  # too hard — red
    "easy": (("#fbeccc", "#4a3508"), ("#9a6a06", "#f0c068")),  # too easy — amber
}
TARGET_LABELS = {"in": "On target", "hard": "Too hard ↑", "easy": "Too easy ↓"}
DANGER_BTN = "#c0392b"
DANGER_BTN_HOVER = "#a02c20"

# How long without a heart-rate reading before the HR tile is dimmed (seconds).
HR_STALE_SECONDS = 10
# How long without any ergometer data before the live indicator warns (seconds).
DATA_STALE_SECONDS = 3
