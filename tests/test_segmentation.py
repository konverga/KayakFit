"""Tests for the v1 ergometer-activity timer domain."""

import pytest

from app.segmentation import LiveSegmenter, detect_segments


def _segments(actives: list[int], timestamps: list[int] | None = None):
    timestamps = timestamps or [i * 1000 for i in range(len(actives))]
    return detect_segments(
        distance_col=None,
        speed_col=None,
        timestamps_ms=timestamps,
        height=len(actives),
        active_col=actives,
    )


def test_activity_flag_defines_exact_runs() -> None:
    segments = _segments([1, 1, 0, 0, 1, 1])
    assert [(s["start_idx"], s["end_idx"]) for s in segments] == [(0, 1), (4, 5)]


def test_stale_speed_cannot_extend_activity() -> None:
    segments = detect_segments(
        distance_col=[3, 6, 6, 6],
        speed_col=[3, 3, 3, 3],
        timestamps_ms=[0, 1000, 2000, 3000],
        height=4,
        active_col=[1, 1, 0, 0],
    )
    assert segments[-1]["end_idx"] == 1


def test_long_timestamp_gap_splits_active_run() -> None:
    segments = _segments([1, 1, 1, 1], [0, 1000, 10_000, 11_000])
    assert [(s["start_idx"], s["end_idx"]) for s in segments] == [(0, 1), (2, 3)]


def test_missing_or_invalid_activity_is_rejected() -> None:
    with pytest.raises(ValueError, match="activity timeline"):
        detect_segments(None, None, [0], 1, active_col=None)
    with pytest.raises(ValueError, match="invalid activity"):
        detect_segments(None, None, [0], 1, active_col=[None])


def test_live_pause_is_debounced_for_stable_ui() -> None:
    segmenter = LiveSegmenter(stationary_threshold=3)
    transitions = []
    distance = 0.0
    for i, active in enumerate([1, 1, 0, 0, 1, 0, 0, 0, 1]):
        if active:
            distance += 3
        state = segmenter.update(
            distance=distance, speed=3 if active else 0,
            timestamp_ms=i * 1000, active=active,
        )
        if state["transition"]:
            transitions.append(state["transition"])
    assert transitions == ["start", "pause", "resume"]


def test_live_missing_activity_is_rejected() -> None:
    with pytest.raises(ValueError, match="activity signal"):
        LiveSegmenter().update(distance=3, speed=3, timestamp_ms=0)
