"""Shared field and CSV-schema definitions.

The single place that names the KayakFirst ergometer's packet fields and the
workout CSV's columns: ``FIELD_MAP`` maps packet indices to field names,
``CSV_COLUMNS`` / ``COLUMN_TYPES`` define the CSV schema and each column's
parse type (int or decimal), and ``ALL_FIELDS`` is the full written-column
order. Used by the BLE parser, the CSV reader/writer, and the FIT exporter so
the data structure is never defined twice.
"""

# Columns stored as fixed-point decimals; everything else is an integer.
_DECIMAL_COLUMNS = {
    "col_4",
    "col_5",
    "distance__m",
    "session_elapsed__s",
    "sample_duration__s",
    "speed__mps",
    "speed_instant__mps",
}

# Ordered list of every CSV column, and each column's parse type
# ("int" or "decimal"). Replaces the former Polars schema.
CSV_COLUMNS: list[str] = [
    "timestamp",
    "session_elapsed__s",
    "sample_duration__s",
    "heart_rate__bpm",
    "kayakfirst_timestamp",
    "col_2",
    "col_3",
    "col_4",
    "col_5",
    "col_6",
    "active_paddling",
    "col_8",
    "distance__m",
    "speed__mps",
    "speed_instant__mps",
    "cadence__spm",
    "cadence_instant__spm",
    "pace_200m__s",
    "pace_200m_instant__s",
    "pace_500m__s",
    "pace_500m_instant__s",
    "pace_1000m__s",
    "pace_1000m_instant__s",
    "pull_force__n",
    "pull_force_instant__n",
    "elapsed_time__s",
    "window_size__s",
]

COLUMN_TYPES: dict[str, str] = {
    col: ("decimal" if col in _DECIMAL_COLUMNS else "int") for col in CSV_COLUMNS
}

FIELD_MAP: dict[int, str] = {
    1: "kayakfirst_timestamp",
    2: "col_2",
    3: "col_3",
    4: "col_4",
    5: "col_5",
    6: "col_6",
    # 1 while strokes are being detected, 0 when idle (also 0 on the stale
    # repeated packets the erg sends after stopping, making it more reliable
    # than the frozen speed/distance fields there).
    7: "active_paddling",
    8: "col_8",
    9: "distance__m",
    10: "speed__mps",
    11: "speed_instant__mps",
    12: "cadence__spm",
    13: "cadence_instant__spm",
    14: "pace_200m__s",
    15: "pace_200m_instant__s",
    16: "pace_500m__s",
    17: "pace_500m_instant__s",
    18: "pace_1000m__s",
    19: "pace_1000m_instant__s",
    20: "pull_force__n",
    21: "pull_force_instant__n",
    22: "elapsed_time__s",
    # Seconds covered by this datapoint: 1 normally, 2 when a poll was missed,
    # larger on the first poll (covers everything since the workout started).
    23: "window_size__s",
}

ALL_FIELDS: list[str] = list(CSV_COLUMNS)


def get_field_name(index: int) -> str:
    """Get field name for a given packet index.

    Args:
        index: Index in the packet (1-based after skipping '6' prefix).

    Returns:
        Field name (returns col_N for unknown indices).
    """
    return FIELD_MAP.get(index, f"col_{index}")
