"""Live metric-field policy regression tests."""

from gui.metrics_format import format_metrics, format_pace, metric_value, target_value


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


def test_live_tiles_and_previews_share_selected_fields() -> None:
    packet = _packet()

    averaged = format_metrics(packet, "averaged")
    instant = format_metrics(packet, "instant")

    assert averaged["speed"] == "7.2"
    assert instant["speed"] == "14.4"
    assert float(averaged["power"]) == metric_value("power", packet, "averaged")
    assert float(instant["power"]) == metric_value("power", packet, "instant")
    assert averaged["stroke"] == "60" and instant["stroke"] == "90"


def test_pace_and_targets_use_selected_fields() -> None:
    packet = _packet()

    assert format_pace(packet, 500, "averaged") == "04:10"
    assert format_pace(packet, 500, "instant") == "02:05"
    assert target_value("spm", packet, data_points="averaged") == 60.0
    assert target_value("spm", packet, data_points="instant") == 90.0
    assert target_value("pace_500", packet, data_points="averaged") == 250.0
    assert target_value("pace_500", packet, data_points="instant") == 125.0
