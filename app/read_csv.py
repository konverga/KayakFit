"""Workout CSV reading.

Parses the semicolon-separated workout CSV into a Table, converting each cell
to its schema type (see ``app.field_mapping``) and supporting both the
"instant" and "averaged" data-point modes. The v1 header must match the writer
schema exactly. Recovery is local to malformed data rows (for example a
crash-truncated final line); schema drift and majority-corrupt files raise
``CsvReadError``.
"""

import csv
from pathlib import Path
from typing import Any

from .field_mapping import COLUMN_TYPES, CSV_COLUMNS
from .logger import Logger
from .table import Table, parse_value

# If more than this fraction of the well-formed data rows fail type conversion,
# the file is treated as fundamentally corrupt (wrong/garbled file, mangled
# encoding, not our data) and rejected outright, rather than silently returning
# whatever fragment happened to parse. A handful of bad cells in an otherwise
# good multi-hour recording is recoverable and expected; a majority-bad file is
# not a recording worth recovering. Mirrors the "is this normal or
# catastrophic" judgement made for runaway programs in app/program.py.
_MAX_CORRUPT_ROW_FRACTION = 0.5


class CsvReadError(Exception):
    """Raised when a workout CSV cannot be read or fails validation."""


class CsvReader:
    """Read workout data from CSV files with semicolon separator."""

    def __init__(self, file_path: str, config: dict[str, Any]) -> None:
        """Initialize reader with path to CSV file.

        Args:
            file_path: Path to the CSV file to read.
            config: Configuration dictionary.

        Raises:
            FileNotFoundError: If CSV file does not exist.
        """
        self.file_path = Path(file_path)
        self.config = config

        if not self.file_path.exists():
            raise FileNotFoundError(f"CSV file not found: {file_path}")

        self.logger = Logger.get_logger(name=__name__)


    def read_all(self, data_points: str = "instant") -> Table:
        """Read all records from the CSV file.

        Args:
            data_points: Type of data points to use ("instant" or "averaged").

        Returns:
            Table of workout data records (columns: timestamp, heart_rate,
            cadence, distance, speed, speed_instant, pull_force, active,
            sample_duration). ``speed`` follows ``data_points`` while
            ``speed_instant`` is retained for resume-distance validation.
            ``active`` is the ergometer's "actively paddling" flag (None in
            recordings from firmware that doesn't send it).
        """
        self.logger.info(
            msg=f"Read records from {self.file_path.name} with {data_points} data points."
        )

        # Preserve the validated recording timeline exactly. Moving/stationary
        # classification belongs to app.segmentation; deleting low-speed rows
        # here used to erase short pauses and turn longer ones into timestamp
        # gaps. Likewise, payload-level de-duplication could remove legitimate
        # samples from a later interval merely because every metric happened to
        # repeat. Transport duplicates, if the protocol proves they exist, must
        # be identified locally and adjacently during capture rather than by a
        # global value-based pass over the canonical recording.

        if data_points not in ("instant", "averaged"):
            raise CsvReadError(f"Unsupported data point mode: {data_points!r}")
        suffix = "_instant" if data_points == "instant" else ""
        key_map: dict[str, tuple[str, ...]] = {
            "timestamp": ("timestamp",),
            "heart_rate__bpm": ("heart_rate",),
            f"cadence{suffix}__spm": ("cadence",),
            "distance__m": ("distance",),
            f"speed{suffix}__mps": ("speed",),
            f"pull_force{suffix}__n": ("pull_force",),
            "active_paddling": ("active",),
            "window_size__s": ("sample_duration",),
        }
        if data_points == "instant":
            key_map["speed_instant__mps"] = ("speed", "speed_instant")
        else:
            key_map["speed_instant__mps"] = ("speed_instant",)

        out_columns = [
            "timestamp", "heart_rate", "cadence", "distance", "speed",
            "speed_instant", "pull_force", "active", "sample_duration",
        ]
        out_rows = self._read_rows(key_map)
        return Table(out_columns, out_rows)

    def _read_rows(
        self, key_map: dict[str, tuple[str, ...]]
    ) -> list[dict[str, Any]]:
        """Stream the CSV into the reduced typed rows used by processing."""
        try:
            with open(self.file_path, encoding="utf-8", newline="") as f:
                reader = csv.reader(f, delimiter=";")
                try:
                    raw_header = next(reader)
                except StopIteration as exc:
                    raise CsvReadError("CSV file is empty or contains no data.") from exc

                header = [h.strip() for h in raw_header]
                if header != CSV_COLUMNS:
                    missing = sorted(set(CSV_COLUMNS) - set(header))
                    extra = sorted(set(header) - set(CSV_COLUMNS))
                    order_error = not missing and not extra
                    raise CsvReadError(
                        "Workout is not v1: CSV schema mismatch "
                        f"(missing={missing}, extra={extra}, order_error={order_error})"
                    )

                parsed: list[dict[str, Any]] = []
                length_skipped = 0
                corrupt_skipped = 0
                data_row_count = 0
                for line_no, raw in enumerate(reader, start=2):
                    if not raw or (len(raw) == 1 and raw[0].strip() == ""):
                        continue
                    if len(raw) != len(header):
                        length_skipped += 1
                        continue
                    data_row_count += 1
                    record: dict[str, Any] = {}
                    try:
                        for col, value in zip(header, raw, strict=True):
                            destinations = key_map.get(col)
                            if destinations is None:
                                continue
                            kind = COLUMN_TYPES.get(col)
                            typed = value if kind is None else parse_value(value, kind)
                            for destination in destinations:
                                record[destination] = typed
                    except (ValueError, ArithmeticError) as e:
                        corrupt_skipped += 1
                        self.logger.warning(
                            msg=(
                                f"Skipping CSV line {line_no}: could not parse column "
                                f"{col!r} value {value!r} ({e})"
                            )
                        )
                        continue
                    parsed.append(record)
        except PermissionError as e:
            raise CsvReadError(f"Permission denied reading: {self.file_path}") from e
        except UnicodeDecodeError as e:
            raise CsvReadError("Encoding error reading CSV.") from e
        except OSError as e:
            raise CsvReadError(f"Could not read CSV: {self.file_path}") from e

        if length_skipped:
            self.logger.warning(
                msg=f"Skipped {length_skipped} malformed CSV row(s) (field-count mismatch)."
            )
        if corrupt_skipped:
            self.logger.warning(
                msg=f"Skipped {corrupt_skipped} CSV row(s) with unparseable values."
            )

        # A majority-corrupt file is not a recording with a few bad cells; it is
        # the wrong/garbled file. Surface that distinctly instead of returning a
        # misleading fragment. (The empty-result check below still covers the
        # all-corrupt / no-data cases.)
        if corrupt_skipped and corrupt_skipped > _MAX_CORRUPT_ROW_FRACTION * data_row_count:
            raise CsvReadError(
                f"CSV is mostly unreadable: {corrupt_skipped} of {data_row_count} "
                "data rows failed to parse."
            )

        if not parsed:
            raise CsvReadError("CSV file is empty or contains no valid data.")
        return parsed
