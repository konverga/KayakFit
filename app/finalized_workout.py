"""Single finalized timeline shared by FIT export and workout summaries."""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from itertools import pairwise
from math import floor, isfinite
from typing import Any

from .segmentation import (
    DEFAULT_MAX_GAP_SECONDS,
    DEFAULT_STATIONARY_THRESHOLD_RECORDS,
    program_step_bounds,
    resolve_segments,
)
from .table import Table


@dataclass(frozen=True)
class ActivitySegment(Mapping[str, Any]):
    """One exact contiguous run of active ergometer records."""

    start_idx: int
    end_idx: int
    start_time_ms: int
    end_time_ms: int
    timer_time_ms: int

    def __getitem__(self, key: str) -> Any:
        if key not in (
            "start_idx", "end_idx", "start_time_ms", "end_time_ms", "timer_time_ms"
        ):
            raise KeyError(key)
        return int(getattr(self, key))

    def __iter__(self) -> Iterator[str]:
        return iter(("start_idx", "end_idx", "start_time_ms", "end_time_ms", "timer_time_ms"))

    def __len__(self) -> int:
        return 5


@dataclass(frozen=True)
class FinalizedLap(Mapping[str, Any]):
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

    def __getitem__(self, key: str) -> Any:
        if key not in (
            "start_idx", "end_idx", "start_time_ms", "end_time_ms",
            "timer_time_ms", "record_indices", "record_weights_s",
            "distance_m", "kind", "label", "program_step_index",
        ):
            raise KeyError(key)
        return getattr(self, key)

    def __iter__(self) -> Iterator[str]:
        return iter(
            (
                "start_idx", "end_idx", "start_time_ms", "end_time_ms",
                "timer_time_ms", "record_indices", "record_weights_s",
                "distance_m", "kind", "label", "program_step_index",
            )
        )

    def __len__(self) -> int:
        return 11


@dataclass(frozen=True)
class FinalizedWorkout:
    """The authoritative record/segment domain of a processed workout."""

    timestamps_ms: list[int]
    # Exact contiguous runs of active FIT records. These own timer events.
    segments: tuple[ActivitySegment, ...]
    # Presentation/FIT lap groups. A lap can contain multiple active runs.
    laps: tuple[FinalizedLap, ...]
    # Row indices where chart lines/smoothing restart (lap or meaningful pause).
    chart_break_indices: tuple[int, ...]
    moving_indices: tuple[int, ...]
    # Active-only cumulative distance written to each FIT record. Missing raw
    # readings stay None so exporters do not invent a measurement.
    record_cumulative_distance_m: dict[int, float | None]
    record_duration_s: dict[int, float]
    total_timer_time_ms: int
    total_elapsed_time_ms: int
    total_distance_m: float

    @property
    def start_time_ms(self) -> int | None:
        """Timestamp of the first FIT timer START event."""
        return self.segments[0]["start_time_ms"] if self.segments else None

    @property
    def end_time_ms(self) -> int | None:
        """Timestamp of the final active FIT record/STOP event."""
        return self.segments[-1]["end_time_ms"] if self.segments else None


def _build_activity_domain(
    table: Table,
    timestamps: list[int],
    resolved: list[dict[str, Any]],
) -> tuple[
    list[ActivitySegment],
    list[int],
    dict[int, int],
    dict[int, tuple[int, int]],
]:
    """Validate sample windows and materialize exact active record runs."""
    sample_durations = table.get_column_or_none("sample_duration")
    if sample_durations is None:
        raise ValueError("Workout is not v1: sample_duration is required")
    segments: list[ActivitySegment] = []
    record_timer_ms: dict[int, int] = {}
    record_windows_ms: dict[int, tuple[int, int]] = {}
    for raw_segment in resolved:
        represented_windows_ms: list[int] = []
        previous_window_end_ms: int | None = None
        for index in range(raw_segment["start_idx"], raw_segment["end_idx"] + 1):
            try:
                represented_ms = round(float(sample_durations[index]) * 1000)
            except (IndexError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"Workout is not v1: invalid sample_duration at row {index}"
                ) from exc
            if represented_ms <= 0:
                raise ValueError(
                    f"Workout is not v1: sample_duration must be positive at row {index}"
                )
            window_end_ms = timestamps[index]
            window_start_ms = window_end_ms - represented_ms
            if (
                previous_window_end_ms is not None
                and window_start_ms < previous_window_end_ms
            ):
                raise ValueError(
                    "Workout is not v1: sample windows overlap at "
                    f"row {index} ({window_start_ms} < {previous_window_end_ms})"
                )
            represented_windows_ms.append(represented_ms)
            record_timer_ms[index] = represented_ms
            record_windows_ms[index] = (window_start_ms, window_end_ms)
            previous_window_end_ms = window_end_ms
        first_index = int(raw_segment["start_idx"])
        last_index = int(raw_segment["end_idx"])
        adjusted = ActivitySegment(
            start_idx=first_index,
            end_idx=last_index,
            start_time_ms=record_windows_ms[first_index][0],
            end_time_ms=record_windows_ms[last_index][1],
            timer_time_ms=sum(represented_windows_ms),
        )
        if adjusted.timer_time_ms > adjusted.end_time_ms - adjusted.start_time_ms:
            raise ValueError("Workout is not v1: active timer exceeds elapsed time")
        segments.append(adjusted)
    moving_indices = [
        index
        for segment in segments
        for index in range(segment.start_idx, segment.end_idx + 1)
    ]
    return segments, moving_indices, record_timer_ms, record_windows_ms


def _calculate_active_distance(
    distances: list[Any],
    speeds: list[Any],
    moving_indices: list[int],
    resume_indices: set[int],
    record_timer_ms: dict[int, int],
) -> tuple[dict[int, float], dict[int, float | None], float]:
    """Attribute cumulative-distance deltas only to active record windows.

    The ergometer can publish a coarse cumulative-distance jump on the first
    packet after a pause. Only the distance represented by that resumed active
    window belongs to the new segment; the remainder accumulated while the
    activity flag was inactive must not create an impossible split speed.
    """
    record_distance_m: dict[int, float] = {}
    moving_set = set(moving_indices)
    previous_valid_distance = 0.0
    for index, value in enumerate(distances):
        try:
            current = float(value)
        except (TypeError, ValueError):
            if index in moving_set:
                record_distance_m[index] = 0.0
            continue
        if index in moving_set:
            distance_delta = max(current - previous_valid_distance, 0.0)
            if index in resume_indices:
                try:
                    reported_speed = float(speeds[index])
                except (IndexError, TypeError, ValueError):
                    reported_speed = 0.0
                represented_s = record_timer_ms.get(index, 0) / 1000.0
                if not isfinite(reported_speed) or reported_speed < 0:
                    reported_speed = 0.0
                if represented_s > 0:
                    distance_delta = min(
                        distance_delta, reported_speed * represented_s
                    )
            record_distance_m[index] = distance_delta
        previous_valid_distance = current

    cumulative: dict[int, float | None] = {}
    total = 0.0
    for index in moving_indices:
        try:
            float(distances[index])
        except (IndexError, TypeError, ValueError):
            cumulative[index] = None
            continue
        total += record_distance_m.get(index, 0.0)
        cumulative[index] = round(total, 2)
    return record_distance_m, cumulative, round(sum(record_distance_m.values()), 2)


def _build_planned_laps(
    segments: list[ActivitySegment],
    program_steps: list[dict[str, Any]] | None,
    moving_indices: list[int],
    record_windows_ms: dict[int, tuple[int, int]],
    record_timer_ms: dict[int, int],
    record_distance_m: dict[int, float],
) -> list[FinalizedLap]:
    """Intersect active sample windows with the performed program timeline."""
    if not segments or program_steps is None:
        return []
    activity_start_ms = segments[0].start_time_ms
    activity_end_ms = segments[-1].end_time_ms
    previous_step_end_ms = activity_start_ms
    moving_cursor = 0
    laps: list[FinalizedLap] = []
    for ordinal, step in enumerate(program_steps):
        bounds = program_step_bounds(step)
        assert bounds is not None
        start_value, end_value = bounds
        step_start_ms = max(round(start_value), activity_start_ms, previous_step_end_ms)
        # A naturally completed plan is only observed when its final sensor
        # packet arrives. The terminal step therefore owns the complete captured
        # activity tail, including the remainder of that sample window and any
        # packet already buffered before polling stops. Interior boundaries stay
        # exact, and conservation remains strict for every non-terminal gap.
        step_end_ms = (
            activity_end_ms
            if ordinal == len(program_steps) - 1
            else min(round(end_value), activity_end_ms)
        )
        if step_end_ms < step_start_ms:
            continue
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
                distance_m += record_distance_m.get(index, 0.0) * (
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


def _build_free_laps(
    segments: list[ActivitySegment],
    threshold: int,
    max_gap_ms: float,
    record_timer_ms: dict[int, int],
    record_distance_m: dict[int, float],
) -> list[FinalizedLap]:
    """Group exact activity runs into natural free-workout laps."""
    groups: list[list[ActivitySegment]] = []
    for segment in segments:
        if not groups:
            groups.append([segment])
            continue
        previous = groups[-1][-1]
        inactive_records = segment.start_idx - previous.end_idx - 1
        timestamp_gap = segment.start_time_ms - previous.end_time_ms
        if inactive_records < threshold and (
            max_gap_ms == 0 or timestamp_gap <= max_gap_ms
        ):
            groups[-1].append(segment)
        else:
            groups.append([segment])

    laps: list[FinalizedLap] = []
    for group in groups:
        first, last = group[0], group[-1]
        indices = tuple(
            index
            for segment in group
            for index in range(segment.start_idx, segment.end_idx + 1)
        )
        laps.append(FinalizedLap(
            start_idx=first.start_idx,
            end_idx=last.end_idx,
            start_time_ms=first.start_time_ms,
            end_time_ms=last.end_time_ms,
            timer_time_ms=sum(item.timer_time_ms for item in group),
            record_indices=indices,
            record_weights_s=tuple(
                record_timer_ms.get(index, 0) / 1000.0 for index in indices
            ),
            distance_m=sum(record_distance_m.get(index, 0.0) for index in indices),
        ))
    return laps


def _chart_breaks(
    segments: list[ActivitySegment],
    laps: list[FinalizedLap],
    moving_indices: list[int],
    planned: bool,
    threshold: int,
    max_gap_ms: float,
) -> set[int]:
    """Find chart polyline breaks without changing the FIT timer domain."""
    breaks = (
        {moving_indices[0]}
        if planned and moving_indices
        else {lap.record_indices[0] for lap in laps if lap.record_indices}
    )
    for previous, segment in pairwise(segments):
        inactive_records = segment.start_idx - previous.end_idx - 1
        timestamp_gap = segment.start_time_ms - previous.end_time_ms
        if inactive_records >= threshold or (
            max_gap_ms > 0 and timestamp_gap > max_gap_ms
        ):
            breaks.add(segment.start_idx)
    return breaks


def _conserve_lap_totals(
    laps: list[FinalizedLap], timer_ms: int, total_distance: float, planned: bool
) -> list[FinalizedLap]:
    """Validate planned conservation and apportion centimetre rounding."""
    if planned:
        if sum(lap.timer_time_ms for lap in laps) != timer_ms:
            raise ValueError(
                "Planned v1 timeline does not cover the complete active timer domain"
            )
        if abs(sum(lap.distance_m for lap in laps) - total_distance) > 0.011:
            raise ValueError(
                "Planned v1 timeline does not cover the complete active distance"
            )
    if not laps or sum(lap.timer_time_ms for lap in laps) != timer_ms:
        return [replace(lap, distance_m=round(lap.distance_m, 2)) for lap in laps]
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
    config: Mapping[str, Any] | None = None,
    logger: Any | None = None,
) -> FinalizedWorkout:
    """Resolve the exact records, segments, timing and distance written to FIT."""
    if program_steps is not None:
        if not program_steps:
            raise ValueError("Planned v1 workout requires a non-empty step timeline")
        for ordinal, step in enumerate(program_steps):
            if not isinstance(step, dict) or program_step_bounds(step) is None:
                raise ValueError(
                    f"Planned v1 workout has an invalid step at index {ordinal}"
                )
    try:
        timestamps = [int(value) for value in table.get_column("timestamp")]
    except (TypeError, ValueError) as exc:
        raise ValueError("Workout is not v1: every timestamp must be an integer") from exc
    if any(current <= previous for previous, current in pairwise(timestamps)):
        raise ValueError("Workout is not v1: timestamps must be strictly increasing")
    # The activity record/timer domain is independent of a workout plan. A
    # plan groups those records later; it must never add/remove sensor samples.
    resolved = resolve_segments(
        timestamps_ms=timestamps,
        height=table.height,
        distance_col=table.get_column_or_none("distance"),
        speed_col=table.get_column_or_none("speed"),
        active_col=table.get_column_or_none("active"),
        config=config,
        logger=logger,
    )
    segments, moving_indices, record_timer_ms, record_windows_ms = (
        _build_activity_domain(table, timestamps, resolved)
    )
    distances = table.get_column("distance")
    speeds = table.get_column("speed")
    record_distance_m, active_cumulative_distance_m, total_distance = (
        _calculate_active_distance(
            distances,
            speeds,
            moving_indices,
            {
                segment.start_idx
                for ordinal, segment in enumerate(segments)
                if ordinal > 0 or segment.start_idx > 0
            },
            record_timer_ms,
        )
    )
    cfg: Mapping[str, Any] = config if config is not None else {}
    threshold = max(
        int(
            cfg.get(
                "autopause_stationary_threshold_records",
                DEFAULT_STATIONARY_THRESHOLD_RECORDS,
            )
        ),
        1,
    )
    max_gap_ms = max(
        float(cfg.get("autopause_gap_seconds", DEFAULT_MAX_GAP_SECONDS)), 0.0
    ) * 1000.0
    laps = _build_planned_laps(
        segments, program_steps, moving_indices, record_windows_ms,
        record_timer_ms, record_distance_m,
    )
    planned_laps = bool(laps)
    if not laps:
        laps = _build_free_laps(
            segments, threshold, max_gap_ms, record_timer_ms, record_distance_m
        )
    chart_breaks = _chart_breaks(
        segments, laps, moving_indices, planned_laps, threshold, max_gap_ms
    )
    timer_ms = sum(segment.timer_time_ms for segment in segments)
    elapsed_ms = (
        int(segments[-1].end_time_ms - segments[0].start_time_ms)
        if segments
        else 0
    )
    laps = _conserve_lap_totals(laps, timer_ms, total_distance, planned_laps)
    if timer_ms > elapsed_ms:
        raise ValueError("Workout is not v1: total timer time exceeds elapsed time")
    if any(
        lap.timer_time_ms > lap.end_time_ms - lap.start_time_ms for lap in laps
    ):
        raise ValueError("Workout is not v1: lap timer time exceeds elapsed time")
    return FinalizedWorkout(
        timestamps_ms=timestamps,
        segments=tuple(segments),
        laps=tuple(laps),
        chart_break_indices=tuple(sorted(chart_breaks)),
        moving_indices=tuple(moving_indices),
        record_cumulative_distance_m=active_cumulative_distance_m,
        record_duration_s={
            index: duration_ms / 1000.0
            for index, duration_ms in record_timer_ms.items()
        },
        total_timer_time_ms=timer_ms,
        total_elapsed_time_ms=elapsed_ms,
        total_distance_m=total_distance,
    )
