"""
Tests for heart-rate zone classification (auto and manual).

metrics_format has no GUI dependency, so it can be imported directly.

Run from the project root:

    python tests/test_hr_zones.py
"""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # metrics_format imports app.power
spec = importlib.util.spec_from_file_location("mf", ROOT / "gui" / "metrics_format.py")
assert spec is not None and spec.loader is not None
mf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mf)


def test_auto_bounds_from_max_hr() -> None:
    bounds = mf.zone_lower_bounds(max_hr=185)
    assert bounds == [0, 111, 130, 148, 166], bounds
    print(f"  ok: auto bounds for max 185 -> {bounds}")


def test_auto_classification() -> None:
    cases = {100: 0, 115: 1, 135: 2, 150: 3, 170: 4}
    for hr, expected in cases.items():
        z = mf.hr_zone(hr, max_hr=185)
        assert z == expected, f"hr {hr} -> Z{z + 1}, expected Z{expected + 1}"
    print("  ok: auto classification across all five zones")


def test_manual_bounds_used() -> None:
    manual = [100, 120, 140, 160, 175]
    assert mf.zone_lower_bounds(max_hr=185, zones=manual) == manual
    # Below the first bound still classifies as Z1.
    assert mf.hr_zone(80, max_hr=185, zones=manual) == 0
    assert mf.hr_zone(130, max_hr=185, zones=manual) == 1
    assert mf.hr_zone(178, max_hr=185, zones=manual) == 4
    print("  ok: manual zones classify by explicit bpm bounds")


def test_invalid_manual_falls_back_to_auto() -> None:
    # Not five values, or not ascending -> ignored in favour of max_hr.
    assert mf.zone_lower_bounds(max_hr=185, zones=[100, 120]) == [0, 111, 130, 148, 166]
    assert mf.zone_lower_bounds(max_hr=185, zones=[100, 90, 140, 160, 175]) == [
        0, 111, 130, 148, 166,
    ]
    print("  ok: invalid manual config falls back to auto")


def test_zone_range_and_invalid_hr() -> None:
    assert mf.zone_range(2, max_hr=185) == (130, 147)
    low, high = mf.zone_range(4, max_hr=185)
    assert high is None and low == 166, (low, high)
    assert mf.hr_zone(None, max_hr=185) == -1
    assert mf.hr_zone("n/a", max_hr=185) == -1
    print("  ok: zone ranges correct; invalid HR returns -1")


def test_target_value_and_status() -> None:
    # HR zone target (returned 1-indexed to match Z1..Z5 in plans).
    assert mf.target_value("hr_zone", {"heart_rate__bpm": 135}, 185) == 3.0
    assert mf.target_status("hr_zone", 3.0, 3, 3) == "in"
    assert mf.target_status("hr_zone", 5.0, 3, 3) == "hard"  # zone too high
    assert mf.target_status("hr_zone", 1.0, 3, 3) == "easy"  # zone too low
    # Pace is inverted: fewer seconds = harder effort.
    assert mf.target_status("pace_500", 138, 130, 145) == "in"
    assert mf.target_status("pace_500", 120, 130, 145) == "hard"
    assert mf.target_status("pace_500", 160, 130, 145) == "easy"
    # Power / spm: higher = harder.
    assert mf.target_status("power", 240, 180, 220) == "hard"
    assert mf.target_status("spm", 50, 58, 64) == "easy"
    # Missing reading -> None.
    assert mf.target_value("hr_zone", {}, 185) is None
    assert mf.target_status("power", None, 180, 220) is None
    print("  ok: target value + status (incl. inverted pace)")


def test_target_text() -> None:
    assert mf.target_text({"metric": "hr_zone", "low": 3, "high": 3}) == "Zone 3"
    assert mf.target_text({"metric": "hr_zone", "low": 2, "high": 3}) == "Zone 2-3"
    assert mf.target_text({"metric": "power", "low": 180, "high": 220}) == "180-220 W"
    assert "/500m" in mf.target_text({"metric": "pace_500", "low": 130, "high": 145})
    assert mf.target_text(None) == ""
    print("  ok: target text labels")
