"""Post-workout summary computation.

Turns a recorded workout (the Table produced by :class:`app.read_csv.CsvReader`)
into the numbers the summary screen shows: totals and averages, per-lap splits,
time in each heart-rate zone, and recorded sample series for the charts.

Pure computation with no GUI dependencies, so it is unit-testable and reusable
(e.g. by a future trends view). Records, timer runs and lap groups come from the
same finalized workout model consumed by the FIT exporter, so the splits always
match the laps written to the FIT file, program-driven or freestyle alike. Time
in HR zones is accumulated over those same moving segments via
:func:`app.stats.time_in_hr_zones`; missing readings remain uncovered time so
the UI can show HR-signal coverage honestly.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .coerce import to_float
from .finalized_workout import finalize_workout
from .logger import Logger
from .power import DEFAULT_PULL_LENGTH_M, estimate_power, normalize_pull_length
from .speed_series import recorded_speed_mps
from .stats import (
    time_in_hr_zones,
    zone_lower_bounds,
)
from .table import Table
from .workout_metrics import SliceMetrics, compute_finalized_metrics

logger = Logger.get_logger(name=__name__)


@dataclass
class LapSummary:
    """Aggregates for one moving segment (one FIT lap)."""

    index: int  # 1-based lap number
    start_s: float  # offset from the canonical recording start, seconds
    active_time_s: float
    elapsed_time_s: float
    pause_after_s: float
    distance_m: float
    avg_speed_mps: float | None
    max_speed_mps: float | None
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

    @property
    def pause_time_s(self) -> float:
        """Inactive time inside this lap, excluding the following gap."""
        return max(self.elapsed_time_s - self.active_time_s, 0.0)


@dataclass
class WorkoutSummary:
    """Everything the summary screen needs, computed once from the CSV."""

    start_time: datetime | None
    active_time_s: float
    elapsed_time_s: float
    pause_time_s: float
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
    # ``series_t`` is seconds since recording elapsed zero; the value
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
    # chart uses these boundaries to break lines without inventing non-FIT
    # records solely as visual sentinels.
    series_breaks: list[bool] = field(default_factory=list)
    # Planned step bands on the chart's canonical session-elapsed time axis.
    # seconds, label and step kind. Empty for free workouts.
    series_steps: list[tuple[float, float, str, str | None]] = field(
        default_factory=list
    )
    series_time_bounds: tuple[float, float] = (0.0, 0.0)

def _lap_summary(
    index: int,
    start_time_ms: int,
    end_time_ms: int,
    timer_time_ms: int,
    distance_m: float,
    t0_ms: int,
    metrics: SliceMetrics,
    pause_after_ms: int = 0,
    kind: str | None = None,
    label: str | None = None,
) -> LapSummary:
    active_time_s = timer_time_ms / 1000.0
    elapsed_time_s = max((end_time_ms - start_time_ms) / 1000.0, 0.0)
    return LapSummary(
        index=index,
        start_s=(start_time_ms - t0_ms) / 1000.0,
        active_time_s=active_time_s,
        elapsed_time_s=elapsed_time_s,
        pause_after_s=max(pause_after_ms / 1000.0, 0.0),
        distance_m=distance_m,
        avg_speed_mps=(
            round(metrics.avg_speed, 3)
            if metrics.avg_speed is not None
            else None
        ),
        max_speed_mps=(
            round(metrics.max_speed, 3)
            if metrics.max_speed is not None
            else None
        ),
        avg_hr=(
            float(round(metrics.avg_heart_rate))
            if metrics.avg_heart_rate is not None
            else None
        ),
        max_hr=(round(metrics.max_heart_rate) if metrics.max_heart_rate is not None else None),
        avg_power=(
            float(round(metrics.avg_power)) if metrics.avg_power is not None else None
        ),
        max_power=(round(metrics.max_power) if metrics.max_power is not None else None),
        avg_spm=(
            float(round(metrics.avg_cadence))
            if metrics.avg_cadence is not None
            else None
        ),
        max_spm=(round(metrics.max_cadence) if metrics.max_cadence is not None else None),
        kind=kind,
        label=label,
    )


def compute_summary(
    table: Table,
    max_hr: Any = 185,
    zones: list[int] | None = None,
    program_steps: list[dict[str, Any]] | None = None,
    pull_length_m: Any = DEFAULT_PULL_LENGTH_M,
) -> WorkoutSummary:
    """Compute a full workout summary from a workout Table.

    Args:
        table: Output of ``CsvReader.read_all`` (including timestamp, heart
            rate, averaged/instant cadence and pull force, distance, and
            instantaneous speed).
        max_hr: Max heart rate used to derive zone bounds (when ``zones`` is None).
        zones: Optional five ascending manual zone lower bounds (bpm).
        program_steps: Optional recorded program step timeline (the
            ``steps.json`` timeline; see ``segmentation.load_program_steps``).
            When present, laps follow the program steps exactly as the FIT
            exporter's do; otherwise laps come from movement detection.
        pull_length_m: Recorded effective pull length used for estimated power.

    Raises:
        ValueError: If the table is empty.
    """
    if table.is_empty():
        raise ValueError("Workout contains no records.")

    finalized = finalize_workout(table, program_steps=program_steps, logger=logger)
    if not finalized.segments:
        raise ValueError("Workout contains no active ergometer samples.")
    pull_length_m = normalize_pull_length(pull_length_m)
    hrs = [to_float(v) for v in table.get_column("heart_rate")]
    power_cadences = [to_float(v) for v in table.get_column("cadence")]
    cadences = [to_float(v) for v in table.get_column("cadence_instant")]
    power_forces = [to_float(v) for v in table.get_column("pull_force")]
    forces = [to_float(v) for v in table.get_column("pull_force_instant")]

    # ---- chart series ------------------------------------------------------
    # Charts, lap/session aggregates and FIT records retain instantaneous
    # sensor points. Power is the calibrated exception: each point is derived
    # from stroke-averaged cadence and pull force. The renderer displays these
    # raw points without applying a moving average.
    series_speed_by_index = recorded_speed_mps(table, finalized)
    series_speed = [series_speed_by_index.get(i) for i in range(table.height)]
    series_hr = [h if h is not None and h > 0 else None for h in hrs]
    series_power = [
        (lambda p: float(p) if p is not None and p > 0 else None)(
            estimate_power(f, c, pull_length_m)
        )
        for f, c in zip(power_forces, power_cadences, strict=False)
    ]
    series_spm = [c if c is not None and c > 0 else None for c in cadences]
    series_pull = [f if f is not None and f > 0 else None for f in forces]

    # ---- laps (identical to the FIT export, by construction) ----------------
    # The single shared finalized model: program-step groups when a timeline
    # exists, or one lap per exact activity run otherwise.
    segments = finalized.segments
    finalized_laps = finalized.laps
    finalized_metrics = compute_finalized_metrics(
        table, finalized, pull_length_m=pull_length_m
    )
    summary_start_ms = finalized.timeline_start_time_ms
    laps = []
    for i, (lap, metrics) in enumerate(
        zip(finalized_laps, finalized_metrics.laps, strict=True)
    ):
        next_start_ms = (
            finalized_laps[i + 1].start_time_ms
            if i + 1 < len(finalized_laps)
            else finalized.timeline_end_time_ms
        )
        laps.append(
            _lap_summary(
                i + 1,
                lap.start_time_ms,
                lap.end_time_ms,
                lap.timer_time_ms,
                lap.distance_m,
                summary_start_ms,
                metrics,
                pause_after_ms=max((next_start_ms or lap.end_time_ms) - lap.end_time_ms, 0),
                kind=lap.kind,
                label=lap.label,
            )
        )

    # ---- totals --------------------------------------------------------------
    active_time_s = finalized.total_timer_time_ms / 1000.0
    elapsed_time_s = finalized.total_elapsed_time_ms / 1000.0
    pause_time_s = finalized.total_pause_time_ms / 1000.0
    distance_m = finalized.total_distance_m
    moving_indices = finalized.moving_indices
    chart_breaks = set(finalized.chart_break_indices)
    chart_start_ms = finalized.timeline_start_time_ms
    # Overall averages/peaks come from the finalized aggregate shared verbatim
    # with FIT session/lap messages.
    #
    # avg_power is the represented-time average of per-sample power estimates (mean of
    # products). The machine display instead shows L x mean(force) x
    # mean(cadence) (product of means), which reads a few watts higher when
    # force and cadence co-vary — a small difference vs the display is
    # expected. max_power is the highest recorded-sample estimate; the display's
    # within-stroke peak is not available in the recorded channel.
    totals = finalized_metrics.session

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
        active_time_s=active_time_s,
        elapsed_time_s=elapsed_time_s,
        pause_time_s=pause_time_s,
        distance_m=distance_m,
        avg_speed_mps=(
            round(totals.avg_speed, 3) if totals.avg_speed is not None else None
        ),
        max_speed_mps=(
            round(totals.max_speed, 3) if totals.max_speed is not None else None
        ),
        avg_hr=(
            float(round(totals.avg_heart_rate))
            if totals.avg_heart_rate is not None
            else None
        ),
        max_hr=(round(totals.max_heart_rate) if totals.max_heart_rate is not None else None),
        avg_power=(
            float(round(totals.avg_power)) if totals.avg_power is not None else None
        ),
        max_power=(round(totals.max_power) if totals.max_power is not None else None),
        avg_spm=(
            float(round(totals.avg_cadence))
            if totals.avg_cadence is not None
            else None
        ),
        max_spm=(
            round(totals.max_cadence)
            if totals.max_cadence is not None
            else None
        ),
        zone_seconds=zone_seconds,
        laps=laps,
        # Place each value at the exact CSV/FIT sample endpoint so a hover time
        # maps one-to-one to the exported sensor record. Explicit bounds retain
        # the complete elapsed timeline around those points.
        series_t=[
            (finalized.timestamps_ms[i] - chart_start_ms) / 1000.0
            for i in moving_indices
        ],
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
        series_time_bounds=(0.0, elapsed_time_s),
    )
