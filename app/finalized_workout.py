"""Single finalized timeline shared by FIT export and workout summaries."""

from dataclasses import dataclass, replace
from math import floor, isfinite
from typing import Any

from .active_distance import ActiveDistanceAccumulator
from .segmentation import (
    MEANINGFUL_PAUSE_SECONDS,
    program_step_bounds,
    resolve_segments,
)
from .table import Table


@dataclass(frozen=True)
class ActivitySegment:
    """One exact contiguous run of active ergometer records."""

    start_idx: int
    end_idx: int
    start_time_ms: int
    end_time_ms: int
    timer_time_ms: int
    record_indices: tuple[int, ...]

@dataclass(frozen=True)
class FinalizedLap:
    """One immutable reporting/FIT lap over zero or more active records."""

    start_idx: int
    end_idx: int
    start_time_ms: int
    end_time_ms: int
    timer_time_ms: int
    record_indices: tuple[int, ...]
    record_weights_s: tuple[float, ...]
    distance_m: float
    kind: str | None = None
    label: str | None = None
    program_step_index: int | None = None

@dataclass(frozen=True)
class FinalizedWorkout:
    """The authoritative record/segment domain of a processed workout."""

    timestamps_ms: list[int]
    timeline_start_time_ms: int
    timeline_end_time_ms: int
    record_windows_ms: dict[int, tuple[int, int]]
    # Exact contiguous runs of active FIT records. These own timer events.
    segments: tuple[ActivitySegment, ...]
    # Presentation/FIT lap groups. A lap can contain multiple active runs.
    laps: tuple[FinalizedLap, ...]
    # Row indices where chart lines restart (meaningful free-lap boundaries or
    # every exact active run for planned workouts).
    chart_break_indices: tuple[int, ...]
    moving_indices: tuple[int, ...]
    # Active-only cumulative distance written to each FIT record. Missing raw
    # readings stay None so exporters do not invent a measurement.
    record_cumulative_distance_m: dict[int, float | None]
    # Active distance attributed to each represented record window. A missing
    # value means the trailing odometer span never received a closing reading.
    record_distance_m: dict[int, float | None]
    record_duration_s: dict[int, float]
    total_timer_time_ms: int
    total_elapsed_time_ms: int
    total_distance_m: float

    @property
    def start_time_ms(self) -> int | None:
        """Timestamp of the first FIT timer START event."""
        return self.segments[0].start_time_ms if self.segments else None

    @property
    def end_time_ms(self) -> int | None:
        """Timestamp of the final active FIT record/STOP event."""
        return self.segments[-1].end_time_ms if self.segments else None

    @property
    def total_pause_time_ms(self) -> int:
        """Wall-clock time inside the session that was not active timer time."""
        return max(self.total_elapsed_time_ms - self.total_timer_time_ms, 0)


def _build_activity_domain(
    record_windows_ms: dict[int, tuple[int, int]],
    resolved: list[dict[str, Any]],
) -> tuple[
    list[ActivitySegment],
    list[int],
    dict[int, int],
]:
    """Materialize exact active runs from canonical represented intervals."""
    segments: list[ActivitySegment] = []
    record_timer_ms: dict[int, int] = {}
    for raw_segment in resolved:
        indices = tuple(int(index) for index in raw_segment["record_indices"])
        represented_windows_ms: list[int] = []
        for index in indices:
            window_start_ms, window_end_ms = record_windows_ms[index]
            represented_ms = window_end_ms - window_start_ms
            if represented_ms <= 0:
                raise ValueError(
                    f"Workout is not v1: elapsed interval must be positive at row {index}"
                )
            represented_windows_ms.append(represented_ms)
            record_timer_ms[index] = represented_ms
        first_index = indices[0]
        last_index = indices[-1]
        adjusted = ActivitySegment(
            start_idx=first_index,
            end_idx=last_index,
            start_time_ms=record_windows_ms[first_index][0],
            end_time_ms=record_windows_ms[last_index][1],
            timer_time_ms=sum(represented_windows_ms),
            record_indices=indices,
        )
        if adjusted.timer_time_ms > adjusted.end_time_ms - adjusted.start_time_ms:
            raise ValueError("Workout is not v1: active timer exceeds elapsed time")
        segments.append(adjusted)
    moving_indices = [
        index for segment in segments for index in segment.record_indices
    ]
    return segments, moving_indices, record_timer_ms


def _build_record_timeline(
    table: Table, raw_timestamps_ms: list[int]
) -> tuple[list[int], dict[int, tuple[int, int]], int, int]:
    """Validate and materialize the canonical CSV timing fields."""
    elapsed_values = table.get_column_or_none("session_elapsed")
    sample_durations = table.get_column_or_none("sample_duration")
    if elapsed_values is None or sample_durations is None:
        raise ValueError(
            "Workout is not v1: session_elapsed and sample_duration are required"
        )
    if len(elapsed_values) != len(raw_timestamps_ms):
        raise ValueError("Workout is not v1: incomplete canonical timeline")

    elapsed_ms: list[int] = []
    windows: dict[int, tuple[int, int]] = {}
    origin_ms: int | None = None
    previous_endpoint_ms = 0
    for index, (timestamp, elapsed_value, duration_value) in enumerate(
        zip(raw_timestamps_ms, elapsed_values, sample_durations, strict=True)
    ):
        try:
            endpoint_ms = round(float(elapsed_value) * 1000)
            represented_ms = round(float(duration_value) * 1000)
        except (OverflowError, TypeError, ValueError) as exc:
            raise ValueError(
                f"Workout is not v1: invalid canonical timing at row {index}"
            ) from exc
        if endpoint_ms < 0 or represented_ms < 0 or represented_ms > endpoint_ms:
            raise ValueError(
                f"Workout is not v1: invalid canonical timing at row {index}"
            )
        if index and endpoint_ms < elapsed_ms[-1]:
            raise ValueError(
                f"Workout is not v1: session_elapsed must not decrease at row {index}"
            )
        if origin_ms is None:
            origin_ms = timestamp - endpoint_ms
        if timestamp != origin_ms + endpoint_ms:
            raise ValueError(
                f"Workout is not v1: timestamp disagrees with session_elapsed at row {index}"
            )
        start_ms = timestamp - represented_ms
        previous_timestamp_ms = origin_ms + previous_endpoint_ms
        if index and start_ms < previous_timestamp_ms:
            raise ValueError(
                f"Workout is not v1: sample windows overlap at row {index}"
            )
        windows[index] = (start_ms, timestamp)
        elapsed_ms.append(endpoint_ms)
        previous_endpoint_ms = endpoint_ms
    assert origin_ms is not None
    return raw_timestamps_ms, windows, origin_ms, origin_ms + elapsed_ms[-1]


def _calculate_active_distance(
    distances: list[Any],
    moving_indices: list[int],
    record_timer_ms: dict[int, int],
) -> tuple[dict[int, float | None], dict[int, float | None], float]:
    """Attribute cumulative-distance deltas only to active record windows.

    Each active interval owns the raw odometer advance since the preceding
    record. Inactive rows advance only the baseline. No speed-based cap rewrites
    a recorded distance jump.
    """
    record_distance_m: dict[int, float | None] = {}
    cumulative: dict[int, float | None] = {}
    moving_set = set(moving_indices)
    accumulator = ActiveDistanceAccumulator()
    pending_indices: list[int] = []
    for index, value in enumerate(distances):
        active = index in moving_set
        if active:
            pending_indices.append(index)
        update = accumulator.update(
            distance_m=value,
            duration_s=record_timer_ms.get(index, 0) / 1000.0,
            active=active,
        )
        if not active:
            for pending_index in pending_indices:
                record_distance_m[pending_index] = None
                cumulative[pending_index] = None
            pending_indices = []
            continue
        try:
            has_distance = isfinite(float(value)) and float(value) >= 0
        except (TypeError, ValueError):
            has_distance = False
        if not has_distance:
            cumulative[index] = None
        if update is None:
            continue

        remaining_distance_m = update.distance_m
        remaining_duration_s = update.duration_s
        for position, pending_index in enumerate(pending_indices):
            represented_s = record_timer_ms.get(pending_index, 0) / 1000.0
            if position == len(pending_indices) - 1 or remaining_duration_s <= 0:
                attributed_m = remaining_distance_m
            else:
                attributed_m = update.distance_m * represented_s / update.duration_s
            record_distance_m[pending_index] = attributed_m
            remaining_distance_m -= attributed_m
            remaining_duration_s -= represented_s
        cumulative[index] = round(update.cumulative_distance_m, 2)
        pending_indices = []

    for pending_index in pending_indices:
        record_distance_m[pending_index] = None
        cumulative[pending_index] = None
    return record_distance_m, cumulative, round(accumulator.active_distance_m, 2)


def _build_planned_laps(
    program_steps: list[dict[str, Any]] | None,
    moving_indices: list[int],
    record_windows_ms: dict[int, tuple[int, int]],
    record_timer_ms: dict[int, int],
    record_distance_m: dict[int, float | None],
    timeline_start_ms: int,
    timeline_end_ms: int,
) -> list[FinalizedLap]:
    """Intersect active sample windows with the performed program timeline."""
    if program_steps is None:
        return []
    previous_step_end_ms = timeline_start_ms
    moving_cursor = 0
    laps: list[FinalizedLap] = []
    for ordinal, step in enumerate(program_steps):
        bounds = program_step_bounds(step)
        assert bounds is not None
        start_seconds, end_seconds = bounds
        step_start_ms = timeline_start_ms + round(start_seconds * 1000)
        step_end_ms = timeline_start_ms + round(end_seconds * 1000)
        if step_start_ms != previous_step_end_ms or step_end_ms > timeline_end_ms:
            raise ValueError("Planned v1 workout has a non-consecutive timeline")
        previous_step_end_ms = step_end_ms
        record_indices: list[int] = []
        record_weights_s: list[float] = []
        distance_m = 0.0
        while moving_cursor < len(moving_indices):
            candidate = moving_indices[moving_cursor]
            if record_windows_ms[candidate][1] > step_start_ms:
                break
            moving_cursor += 1
        record_cursor = moving_cursor
        while record_cursor < len(moving_indices):
            index = moving_indices[record_cursor]
            record_start_ms, record_end_ms = record_windows_ms[index]
            if record_start_ms >= step_end_ms:
                break
            duration_ms = record_timer_ms.get(index, 0)
            overlap_ms = max(
                min(record_end_ms, step_end_ms) - max(record_start_ms, step_start_ms),
                0,
            )
            if duration_ms > 0 and overlap_ms > 0:
                record_indices.append(index)
                record_weights_s.append(overlap_ms / 1000.0)
                distance_m += (record_distance_m.get(index) or 0.0) * (
                    overlap_ms / duration_ms
                )
            record_cursor += 1
        laps.append(FinalizedLap(
            start_idx=record_indices[0] if record_indices else 0,
            end_idx=record_indices[-1] if record_indices else -1,
            start_time_ms=step_start_ms,
            end_time_ms=step_end_ms,
            timer_time_ms=round(sum(record_weights_s) * 1000),
            record_indices=tuple(record_indices),
            record_weights_s=tuple(record_weights_s),
            distance_m=distance_m,
            kind=step.get("type"),
            label=step.get("label"),
            program_step_index=step.get("index", ordinal),
        ))
    return laps


def _group_free_segments(
    segments: list[ActivitySegment],
) -> list[list[ActivitySegment]]:
    """Group exact runs by the shared meaningful-pause boundary."""
    groups: list[list[ActivitySegment]] = []
    pause_threshold_ms = round(MEANINGFUL_PAUSE_SECONDS * 1000)
    for segment in segments:
        if not groups:
            groups.append([segment])
            continue
        previous = groups[-1][-1]
        inactive_ms = segment.start_time_ms - previous.end_time_ms
        if inactive_ms < pause_threshold_ms:
            groups[-1].append(segment)
        else:
            groups.append([segment])
    return groups


def _build_free_laps(
    segments: list[ActivitySegment],
    record_timer_ms: dict[int, int],
    record_distance_m: dict[int, float | None],
) -> list[FinalizedLap]:
    """Group exact runs separated by less than one meaningful free pause."""
    groups = _group_free_segments(segments)

    laps: list[FinalizedLap] = []
    for group in groups:
        first, last = group[0], group[-1]
        indices = tuple(
            index for segment in group for index in segment.record_indices
        )
        laps.append(FinalizedLap(
            start_idx=first.start_idx,
            end_idx=last.end_idx,
            start_time_ms=first.start_time_ms,
            end_time_ms=last.end_time_ms,
            timer_time_ms=sum(segment.timer_time_ms for segment in group),
            record_indices=indices,
            record_weights_s=tuple(
                record_timer_ms.get(index, 0) / 1000.0 for index in indices
            ),
            distance_m=sum((record_distance_m.get(index) or 0.0) for index in indices),
        ))
    return laps


def _chart_breaks(segments: list[ActivitySegment]) -> set[int]:
    """Restart chart lines only at meaningful pauses, for planned and free alike.

    A break starts a new summary polyline. Both workout modes group exact runs
    separated by less than the fixed five-second meaningful-pause delay
    (``_group_free_segments``). A brief cleared activity flag between strokes or
    a sub-threshold uncovered gap therefore does not fragment one continuous
    effort into disconnected segments. The latter is common at high cadence,
    when the ergometer reports a one-second window across a two-second elapsed
    advance and leaves one second uncovered inside otherwise unbroken paddling.
    Exact runs, timer time, distance, and laps are unaffected; only line
    continuity changes. Planned laps still come from performed steps and render
    as chart bands rather than line breaks (D019).
    """
    return {group[0].start_idx for group in _group_free_segments(segments)}


def _conserve_lap_totals(
    laps: list[FinalizedLap], timer_ms: int, total_distance: float, planned: bool
) -> list[FinalizedLap]:
    """Validate lap conservation and apportion centimetre rounding."""
    lap_timer_ms = sum(lap.timer_time_ms for lap in laps)
    lap_distance_m = sum(lap.distance_m for lap in laps)
    if lap_timer_ms != timer_ms:
        label = "Planned timeline" if planned else "Free-workout laps"
        raise ValueError(f"{label} do not cover the complete active timer domain")
    if abs(lap_distance_m - total_distance) > 0.011:
        label = "Planned timeline" if planned else "Free-workout laps"
        raise ValueError(f"{label} do not cover the complete active distance")
    if not laps:
        return []
    exact_cents = [max(lap.distance_m, 0.0) * 100 for lap in laps]
    lap_cents = [floor(value) for value in exact_cents]
    remaining = round(total_distance * 100) - sum(lap_cents)
    remainders = sorted(
        range(len(laps)),
        key=lambda index: exact_cents[index] - lap_cents[index],
        reverse=True,
    )
    for index in remainders[:remaining]:
        lap_cents[index] += 1
    return [
        replace(lap, distance_m=cents / 100.0)
        for lap, cents in zip(laps, lap_cents, strict=True)
    ]


def finalize_workout(
    table: Table,
    *,
    program_steps: list[dict[str, Any]] | None = None,
    logger: Any | None = None,
) -> FinalizedWorkout:
    """Resolve the exact records, segments, timing and distance written to FIT."""
    if program_steps is not None:
        if not program_steps:
            raise ValueError("Planned v1 workout requires a non-empty step timeline")
        previous_end = 0.0
        for ordinal, step in enumerate(program_steps):
            bounds = program_step_bounds(step)
            if (
                bounds is None
                or step.get("index") != ordinal
                or bounds[0] != previous_end
            ):
                raise ValueError(
                    f"Planned v1 workout has an invalid step at index {ordinal}"
                )
            previous_end = bounds[1]
    try:
        raw_timestamps = []
        for value in table.get_column("timestamp"):
            timestamp = int(value)
            if isinstance(value, bool) or float(value) != timestamp:
                raise ValueError
            raw_timestamps.append(timestamp)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError("Workout is not v1: every timestamp must be an integer") from exc
    if not raw_timestamps:
        raise ValueError("Workout contains no records")
    timestamps, record_windows_ms, timeline_start_ms, timeline_end_ms = (
        _build_record_timeline(table, raw_timestamps)
    )
    if program_steps is not None:
        _, final_end_s = program_step_bounds(program_steps[-1]) or (0.0, -1.0)
        timeline_elapsed_s = (timeline_end_ms - timeline_start_ms) / 1000.0
        if round(final_end_s * 1000) != round(timeline_elapsed_s * 1000):
            raise ValueError("Planned v1 workout must cover the complete session timeline")
    # The activity record/timer domain is independent of a workout plan. A
    # plan groups those records later; it must never add/remove sensor samples.
    active_values = table.get_column_or_none("active")
    if active_values is None:
        raise ValueError("Workout is not v1: active_paddling is required for every row")
    resolved = resolve_segments(
        record_windows_ms=record_windows_ms,
        active_col=active_values,
        logger=logger,
    )
    segments, moving_indices, record_timer_ms = _build_activity_domain(
        record_windows_ms, resolved
    )
    distances = table.get_column("distance")
    record_distance_m, active_cumulative_distance_m, total_distance = (
        _calculate_active_distance(
            distances,
            moving_indices,
            record_timer_ms,
        )
    )
    laps = _build_planned_laps(
        program_steps, moving_indices, record_windows_ms,
        record_timer_ms, record_distance_m, timeline_start_ms, timeline_end_ms,
    )
    planned_laps = program_steps is not None
    if not planned_laps:
        laps = _build_free_laps(
            segments, record_timer_ms, record_distance_m
        )
    chart_breaks = _chart_breaks(segments)
    timer_ms = sum(segment.timer_time_ms for segment in segments)
    elapsed_ms = timeline_end_ms - timeline_start_ms
    laps = _conserve_lap_totals(laps, timer_ms, total_distance, planned_laps)
    if timer_ms > elapsed_ms:
        raise ValueError("Workout is not v1: total timer time exceeds elapsed time")
    if any(
        lap.timer_time_ms > lap.end_time_ms - lap.start_time_ms for lap in laps
    ):
        raise ValueError("Workout is not v1: lap timer time exceeds elapsed time")
    return FinalizedWorkout(
        timestamps_ms=timestamps,
        timeline_start_time_ms=timeline_start_ms,
        timeline_end_time_ms=timeline_end_ms,
        record_windows_ms=record_windows_ms,
        segments=tuple(segments),
        laps=tuple(laps),
        chart_break_indices=tuple(sorted(chart_breaks)),
        moving_indices=tuple(moving_indices),
        record_cumulative_distance_m=active_cumulative_distance_m,
        record_distance_m=record_distance_m,
        record_duration_s={
            index: duration_ms / 1000.0
            for index, duration_ms in record_timer_ms.items()
        },
        total_timer_time_ms=timer_ms,
        total_elapsed_time_ms=elapsed_ms,
        total_distance_m=total_distance,
    )
