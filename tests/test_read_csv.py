"""
Tests for the stdlib CSV reader (replacement for the former Polars reader).

Covers typed parsing, preservation of the raw timeline, schema validation, and
the resulting Table shape.

Run from the project root:

    python tests/test_read_csv.py
"""

import contextlib
import logging
import sys
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.field_mapping import ALL_FIELDS
from app.read_csv import CsvReader, CsvReadError

CONFIG = {"log_level": "warning"}


@contextlib.contextmanager
def _capture_reader_logs(level=logging.WARNING):
    """Capture records emitted by app.read_csv's module logger."""
    records = []

    class _ListHandler(logging.Handler):
        def emit(self, record):
            records.append(record)

    logger = logging.getLogger("app.read_csv")
    handler = _ListHandler()
    handler.setLevel(level)
    logger.addHandler(handler)
    old_level = logger.level
    logger.setLevel(level)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


def _row(i: int, speed: str = "3.20", distance: str | None = None) -> dict[str, Any]:
    dist = distance if distance is not None else f"{i * 3.2:.2f}"
    return {
        "timestamp": 1_700_000_000_000 + i * 1000,
        "session_elapsed__s": i,
        "sample_duration__s": 1,
        "heart_rate__bpm": 120 + (i % 10),
        "kayakfirst_timestamp": 1_700_000_000_000 + i * 1000,
        "col_2": 0, "col_3": 0, "col_4": "0.00", "col_5": "0.00",
        "col_6": 0, "active_paddling": 1 if float(speed) > 0 else 0, "col_8": 0,
        "distance__m": dist,
        "speed__mps": speed, "speed_instant__mps": speed,
        "cadence__spm": 80, "cadence_instant__spm": 80,
        "pace_200m__s": 62, "pace_200m_instant__s": 62,
        "pace_500m__s": 156, "pace_500m_instant__s": 156,
        "pace_1000m__s": 312, "pace_1000m_instant__s": 312,
        "pull_force__n": 200, "pull_force_instant__n": 200,
        "elapsed_time__s": i, "window_size__s": 1,
    }


def _write(path: Path, rows: list[Any], full_schema: bool = True) -> None:
    fields = ALL_FIELDS if full_schema else ALL_FIELDS[:5]
    _write_cols(path, rows, fields)


def _write_cols(path: Path, rows: list[Any], fields: list[str]) -> None:
    """Write a CSV with an explicit column list (for schema-tolerance tests)."""
    lines = [";".join(fields)]
    for r in rows:
        lines.append(";".join(str(r[f]) for f in fields))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_parse_types_and_shape() -> None:
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "w.csv"
        _write(p, [_row(i) for i in range(1, 6)])
        t = CsvReader(str(p), CONFIG).read_all()
        assert t.columns == [
            "timestamp", "session_elapsed", "sample_duration",
            "device_elapsed", "device_window", "kayakfirst_timestamp",
            "heart_rate", "cadence", "distance", "speed_instant",
            "pull_force", "active",
        ]
        assert t.height == 5
        assert isinstance(t.get_column("timestamp")[0], int)
        assert isinstance(t.get_column("distance")[0], Decimal)
        assert isinstance(t.get_column("speed_instant")[0], Decimal)
        print("  ok: typed columns (int timestamp, Decimal distance/speed)")


def test_unused_malformed_pace_does_not_discard_sensor_row() -> None:
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "w.csv"
        row = _row(1)
        row["pace_200m__s"] = "(0, 0)"
        _write(p, [row])

        table = CsvReader(str(p), CONFIG).read_all()

        assert table.height == 1
        assert table.get_column("distance") == [Decimal("3.20")]


def test_canonical_metric_channels_are_fixed() -> None:
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "w.csv"
        row = _row(1)
        row["speed__mps"] = "2.50"
        row["speed_instant__mps"] = "3.25"
        row["cadence__spm"] = 60
        row["cadence_instant__spm"] = 90
        row["pull_force__n"] = 100
        row["pull_force_instant__n"] = 200
        _write(p, [row])

        table = CsvReader(str(p), CONFIG).read_all()

        assert table.get_column("speed_instant") == [Decimal("3.25")]
        assert table.get_column("cadence") == [60]
        assert table.get_column("pull_force") == [100]


def test_stationary_rows_are_preserved() -> None:
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "w.csv"
        # Stationary rows are part of the canonical timeline; segmentation,
        # rather than the CSV parser, decides whether they reach the FIT file.
        rows = [_row(i) for i in range(1, 4)]
        rows += [_row(i, speed="0.00", distance=f"{3 * 3.2:.2f}") for i in range(4, 6)]
        _write(p, rows)
        t = CsvReader(str(p), CONFIG).read_all()
        assert t.height == 5, f"expected all 5 rows, got {t.height}"
        print("  ok: stationary rows preserved for pause detection")


def test_repeated_payload_rows_are_preserved() -> None:
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "w.csv"
        # Equal metric payloads at distinct timestamps are not provably duplicate
        # sensor events and must remain in the canonical timeline.
        r1 = _row(1, distance="10.00")
        dup = dict(r1)
        dup["timestamp"] = r1["timestamp"] + 1000
        dup["session_elapsed__s"] = 2
        dup["heart_rate__bpm"] = r1["heart_rate__bpm"] + 5
        r3 = _row(1, distance="20.00")  # different distance -> distinct row
        r3["timestamp"] = r1["timestamp"] + 2000
        r3["session_elapsed__s"] = 3
        _write(p, [r1, dup, r3])
        t = CsvReader(str(p), CONFIG).read_all()
        assert t.height == 3, f"expected all repeated samples, got {t.height}"
        assert t.get_column("timestamp")[0] == r1["timestamp"]
        print("  ok: repeated payload rows preserved")


def test_schema_mismatch_raises() -> None:
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "bad.csv"
        _write(p, [_row(i) for i in range(1, 4)], full_schema=False)
        try:
            CsvReader(str(p), CONFIG).read_all()
            raised = False
        except CsvReadError:
            raised = True
        assert raised, "expected CsvReadError on schema mismatch"
        print("  ok: schema mismatch raises CsvReadError")


def test_one_corrupt_cell_keeps_other_rows() -> None:
    # Primary fix: a single unparseable cell skips only its row, never the file.
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "w.csv"
        rows = [_row(i) for i in range(1, 21)]  # 20 distinct moving rows
        rows[9]["distance__m"] = "abc"  # 10th data row -> line 11, decimal col
        _write(p, rows)
        with _capture_reader_logs() as recs:
            t = CsvReader(str(p), CONFIG).read_all()
        assert t.height == 19, f"expected 19 surviving rows, got {t.height}"
        msgs = [r.getMessage() for r in recs]
        assert any("line 11" in m and "distance__m" in m for m in msgs), \
            f"expected a per-row skip warning naming line 11/distance__m, got {msgs}"
    print("  ok: one corrupt cell skips only its row, rest survive (warned)")


def test_mostly_corrupt_file_raises() -> None:
    # Majority-corrupt file is surfaced distinctly, not returned as a fragment.
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "bad.csv"
        rows = [_row(i) for i in range(1, 11)]  # 10 data rows
        for j in range(6):  # 6/10 = 60% > 50% threshold
            rows[j]["distance__m"] = "xx"
        _write(p, rows)
        try:
            CsvReader(str(p), CONFIG).read_all()
            raised, msg = False, ""
        except CsvReadError as e:
            raised, msg = True, str(e)
        assert raised, "a majority-corrupt file should raise CsvReadError"
        assert "mostly unreadable" in msg, f"expected distinct message, got {msg!r}"
    print("  ok: majority-corrupt file raises a distinct CsvReadError")


def test_missing_column_is_rejected() -> None:
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "w.csv"
        fields = [f for f in ALL_FIELDS if f != "active_paddling"]
        _write_cols(p, [_row(i) for i in range(1, 6)], fields)
        with pytest.raises(CsvReadError, match="active_paddling"):
            CsvReader(str(p), CONFIG).read_all()


def test_unknown_extra_column_is_rejected() -> None:
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "w.csv"
        rows = [_row(i) for i in range(1, 6)]
        for r in rows:
            r["future_metric"] = 42
        _write_cols(p, rows, [*ALL_FIELDS, "future_metric"])
        with pytest.raises(CsvReadError, match="future_metric"):
            CsvReader(str(p), CONFIG).read_all()


def test_missing_required_column_raises() -> None:
    # Secondary fix: a load-bearing column missing still fails clearly.
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "bad.csv"
        fields = [f for f in ALL_FIELDS if f != "distance__m"]
        _write_cols(p, [_row(i) for i in range(1, 6)], fields)
        try:
            CsvReader(str(p), CONFIG).read_all()
            raised, msg = False, ""
        except CsvReadError as e:
            raised, msg = True, str(e)
        assert raised, "missing a required column must raise CsvReadError"
        assert "distance__m" in msg, f"error should name the missing column, got {msg!r}"
    print("  ok: missing load-bearing column still raises CsvReadError")


def test_length_mismatch_row_still_skipped() -> None:
    # Regression: a field-count-mismatched (crash-truncated) row is skipped, the
    # rest survive — unchanged pre-existing behavior.
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "w.csv"
        rows = [_row(i) for i in range(1, 6)]
        _write(p, rows)
        lines = p.read_text(encoding="utf-8").splitlines()
        lines[3] = ";".join(lines[3].split(";")[:-4])  # truncate 3rd data row
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with _capture_reader_logs() as recs:
            t = CsvReader(str(p), CONFIG).read_all()
        assert t.height == 4, f"expected 4 rows after skipping truncated one, got {t.height}"
        assert any("field-count mismatch" in r.getMessage() for r in recs), \
            "truncated row skip should be logged"
    print("  ok: field-count-mismatch row still skipped, rest survive")


def test_empty_file_raises() -> None:
    # Regression: a genuinely empty file still fails clearly.
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "empty.csv"
        p.write_text("", encoding="utf-8")
        try:
            CsvReader(str(p), CONFIG).read_all()
            raised = False
        except CsvReadError:
            raised = True
        assert raised, "empty file should raise CsvReadError"
    print("  ok: empty file still raises CsvReadError")
