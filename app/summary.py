"""Post-workout summary computation.

Turns a recorded workout (the Table produced by :class:`app.read_csv.CsvReader`)
into the numbers the summary screen shows: totals and averages, per-lap splits,
time in each heart-rate zone, and per-second series for the charts.

Pure computation with no GUI dependencies, so it is unit-testable and reusable
(e.g. by a future trends view). Records, timer runs and lap groups come from the
same finalized workout model consumed by the FIT exporter, so the splits always
match the laps written to the FIT file, program-driven or freestyle alike. Time
in HR zones is accumulated over those same moving segments via
:func:`app.stats.time_in_hr_zones`; missing readings remain uncovered time so
the UI can show HR-signal coverage honestly.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .coerce import to_float
from .finalized_workout import finalize_workout
from .logger import Logger
from .power import DEFAULT_PULL_LENGTH_M, estimate_power, normalize_pull_length
from .stats import (
    aggregate_metrics,
    time_in_hr_zones,
    zone_lower_bounds,
)
from .table import Table

logger = Logger.get_logger(name=__name__)


def _moving_speed(speed_mps: float | None) -> float | None:
    """Speed in m/s, or None when not meaningfully moving (matches CsvReader)."""
    if speed_mps is None or speed_mps <= 0.3:
        return None
    return speed_mps


@dataclass
class LapSummary:
    """Aggregates for one moving segment (one FIT lap)."""

    index: int  # 1-based lap number
    start_s: float  # offset from workout start (wall clock, seconds)
    duration_s: float  # active-paddling timer time
    elapsed_s: float  # step/lap wall time, including inactive rest
    distance_m: float
    avg_speed_mps: float | None
    avg_hr: float | None
    max_hr: int | None
    avg_power: float | None
    max_power: int | None
    avg_spm: float | None
    max_spm: int | None
    # Program step this lap came from (None for movement-detected laps), so a
    # UI can label rest vs. work laps instead of anonymous numbers.
    kind: str | None = None  # warmup | work | rest | cooldown | effort
    label: str | None = None  # e.g. "Work 10:00"


@dataclass
class WorkoutSummary:
    """Everything the summary screen needs, computed once from the CSV."""

    start_time: datetime | None
    duration_s: float  # moving time (sum of laps)
    distance_m: float
    avg_speed_mps: float | None  # over moving time
    max_speed_mps: float | None
    avg_hr: float | None
    max_hr: int | None
    avg_power: float | None
    max_power: int | None
    avg_spm: float | None
    max_spm: int | None
    zone_seconds: list[float] = field(default_factory=lambda: [0.0] * 5)
    laps: list[LapSummary] = field(default_factory=list)
    # Chart series, one entry per active sensor record emitted to FIT. Synthetic
    # GPS start anchors are export-only compatibility records and do not appear.
    # ``series_t`` is seconds since the first record (wall clock); the value
    # series contain None for gaps so a chart can break the line instead of
    # interpolating across pauses. ``series_distance`` is the cumulative
    # distance (metres) at each record, an alternative x-axis for the same
    # charts; unlike ``series_t`` it can contain None where a distance reading
    # was missing (an empty CSV cell).
    series_t: list[float] = field(default_factory=list)
    series_speed: list[float | None] = field(default_factory=list)  # m/s
    series_hr: list[float | None] = field(default_factory=list)  # bpm
    series_power: list[float | None] = field(default_factory=list)  # W
    series_spm: list[float | None] = field(default_factory=list)  # strokes/min
    series_pull: list[float | None] = field(default_factory=list)  # N
    series_distance: list[float | None] = field(default_factory=list)  # m
    # True on the first record of each finalized lap or meaningful pause. The
    # chart uses these boundaries to break lines and smoothing without
    # inventing non-FIT records solely as visual sentinels.
    series_breaks: list[bool] = field(default_factory=list)
    # Planned step bands on the chart's time axis: start/end seconds relative
    # to its first active record, label and step kind. Empty for free workouts.
    series_steps: list[tuple[float, float, str, str | None]] = field(
        default_factory=list
    )


def _lap_summary(
    table: Table,
    index: int,
    record_indices: Sequence[int],
    start_time_ms: int,
    end_time_ms: int,
    timer_time_ms: int,
    record_weights_s: Sequence[float],
    distance_m: float,
    t0_ms: int,
    pull_length_m: float,
    kind: str | None = None,
    label: str | None = None,
) -> LapSummary:
    lap = table.select_rows(record_indices)

    duration_s = timer_time_ms / 1000.0
    elapsed_s = max((end_time_ms - start_time_ms) / 1000.0, 0.0)

    # Shared aggregation: HR/cadence/power over strictly-positive readings,
    # avg speed as distance/elapsed-time (see app.stats). The FIT exporter's
    # per-lap stats go through the same function, so the splits agree.
    m = aggregate_metrics(
        heart_rate=lap.get_column("heart_rate"),
        cadence=lap.get_column("cadence"),
        pull_force=lap.get_column("pull_force"),
        sample_weights=record_weights_s,
        pull_length_m=pull_length_m,
        distance_m=distance_m,
        duration_s=duration_s,
    )
    return LapSummary(
        index=index,
        start_s=(start_time_ms - t0_ms) / 1000.0,
        duration_s=duration_s,
        elapsed_s=elapsed_s,
        distance_m=distance_m,
        avg_speed_mps=(
            round(m["avg_speed"], 3)
            if m["avg_speed"] is not None
            else None
        ),
        avg_hr=(
            float(int(m["avg_heart_rate"])) if m["avg_heart_rate"] is not None else None
        ),
        max_hr=int(m["max_heart_rate"]) if m["max_heart_rate"] is not None else None,
        avg_power=(float(int(m["avg_power"])) if m["avg_power"] is not None else None),
        max_power=int(m["max_power"]) if m["max_power"] is not None else None,
        avg_spm=(
            float(int(m["avg_cadence"])) if m["avg_cadence"] is not None else None
        ),
        max_spm=int(m["max_cadence"]) if m["max_cadence"] is not None else None,
        kind=kind,
        label=label,
    )


def compute_summary(
    table: Table,
    max_hr: Any = 185,
    zones: list[int] | None = None,
    program_steps: list[dict[str, Any]] | None = None,
    config: Mapping[str, Any] | None = None,
) -> WorkoutSummary:
    """Compute a full workout summary from a workout Table.

    Args:
        table: Output of ``CsvReader.read_all`` (columns: timestamp,
            heart_rate, cadence, distance, speed, pull_force).
        max_hr: Max heart rate used to derive zone bounds (when ``zones`` is None).
        zones: Optional five ascending manual zone lower bounds (bpm).
        program_steps: Optional recorded program step timeline (the
            ``steps.json`` timeline; see ``segmentation.load_program_steps``).
            When present, laps follow the program steps exactly as the FIT
            exporter's do; otherwise laps come from movement detection.
        config: Optional app configuration mapping. Auto-pause tuning is
            applied by the same finalization path used by the FIT exporter, so
            on-screen splits and exported laps agree by construction.

    Raises:
        ValueError: If the table is empty.
    """
    if table.is_empty():
        raise ValueError("Workout contains no records.")

    finalized = finalize_workout(
        table, program_steps=program_steps, config=config, logger=logger
    )
    if not finalized.segments:
        raise ValueError("Workout contains no active ergometer samples.")
    pull_length_m = normalize_pull_length(
        config.get("pull_length_m", DEFAULT_PULL_LENGTH_M)
        if config is not None
        else DEFAULT_PULL_LENGTH_M
    )
    timestamps = finalized.timestamps_ms
    t0_ms = timestamps[0]

    speeds = [to_float(v) for v in table.get_column("speed")]
    hrs = [to_float(v) for v in table.get_column("heart_rate")]
    cadences = [to_float(v) for v in table.get_column("cadence")]
    forces = [to_float(v) for v in table.get_column("pull_force")]

    # ---- chart series ------------------------------------------------------
    series_speed = [_moving_speed(s) for s in speeds]
    series_hr = [h if h is not None and h > 0 else None for h in hrs]
    series_power = [
        (lambda p: float(p) if p is not None and p > 0 else None)(
            estimate_power(f, c, pull_length_m)
        )
        for f, c in zip(forces, cadences, strict=False)
    ]
    series_spm = [c if c is not None and c > 0 else None for c in cadences]
    series_pull = [f if f is not None and f > 0 else None for f in forces]

    # ---- laps (identical to the FIT export, by construction) ----------------
    # The single shared finalized model: program-step groups when a timeline
    # exists, natural autopause groups otherwise, with exact active runs kept
    # separately for FIT timer events and the chart record domain.
    segments = finalized.segments
    finalized_laps = finalized.laps
    summary_start_ms = segments[0]["start_time_ms"] if segments else t0_ms
    laps = [
        _lap_summary(
            table,
            i + 1,
            lap.record_indices,
            lap.start_time_ms,
            lap.end_time_ms,
            lap.timer_time_ms,
            lap.record_weights_s,
            lap.distance_m,
            summary_start_ms,
            pull_length_m,
            kind=lap.kind,
            label=lap.label,
        )
        for i, lap in enumerate(finalized_laps)
    ]

    # ---- totals --------------------------------------------------------------
    duration_s = finalized.total_timer_time_ms / 1000.0
    distance_m = finalized.total_distance_m
    moving_indices = finalized.moving_indices
    chart_breaks = set(finalized.chart_break_indices)
    chart_start_ms = timestamps[moving_indices[0]] if moving_indices else summary_start_ms
    moving_hrs = [hrs[i] for i in moving_indices]
    moving_cadences = [cadences[i] for i in moving_indices]
    moving_forces = [forces[i] for i in moving_indices]
    moving_speeds = [speeds[i] for i in moving_indices]
    moving_weights = [finalized.record_duration_s[i] for i in moving_indices]

    # Overall averages/peaks via the shared aggregator (same rules the FIT
    # exporter's session stats use): HR/cadence/power over strictly-positive
    # readings, avg speed as distance / moving time.
    #
    # avg_power is the time-average of the per-second power estimates (mean of
    # products). The machine display instead shows L x mean(force) x
    # mean(cadence) (product of means), which reads a few watts higher when
    # force and cadence co-vary — a small difference vs the display is
    # expected. max_power is the highest one-second estimate; the display's
    # "Max Power" is a sub-second within-stroke peak (~2.2x the mean) that a
    # 1 Hz log cannot reproduce.
    totals = aggregate_metrics(
        heart_rate=moving_hrs,
        cadence=moving_cadences,
        pull_force=moving_forces,
        speed=moving_speeds,
        sample_weights=moving_weights,
        distance_m=distance_m,
        duration_s=duration_s,
        pull_length_m=pull_length_m,
    )

    # ---- time in HR zones ----------------------------------------------------
    # Accumulated over the same moving segments as the laps (shared
    # app.stats.time_in_hr_zones). Missing readings deliberately remain
    # uncovered so the zone bar can display an explicit no-signal share.
    zone_seconds = time_in_hr_zones(
        heart_rate=hrs,
        segments=segments,
        bounds=zone_lower_bounds(max_hr=max_hr, zones=zones),
        sample_weights=[
            finalized.record_duration_s.get(i, 0.0) for i in range(table.height)
        ],
    )

    return WorkoutSummary(
        start_time=datetime.fromtimestamp(summary_start_ms / 1000.0),
        duration_s=duration_s,
        distance_m=distance_m,
        avg_speed_mps=(
            round(totals["avg_speed"], 3) if totals["avg_speed"] is not None else None
        ),
        max_speed_mps=(
            round(totals["max_speed"], 3) if totals["max_speed"] is not None else None
        ),
        avg_hr=(
            float(int(totals["avg_heart_rate"]))
            if totals["avg_heart_rate"] is not None
            else None
        ),
        max_hr=int(totals["max_heart_rate"]) if totals["max_heart_rate"] is not None else None,
        avg_power=(
            float(int(totals["avg_power"])) if totals["avg_power"] is not None else None
        ),
        max_power=int(totals["max_power"]) if totals["max_power"] is not None else None,
        avg_spm=(
            float(int(totals["avg_cadence"]))
            if totals["avg_cadence"] is not None
            else None
        ),
        max_spm=(
            int(totals["max_cadence"])
            if totals["max_cadence"] is not None
            else None
        ),
        zone_seconds=zone_seconds,
        laps=laps,
        # These are the active sensor records emitted to FIT. The visualization's
        # first record is time zero; FIT START and its GPS-only anchor may precede
        # it by that record's sample window, but plotting the end timestamp at
        # +1 s created a false empty tail. Later wall-clock gaps are retained.
        series_t=[(timestamps[i] - chart_start_ms) / 1000.0 for i in moving_indices],
        series_speed=[series_speed[i] for i in moving_indices],
        series_hr=[series_hr[i] for i in moving_indices],
        series_power=[series_power[i] for i in moving_indices],
        series_spm=[series_spm[i] for i in moving_indices],
        series_pull=[series_pull[i] for i in moving_indices],
        series_distance=[
            finalized.record_cumulative_distance_m.get(i) for i in moving_indices
        ],
        series_breaks=[i in chart_breaks for i in moving_indices],
        series_steps=[
            (
                max((lap.start_time_ms - chart_start_ms) / 1000.0, 0.0),
                max((lap.end_time_ms - chart_start_ms) / 1000.0, 0.0),
                str(lap.label or lap.kind or "Step"),
                lap.kind,
            )
            for lap in finalized_laps
            if lap.label or lap.kind
        ],
    )
