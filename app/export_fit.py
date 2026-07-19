"""CSV -> Garmin FIT conversion for kayaking activities.

Converts recorded workout data (a Table read from the workout CSV) into a
Garmin FIT activity file via ``fit_tool``. Exact timer runs come from the
recorded activity flag; finalized laps are one per recorded program step for
planned workouts and one per exact active run for free workouts.
Session/lap statistics use the shared finalized metric result, so the exported
numbers always match the in-app summary screen. Each active run
gets proper START/STOP_ALL timer events (moving vs elapsed time) for accurate
Strava calculations. Synthetic GPS positions map the same active-only distance
onto the 2 km Hazewinkel course because Strava uses positions to calculate the
overview moving time for kayaking. A metric-free position at every active-run
start anchors the first represented sample window. Standard sensor metrics are
written: heart rate, cadence, power, distance and speed.
"""

import os
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from fit_tool.fit_file_builder import FitFileBuilder
from fit_tool.profile.messages.activity_message import ActivityMessage
from fit_tool.profile.messages.device_info_message import DeviceInfoMessage
from fit_tool.profile.messages.event_message import EventMessage
from fit_tool.profile.messages.file_id_message import FileIdMessage
from fit_tool.profile.messages.lap_message import LapMessage
from fit_tool.profile.messages.record_message import RecordMessage
from fit_tool.profile.messages.session_message import SessionMessage
from fit_tool.profile.profile_type import (
    Activity,
    Event,
    EventType,
    FileType,
    Manufacturer,
    SessionTrigger,
    Sport,
    SubSport,
    TimerTrigger,
)

from .finalized_workout import ActivitySegment, FinalizedWorkout, finalize_workout
from .logger import Logger
from .power import DEFAULT_PULL_LENGTH_M, estimate_power, normalize_pull_length
from .read_csv import CsvReader, CsvReadError
from .segmentation import load_program_steps
from .speed_series import recorded_speed_mps
from .table import Table
from .workout_metadata import load_workout_metadata
from .workout_metrics import SliceMetrics, compute_finalized_metrics
from .workout_paths import WorkoutPaths

# Strava recalculates a kayaking activity's overview moving time from GPS
# positions instead of reliably honoring the FIT session timer. Map KayakFit's
# authoritative active-only distance onto this deterministic out-and-back
# course so a pause remains stationary in Strava without changing FIT totals.
_GPS_COURSE_START = (51.061847, 4.403456)  # (latitude, longitude) in degrees
_GPS_COURSE_END = (51.069048, 4.376805)
_GPS_COURSE_LENGTH_M = 2000.0


def _synthetic_gps_position(distance_m: float) -> tuple[float, float]:
    """Map cumulative active distance onto the Hazewinkel out-and-back course."""
    cycle_length_m = 2 * _GPS_COURSE_LENGTH_M
    position_in_cycle_m = distance_m % cycle_length_m
    if position_in_cycle_m <= _GPS_COURSE_LENGTH_M:
        progress = position_in_cycle_m / _GPS_COURSE_LENGTH_M
    else:
        progress = (cycle_length_m - position_in_cycle_m) / _GPS_COURSE_LENGTH_M

    start_lat, start_lon = _GPS_COURSE_START
    end_lat, end_lon = _GPS_COURSE_END
    return (
        start_lat + (end_lat - start_lat) * progress,
        start_lon + (end_lon - start_lon) * progress,
    )


class FitExporter:
    """Export workout data to FIT format for indoor kayaking."""

    def __init__(self, pull_length_m: Any = DEFAULT_PULL_LENGTH_M) -> None:
        """Initialize the FIT exporter with recorded power calibration."""
        self.pull_length_m = normalize_pull_length(pull_length_m)
        self.logger = Logger.get_logger(name=__name__)

    @staticmethod
    def convert_csv_to_fit(file_path: str) -> dict[str, Any]:
        """Convert a CSV file to FIT format.

        Args:
            file_path: Path to the CSV file.

        Returns:
            {"success": True, "file_path": Path(...)} on success or
            {"success": False, "message": "..."} on failure.
        """
        try:
            file_path_obj = Path(file_path)
            metadata = load_workout_metadata(file_path_obj)
            processing_config = metadata["processing_config"]
            assert isinstance(processing_config, dict)
            csv_reader = CsvReader(file_path=str(file_path_obj))
            workout_df = csv_reader.read_all()

            if workout_df.is_empty():
                return {
                    "success": False,
                    "message": "CSV file contains no valid workout data",
                }

            fit_path = WorkoutPaths.from_csv(file_path_obj).fit
            program_steps = (
                load_program_steps(file_path_obj)
                if metadata["workout_mode"] == "planned"
                else None
            )
            if metadata["workout_mode"] == "planned" and program_steps is None:
                raise ValueError("Planned v1 workout is missing its step timeline")
            exporter = FitExporter(processing_config["pull_length_m"])
            success = exporter.export(
                workout_df=workout_df,
                output_path=str(fit_path),
                program_steps=program_steps,
            )

            if not success:
                return {
                    "success": False,
                    "message": "Failed to convert CSV to FIT format",
                }

            return {"success": True, "file_path": fit_path}

        except FileNotFoundError as e:
            return {"success": False, "message": f"CSV file not found: {e!s}"}
        except CsvReadError as e:
            # Expected, user-facing failure (bad/empty/malformed CSV); no stack trace.
            return {"success": False, "message": str(e)}
        except Exception as e:
            traceback.print_exc()
            return {
                "success": False,
                "message": f"Error converting CSV to FIT: {e!s}",
            }

    def export(
        self,
        workout_df: Table,
        output_path: str,
        program_steps: list[dict[str, Any]] | None = None,
    ) -> bool:
        """Export workout data to FIT file.

        Args:
            workout_df: Table of recorded sensor samples.
            output_path: Path to output .fit file.
            program_steps: Optional program step timeline; when present the FIT
                laps follow the program's steps instead of auto-pause segments.

        Returns:
            True if export successful, False otherwise.
        """
        if workout_df.height < 2:
            self.logger.warning(msg="Not enough data to create FIT file")
            return False

        try:
            self.logger.info(msg=f"Exporting to FIT format: {output_path}")

            finalized = finalize_workout(
                workout_df,
                program_steps=program_steps,
                logger=self.logger,
            )
            if not finalized.segments:
                self.logger.warning(msg="No moving segments found; FIT export skipped")
                return False
            finalized_metrics = compute_finalized_metrics(
                workout_df,
                finalized,
                pull_length_m=self.pull_length_m,
            )
            fit_start = datetime.fromtimestamp(
                finalized.timeline_start_time_ms / 1000
            )

            builder = FitFileBuilder(auto_define=True, min_string_size=50)

            self._add_file_id(builder=builder, start_time=fit_start)
            self._add_device_info(builder=builder, start_time=fit_start)
            # Add records and get segment information
            record_segments = self._add_records(
                builder=builder, workout_df=workout_df, finalized=finalized
            )

            # Calculate overall statistics
            first_start_ms = finalized.timeline_start_time_ms
            last_end_ms = finalized.timeline_end_time_ms
            total_timer_time_ms = finalized.total_timer_time_ms
            total_elapsed_time_ms = finalized.total_elapsed_time_ms

            # Timer events follow every exact active run. Free laps use those
            # same runs; planned laps can group multiple runs inside one step.
            for lap, metrics in zip(
                finalized.laps, finalized_metrics.laps, strict=True
            ):
                self._add_lap(
                    builder=builder,
                    start_time_ms=lap.start_time_ms,
                    end_time_ms=lap.end_time_ms,
                    timer_time_ms=lap.timer_time_ms,
                    distance_m=lap.distance_m,
                    metrics=metrics,
                )

            # Add overall session summary - MUST be after laps for proper FIT structure
            self._add_session(
                builder=builder,
                start_time_ms=first_start_ms,
                end_time_ms=last_end_ms,
                total_timer_time_ms=total_timer_time_ms,
                total_elapsed_time_ms=total_elapsed_time_ms,
                total_distance_m=finalized.total_distance_m,
                num_laps=len(finalized.laps),
                metrics=finalized_metrics.session,
            )
            self._add_activity(
                builder=builder,
                end_time_ms=last_end_ms,
                total_timer_time_ms=total_timer_time_ms,
            )

            self.logger.info(
                msg=(
                    f"Created {len(finalized.laps)} laps from "
                    f"{len(record_segments)} active run(s), "
                    f"total_timer_time={total_timer_time_ms / 1000:.0f}s, "
                    f"total_elapsed_time={total_elapsed_time_ms / 1000:.0f}s"
                )
            )

            fit_file = builder.build()
            # Never destroy a previously valid FIT before the replacement has
            # been completely written. The sibling temporary file is atomically
            # promoted only after fit_tool finishes successfully.
            output = Path(output_path)
            temp_output = output.with_name(output.name + ".tmp")
            try:
                fit_file.to_file(path=str(temp_output))
                os.replace(temp_output, output)
            finally:
                if temp_output.exists():
                    temp_output.unlink()

            records_written = len(finalized.moving_indices)
            self.logger.info(
                msg=(
                    f"Successfully created FIT file with {records_written} "
                    f"sensor records, {len(finalized.segments)} GPS anchors "
                    f"and {len(finalized.laps)} laps"
                )
            )
            return True

        except Exception as e:
            self.logger.error(msg=f"Error creating FIT file: {e}")
            traceback.print_exc()
            return False

    def _add_file_id(self, builder: FitFileBuilder, start_time: datetime) -> None:
        """Add File ID message.

        Args:
            builder: FIT file builder instance.
            start_time: Workout start time.
        """
        file_id = FileIdMessage()
        file_id.type = FileType.ACTIVITY
        file_id.manufacturer = Manufacturer.DEVELOPMENT.value
        file_id.product = 0
        file_id.time_created = round(start_time.timestamp() * 1000)
        file_id.serial_number = 0x4B594B46  # "KYKF" in hex
        file_id.product_name = "KayakFit"
        builder.add(message=file_id)

    def _add_device_info(self, builder: FitFileBuilder, start_time: datetime) -> None:
        """Add Device Info message for KayakFirst Bull.

        Args:
            builder: FIT file builder instance.
            start_time: Workout start time.
        """
        device_info = DeviceInfoMessage()
        device_info.timestamp = round(start_time.timestamp() * 1000)
        device_info.manufacturer = Manufacturer.DEVELOPMENT.value
        device_info.product = 0
        device_info.serial_number = 0x4B594B46  # "KYKF" in hex
        device_info.device_index = 0
        device_info.product_name = "KayakFirst Bull"
        builder.add(message=device_info)

    def _add_records(
        self,
        builder: FitFileBuilder,
        workout_df: Table,
        finalized: FinalizedWorkout | None = None,
    ) -> tuple[ActivitySegment, ...]:
        """Add active data records with exact activity-run timer events.

        Args:
            builder: FIT file builder instance.
            workout_df: Table of workout data points.
            finalized: Optional pre-resolved authoritative timeline.

        Returns:
            Exact typed activity segments used for timer events.
        """
        self.logger.info(msg=f"Adding {workout_df.height} data records...")

        finalized = finalized or finalize_workout(workout_df, logger=self.logger)
        timestamps_ms = finalized.timestamps_ms

        # Materialize columns to Python lists once for fast positional access.
        # FIT cadence retains the instantaneous samples; estimated power alone
        # uses the calibrated stroke-averaged cadence/force pair.
        heart_rate_col = workout_df.get_column_or_none("heart_rate")
        cadence_col = workout_df.get_column_or_none("cadence_instant")
        power_cadence_col = workout_df.get_column_or_none("cadence")
        power_pull_force_col = workout_df.get_column_or_none("pull_force")
        # FIT record speed is the same raw instantaneous channel used by the
        # summary and chart.
        speed_by_index = recorded_speed_mps(workout_df, finalized)

        # The shared finalizer supplies the exact same activity segments used by
        # the summary, so exported and displayed splits agree by construction.
        segments = finalized.segments

        def _emit_timer_event(event_type: Any, timestamp_ms: int, trigger: Any) -> None:
            """Add a single TIMER event (START / STOP_ALL) at a given time."""
            event = EventMessage()
            event.event = Event.TIMER
            event.event_type = event_type
            event.event_group = 0
            event.timer_trigger = trigger
            event.timestamp = timestamp_ms
            builder.add(message=event)

        def _add_gps_anchor(timestamp_ms: int, distance_m: float) -> None:
            """Anchor the compatibility track at an active sample-window start."""
            anchor = RecordMessage()
            anchor.timestamp = timestamp_ms
            anchor.distance = distance_m
            latitude, longitude = _synthetic_gps_position(distance_m)
            anchor.position_lat = latitude
            anchor.position_long = longitude
            builder.add(message=anchor)

        def _add_record(i: int) -> float | None:
            """Build and add one record message for row ``i``."""
            record = RecordMessage()
            record.timestamp = timestamps_ms[i]

            if heart_rate_col is not None and heart_rate_col[i] is not None:
                heart_rate = int(heart_rate_col[i])
                if 0 < heart_rate <= 255:
                    record.heart_rate = heart_rate

            if cadence_col is not None and cadence_col[i] is not None:
                cadence = int(cadence_col[i])
                if 0 < cadence <= 255:
                    record.cadence = cadence

            record_distance = finalized.record_cumulative_distance_m.get(i)
            if record_distance is not None and record_distance >= 0:
                record.distance = record_distance
                latitude, longitude = _synthetic_gps_position(record_distance)
                record.position_lat = latitude
                record.position_long = longitude

            record_speed = speed_by_index.get(i)
            if record_speed is not None and 0 <= record_speed <= 65.535:
                record.speed = record_speed

            if power_pull_force_col is not None and power_cadence_col is not None:
                power = estimate_power(
                    power_pull_force_col[i],
                    power_cadence_col[i],
                    self.pull_length_m,
                )
                if power is not None and 0 < power <= 65535:
                    record.power = power

            builder.add(message=record)
            return record_distance if record_distance is not None else None

        # Emit events and records in strict chronological order so the file is
        # well-formed for any FIT reader (not just Strava):
        #   [resume START] -> GPS anchor -> sensor records -> STOP_ALL
        last_index = len(segments) - 1
        previous_distance_m = 0.0
        for seg_index, segment in enumerate(segments):
            if seg_index == 0:
                _emit_timer_event(
                    EventType.START, segment.start_time_ms, TimerTrigger.MANUAL
                )
            else:
                self.logger.debug(
                    msg=f"Resume START at ts={segment.start_time_ms} "
                    f"(segment {seg_index})"
                )
                _emit_timer_event(
                    EventType.START, segment.start_time_ms, TimerTrigger.AUTO
                )

            # Sensor records describe intervals ending at their timestamp. A
            # coordinate only at that endpoint makes Strava lose the first
            # represented window of every run. This metric-free anchor supplies
            # the position before that window and keeps the paused interval at
            # the preceding cumulative distance.
            _add_gps_anchor(segment.start_time_ms, previous_distance_m)
            for i in segment.record_indices:
                record_distance = _add_record(i)
                if record_distance is not None and record_distance >= 0:
                    previous_distance_m = record_distance

            if seg_index < last_index:
                self.logger.debug(
                    msg=f"Autopause STOP_ALL at ts={segment.end_time_ms} "
                    f"(segment {seg_index})"
                )
                _emit_timer_event(
                    EventType.STOP_ALL, segment.end_time_ms, TimerTrigger.AUTO
                )
            else:
                # Final manual stop at the end of the workout.
                _emit_timer_event(
                    EventType.STOP_ALL, segment.end_time_ms, TimerTrigger.MANUAL
                )

        return segments

    def _add_session(
        self,
        builder: FitFileBuilder,
        start_time_ms: int,
        end_time_ms: int,
        total_timer_time_ms: int,
        total_elapsed_time_ms: int,
        total_distance_m: float,
        num_laps: int,
        metrics: SliceMetrics,
    ) -> None:
        """Add session summary message.

        Args:
            builder: FIT file builder instance.
            start_time_ms: First segment start timestamp in milliseconds.
            end_time_ms: Last segment end timestamp in milliseconds.
            total_timer_time_ms: Sum of all lap durations in milliseconds (moving time).
            total_elapsed_time_ms: Time from first start to last stop in milliseconds (total time).
            total_distance_m: Distance attributed to active ergometer windows.
            num_laps: Number of laps in the session.
            metrics: Finalized session aggregate shared with the summary.
        """
        session = SessionMessage()
        session.timestamp = end_time_ms
        session.start_time = start_time_ms
        session.total_elapsed_time = total_elapsed_time_ms / 1000.0
        session.total_timer_time = total_timer_time_ms / 1000.0
        session.trigger = SessionTrigger.ACTIVITY_END
        session.sport = Sport.KAYAKING
        session.sub_sport = SubSport.GENERIC
        session.first_lap_index = 0
        session.num_laps = num_laps

        session.total_distance = total_distance_m
        self._set_aggregate_fields(session, metrics)

        builder.add(message=session)

    def _add_activity(
        self,
        builder: FitFileBuilder,
        end_time_ms: int,
        total_timer_time_ms: int,
    ) -> None:
        """Add the required final activity summary for this single session."""
        activity = ActivityMessage()
        activity.timestamp = end_time_ms
        activity.total_timer_time = total_timer_time_ms / 1000.0
        activity.num_sessions = 1
        activity.type = Activity.MANUAL
        activity.event = Event.ACTIVITY
        activity.event_type = EventType.STOP
        builder.add(message=activity)

    def _add_lap(
        self,
        builder: FitFileBuilder,
        start_time_ms: int,
        end_time_ms: int,
        timer_time_ms: int,
        distance_m: float,
        metrics: SliceMetrics,
    ) -> None:
        """Add lap summary message for a specific segment.

        Args:
            builder: FIT file builder instance.
            start_time_ms: Segment start timestamp in milliseconds.
            end_time_ms: Segment end timestamp in milliseconds.
            timer_time_ms: Sum of active ergometer sample windows.
            distance_m: Distance attributed to this lap's active windows.
            metrics: Finalized lap aggregate shared with the summary.
        """
        lap = LapMessage()
        lap.timestamp = end_time_ms
        lap.start_time = start_time_ms
        lap.total_elapsed_time = (end_time_ms - start_time_ms) / 1000.0
        lap.total_timer_time = timer_time_ms / 1000.0
        lap.sport = Sport.KAYAKING
        lap.sub_sport = SubSport.GENERIC

        lap.total_distance = distance_m
        self._set_aggregate_fields(lap, metrics)

        builder.add(message=lap)

    @staticmethod
    def _set_aggregate_fields(message: Any, metrics: SliceMetrics) -> None:
        """Copy one finalized aggregate into a FIT session or lap message."""
        for attr, value, integer in (
            ("avg_heart_rate", metrics.avg_heart_rate, True),
            ("max_heart_rate", metrics.max_heart_rate, True),
            ("avg_cadence", metrics.avg_cadence, True),
            ("max_cadence", metrics.max_cadence, True),
            ("avg_power", metrics.avg_power, True),
            ("max_power", metrics.max_power, True),
            ("avg_speed", metrics.avg_speed, False),
            ("max_speed", metrics.max_speed, False),
        ):
            if value is not None:
                setattr(message, attr, round(value) if integer else value)
