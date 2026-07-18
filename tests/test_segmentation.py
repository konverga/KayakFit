"""Tests for the v1 ergometer-activity timer domain."""

import pytest

from app.segmentation import LiveSegmenter, detect_segments


def _segments(
    actives: list[int], windows: dict[int, tuple[int, int]] | None = None
):
    windows = windows or {i: (i * 1000, (i + 1) * 1000) for i in range(len(actives))}
    return detect_segments(
        active_col=actives,
        record_windows_ms=windows,
    )


def test_activity_flag_defines_exact_runs() -> None:
    segments = _segments([1, 1, 0, 0, 1, 1])
    assert [(s["start_idx"], s["end_idx"]) for s in segments] == [(0, 1), (4, 5)]


def test_stale_speed_cannot_extend_activity() -> None:
    segments = detect_segments(
        active_col=[1, 1, 0, 0],
        record_windows_ms={i: (i * 1000, (i + 1) * 1000) for i in range(4)},
    )
    assert segments[-1]["end_idx"] == 1


def test_uncovered_gap_breaks_an_active_run() -> None:
    windows = {0: (0, 1000), 1: (1000, 2000), 2: (9000, 10_000), 3: (10_000, 11_000)}
    segments = _segments([1, 1, 1, 1], windows)
    assert [(s["start_idx"], s["end_idx"]) for s in segments] == [(0, 1), (2, 3)]


def test_missing_or_invalid_activity_is_rejected() -> None:
    with pytest.raises((TypeError, ValueError)):
        detect_segments(active_col=None, record_windows_ms={0: (0, 1000)})
    with pytest.raises(ValueError, match="invalid activity"):
        detect_segments(active_col=[None], record_windows_ms={0: (0, 1000)})
    with pytest.raises(ValueError, match="invalid activity"):
        detect_segments(active_col=[True], record_windows_ms={0: (0, 1000)})


def test_live_pause_matches_each_activity_transition() -> None:
    segmenter = LiveSegmenter()
    transitions = []
    for active in [1, 1, 0, 0, 1, 0, 0, 0, 1]:
        state = segmenter.update(active=active)
        if state["transition"]:
            transitions.append(state["transition"])
    assert transitions == ["start", "pause", "resume", "pause", "resume"]


def test_live_missing_activity_is_rejected() -> None:
    with pytest.raises(ValueError, match="activity signal"):
        LiveSegmenter().update(active=None)


def test_live_gap_starts_a_new_lap_without_inflating_time() -> None:
    segmenter = LiveSegmenter()
    assert segmenter.update(active=1)["lap"] == 1
    resumed = segmenter.update(active=1, gap_before_s=30)
    assert resumed == {"paused": False, "lap": 2, "transition": "resume"}
