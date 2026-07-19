"""Minimal columnar table backed by plain Python lists.

This provides only the column and row access needed by workout processing,
without adding a DataFrame dependency to the packaged application.
"""

from collections.abc import Sequence
from decimal import Decimal
from typing import Any


class Table:
    """An ordered set of rows, each a ``{column: value}`` dict."""

    def __init__(self, columns: Sequence[str], rows: list[dict[str, Any]]) -> None:
        self._columns = list(columns)
        self._column_set = frozenset(self._columns)  # O(1) membership tests
        self._rows = rows

    # ---- shape -------------------------------------------------------------
    @property
    def height(self) -> int:
        """Return the number of rows."""
        return len(self._rows)

    @property
    def columns(self) -> list[str]:
        """Return the column names in order."""
        return list(self._columns)

    def is_empty(self) -> bool:
        """Return True if the table has no rows."""
        return not self._rows

    def __contains__(self, column: str) -> bool:  # enables `"x" in table`
        return column in self._column_set

    # ---- access ------------------------------------------------------------
    def get_column(self, name: str) -> list[Any]:
        """Return all values for a column (missing column -> all None)."""
        return [row.get(name) for row in self._rows]

    def get_column_or_none(self, name: str) -> list[Any] | None:
        """Return a column's values, or None when the column doesn't exist.

        The common "optional column" pattern (``get_column(x) if x in table
        else None``) in one place: callers that treat a missing column as "no
        data at all" (rather than a column of Nones) use this.
        """
        return self.get_column(name) if name in self._column_set else None

    def row(self, index: int) -> dict[str, Any]:
        """Return a single row as a dict (supports negative indices)."""
        return dict(self._rows[index])

def parse_value(raw: str, kind: str) -> Any | None:
    """Parse a CSV cell to the configured type (``"int"`` or ``"decimal"``).

    Empty strings and the literal ``"None"`` become None, matching how the CSV
    was written for absent readings.

    Raises:
        ValueError: If a non-empty value cannot be parsed as the given type.
    """
    if raw is None:
        return None
    text = raw.strip()
    if text == "" or text == "None":
        return None
    if kind == "decimal":
        return Decimal(text)
    return int(text)
