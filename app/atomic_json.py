"""Durable atomic JSON persistence for workout control files."""

import contextlib
import json
import os
from pathlib import Path
from typing import Any


def write_atomic_json(path: Path, payload: Any) -> None:
    """Replace ``path`` only after its complete JSON payload is durable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    try:
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        with contextlib.suppress(OSError):
            temp.unlink()
