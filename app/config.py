"""Typed accessors for the raw config dict.

``config.yml`` values arrive untyped (YAML scalars, often strings like
``"yes"``). Boolean-ish settings used to be parsed ad hoc at every call site
with slightly diverging accepted spellings; this module is the single place
that defines what counts as true.
"""

from collections.abc import Mapping
from typing import Any

# Accepted truthy spellings for boolean-ish config values, lowercased.
_TRUTHY = frozenset({"yes", "y", "true", "1", "on"})


def parse_bool(value: Any, default: bool = False) -> bool:
    """Interpret a config-style boolean value.

    Real booleans pass through; anything else is stringified and matched
    against the accepted truthy spellings (``yes``/``y``/``true``/``1``/``on``,
    case-insensitive). ``None`` returns ``default``.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in _TRUTHY


def get_bool(config: Mapping[str, Any], key: str, default: bool = False) -> bool:
    """Read a boolean-ish value from a config mapping (see :func:`parse_bool`)."""
    return parse_bool(config.get(key), default=default)
