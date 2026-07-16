"""Versioned processing metadata stored in each workout directory."""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .atomic_json import write_atomic_json
from .workout_paths import WorkoutPaths

PROCESSING_CONFIG_KEYS = (
    "pull_length_m",
    "data_points",
    "autopause_stationary_threshold_records",
    "autopause_gap_seconds",
    "max_hr",
    "hr_zone_mode",
    "hr_zones",
)
METADATA_VERSION = 1


def _validate_processing_config(saved: dict[str, Any]) -> None:
    """Validate every value that can change finalized workout output."""
    try:
        pull_length = float(saved["pull_length_m"])
        stationary = int(saved["autopause_stationary_threshold_records"])
        gap = float(saved["autopause_gap_seconds"])
        max_hr = float(saved["max_hr"])
    except (TypeError, ValueError) as exc:
        raise ValueError("Workout is not v1: invalid numeric processing metadata") from exc
    if not 0.3 <= pull_length <= 1.2:
        raise ValueError("Workout is not v1: pull_length_m is out of range")
    if isinstance(saved["autopause_stationary_threshold_records"], bool) or stationary < 1:
        raise ValueError("Workout is not v1: invalid autopause stationary threshold")
    if gap < 0 or max_hr <= 0:
        raise ValueError("Workout is not v1: invalid gap or maximum heart rate")
    if saved["data_points"] not in ("instant", "averaged"):
        raise ValueError("Workout is not v1: invalid data_points mode")
    zone_mode = saved["hr_zone_mode"]
    zones = saved["hr_zones"]
    if zone_mode not in ("auto", "manual") or not isinstance(zones, list):
        raise ValueError("Workout is not v1: invalid heart-rate zone configuration")
    if zone_mode == "manual":
        try:
            zone_values = [int(value) for value in zones]
        except (TypeError, ValueError) as exc:
            raise ValueError("Workout is not v1: invalid manual HR zones") from exc
        if len(zone_values) != 5 or zone_values != sorted(set(zone_values)):
            raise ValueError("Workout is not v1: manual HR zones must be ascending")


def metadata_path(csv_path: str | Path) -> Path:
    """Return the fixed metadata path for a workout CSV."""
    return WorkoutPaths.from_csv(csv_path).metadata


def save_workout_metadata(
    csv_path: str | Path,
    config: Mapping[str, Any],
    workout_mode: str,
) -> None:
    """Atomically save the processing inputs needed to reproduce a FIT."""
    if workout_mode not in ("free", "planned"):
        raise ValueError("workout_mode must be free or planned")
    missing = [key for key in PROCESSING_CONFIG_KEYS if key not in config]
    if missing:
        raise ValueError(f"Cannot save v1 metadata: configuration missing {missing}")
    path = metadata_path(csv_path)
    payload = {
        "version": METADATA_VERSION,
        "workout_mode": workout_mode,
        "processing_config": {
            key: config[key] for key in PROCESSING_CONFIG_KEYS
        },
    }
    write_atomic_json(path, payload)


def load_workout_metadata(csv_path: str | Path) -> dict[str, Any]:
    """Load and validate a v1 workout metadata sidecar."""
    try:
        with open(metadata_path(csv_path), encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ValueError("Workout is not v1: valid metadata is required") from exc
    if not isinstance(payload, dict) or payload.get("version") != METADATA_VERSION:
        raise ValueError("Workout is not v1: unsupported metadata version")
    if payload.get("workout_mode") not in ("free", "planned"):
        raise ValueError("Workout is not v1: workout_mode must be free or planned")
    saved = payload.get("processing_config")
    if not isinstance(saved, dict):
        raise ValueError("Workout is not v1: processing_config is required")
    missing = [key for key in PROCESSING_CONFIG_KEYS if key not in saved]
    if missing:
        raise ValueError(f"Workout is not v1: metadata missing {missing}")
    _validate_processing_config(saved)
    return payload


def load_workout_config(csv_path: str | Path) -> dict[str, Any]:
    """Load and validate the exact v1 processing configuration."""
    payload = load_workout_metadata(csv_path)
    saved = payload.get("processing_config")
    assert isinstance(saved, dict)
    return dict(saved)
