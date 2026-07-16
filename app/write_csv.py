"""Workout CSV writing.

Streams workout records to a semicolon-separated CSV (spreadsheet-friendly) as
they arrive: every row is flushed immediately and the file is periodically
fsync'ed, so an interrupted session keeps its data, and the file is made
read-only on close to protect the recording. The write path is deliberately
simple; the defensive per-cell/per-row recovery lives in the read path
(``app.read_csv``).
"""

import contextlib
import csv
import os
import stat
import time
from typing import Any

from .field_mapping import ALL_FIELDS
from .logger import Logger

# Force data to physical disk at most this often (seconds). flush() pushes to
# the OS buffer every row; fsync() guarantees durability across a power loss
# without the cost of syncing on every single record.
FSYNC_INTERVAL_S = 5.0


class CsvWriteError(OSError):
    """Raised when a workout row cannot be persisted safely."""


class CsvWriter:
    """Writer for workout data in CSV format with semicolon separator."""

    def __init__(self, output_path: str, config: dict[str, Any]) -> None:
        """Initialize CSV writer.

        Args:
            output_path: Path to output .csv file.
            config: Configuration dictionary.
        """
        self.output_path = output_path
        self.config = config
        self.file: Any | None = None
        self.writer: csv.DictWriter[str] | None = None
        self._last_fsync = 0.0
        self.records_written = 0
        self.failed = False

        self.logger = Logger.get_logger(name=__name__)

        self._open()

    def _open(self) -> None:
        """Open CSV file for writing and write header."""
        try:
            # The file must stay open across many write_record() calls and is
            # closed explicitly in close() (this object is itself a context
            # manager, see __enter__/__exit__), so a `with` here would close
            # it prematurely despite SIM115.
            self.file = open(  # noqa: SIM115
                file=self.output_path, mode="x", newline="", encoding="utf-8"
            )
            self.writer = csv.DictWriter(
                f=self.file,
                fieldnames=ALL_FIELDS,
                delimiter=";",
                extrasaction="ignore"
            )
            self.writer.writeheader()
            self.file.flush()
            os.fsync(self.file.fileno())
            self.logger.info(msg=f"Opened CSV file: {self.output_path}")
        except FileExistsError:
            raise
        except Exception as e:
            if self.file is not None:
                with contextlib.suppress(Exception):
                    self.file.close()
                self.file = None
            with contextlib.suppress(OSError):
                os.unlink(self.output_path)
            self.logger.error(msg=f"Failed to open CSV file: {e}")
            raise CsvWriteError(f"Could not create workout CSV: {e}") from e

    def write_record(self, data: dict[str, Any]) -> None:
        """Write a single data record.

        Args:
            data: Dictionary containing workout data point.
        """
        if self.failed:
            raise CsvWriteError("CSV writer is unavailable after an earlier write failure")
        if not self.writer or not self.file:
            raise CsvWriteError("CSV writer is not initialized")

        try:
            record = {field: data.get(field) for field in ALL_FIELDS}
            self.writer.writerow(rowdict=record)
            # Flush each record so an unexpected shutdown does not lose data...
            self.file.flush()
            # ...and periodically fsync so a hard power loss can't drop rows
            # still sitting in the OS write cache. An fsync failure matters: the
            # application must not report a row as durable after the filesystem
            # has rejected the durability boundary.
            now = time.monotonic()
            if now - self._last_fsync >= FSYNC_INTERVAL_S:
                os.fsync(self.file.fileno())
                self._last_fsync = now
            self.records_written += 1
        except Exception as e:
            self.failed = True
            self.logger.error(msg=f"Error writing record: {e}")
            raise CsvWriteError(
                f"Could not save workout row {self.records_written + 1}: {e}"
            ) from e

    def close(self) -> str:
        """Close CSV file.

        Returns:
            Path to the closed file.
        """
        close_error: Exception | None = None
        if self.file:
            try:
                self.file.flush()
                os.fsync(self.file.fileno())
            except (OSError, ValueError) as e:
                close_error = e
                self.failed = True
            try:
                self.file.close()
            except OSError as e:
                close_error = close_error or e
                self.failed = True
            self.file = None
            self.writer = None

        try:
            os.chmod(path=self.output_path, mode=stat.S_IREAD)
            self.logger.info(msg=f"Closed CSV file: {self.output_path}  [READ-ONLY]")
        except Exception:
            self.logger.info(msg=f"Closed CSV file: {self.output_path}")

        if close_error is not None:
            raise CsvWriteError(f"Could not finalize workout CSV: {close_error}") from close_error

        return self.output_path

    def __enter__(self) -> CsvWriter:
        """Context manager entry."""
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Context manager exit."""
        self.close()
