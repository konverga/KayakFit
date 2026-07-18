"""Live metric-field policy regression tests."""

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

    assert formatted["speed"] == "7.2"
    assert formatted["stroke"] == "60"
    assert formatted["pull"] == "100"
    assert formatted["power"] == "62"
    assert metric_value("speed", packet) == 7.2


def test_pace_and_targets_use_fixed_canonical_fields() -> None:
    packet = _packet()

    assert format_pace(packet, 500) == "04:10"
    assert target_value("spm", packet) == 60.0
    assert target_value("pace_500", packet) == 250.0
    assert target_value("power", packet) == 62.0


def test_finalized_split_formatting_preserves_recorded_precision() -> None:
    assert fmt_time_precise(0.375) == "00:00.375"
    assert fmt_time_precise(16.75) == "00:16.75"
    assert fmt_metres(0.75) == "0.75 m"
    assert fmt_metres(24.0) == "24 m"
