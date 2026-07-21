"""Live metric-field policy regression tests."""

from gui.dashboard_constants import TILES
from gui.metrics_format import (
    fmt_metres,
    fmt_time_precise,
    format_metrics,
    format_pace,
    metric_value,
    target_value,
)


def _packet() -> dict[str, float]:
    return {
        "speed__mps": 2.0,
        "speed_instant__mps": 4.0,
        "cadence__spm": 60.0,
        "cadence_instant__spm": 90.0,
        "pull_force__n": 100.0,
        "pull_force_instant__n": 200.0,
        "pace_500m__s": 250.0,
        "pace_500m_instant__s": 125.0,
    }


def test_live_metrics_use_fixed_canonical_fields() -> None:
    packet = _packet()

    formatted = format_metrics(packet)

    assert formatted["speed"] == "14.4"
    assert formatted["stroke"] == "90"
    assert formatted["pull"] == "200"
    assert formatted["power"] == "180"
    assert metric_value("speed", packet) == 14.4


def test_dashboard_sensor_cards_are_explicitly_live() -> None:
    labels = {key: label for key, label, _unit, _stats in TILES}

    assert labels["time"] == "Active time"
    assert labels["distance"] == "Active distance"
    for key in ("speed", "pace", "stroke", "hr", "pull", "power"):
        assert labels[key].startswith("Live ")


def test_pace_and_targets_use_fixed_canonical_fields() -> None:
    packet = _packet()

    assert format_pace(packet, 500) == "02:05"
    assert target_value("spm", packet) == 90.0
    assert target_value("pace_500", packet) == 125.0
    assert target_value("power", packet) == 180.0


def test_live_metrics_do_not_fall_back_to_averaged_fields() -> None:
    averaged_only = {
        key: value for key, value in _packet().items() if "instant" not in key
    }

    assert format_metrics(averaged_only) == {}
    assert format_pace(averaged_only, 500) is None
    assert metric_value("speed", averaged_only) is None
    assert target_value("spm", averaged_only) is None
    assert target_value("pace_500", averaged_only) is None
    assert target_value("power", averaged_only) is None


def test_finalized_split_formatting_preserves_recorded_precision() -> None:
    assert fmt_time_precise(0.375) == "00:00.375"
    assert fmt_time_precise(16.75) == "00:16.75"
    assert fmt_metres(0.75) == "0.75 m"
    assert fmt_metres(24.0) == "24 m"
