"""
Verification of the FIT timer-event timeline produced by the exporter.

The test drives ``FitExporter._add_records`` against a synthetic workout
with two autopauses. It asserts that the emitted message stream:

  * contains balanced START / STOP_ALL timer events (one START per segment, one
    STOP_ALL per segment),
  * starts with a manual START and ends with a manual STOP_ALL, with automatic
    pause/resume events in between,
  * adds one metric-free GPS anchor at the start of every active run,
  * brackets every moving segment with START ... STOP_ALL, and
  * is strictly chronological across both events and records.
"""

from fit_tool.profile.profile_type import EventType, TimerTrigger

from app.export_fit import FitExporter
from app.table import Table


def _build_workout():
    """Two pauses → three moving segments. Returns a real Table and the base ts."""
    base = 1_700_000_000_000
    ts, dist, speed = [], [], []
    d = 0.0
    t = 0

    def moving(n):
        nonlocal d, t
        for _ in range(n):
            d += 3.0
            ts.append(base + t)
            dist.append(d)
            speed.append(3.0)
            t += 1000

    def paused(n):
        nonlocal t
        for _ in range(n):
            ts.append(base + t)
            dist.append(d)  # distance frozen
            speed.append(0.0)
            t += 1000

    moving(8)
    paused(6)
    moving(8)
    paused(6)
    moving(8)

    columns = [
        "timestamp", "session_elapsed", "heart_rate", "cadence", "cadence_instant", "distance",
        "speed_instant", "pull_force", "pull_force_instant", "active", "sample_duration",
    ]
    rows = [
        {
            "timestamp": base + (i + 2) * 1000,
            "session_elapsed": i + 2,
            "heart_rate": 120,
            "cadence": 80,
            "cadence_instant": 80,
            "distance": dist[i],
            "speed_instant": speed[i],
            "pull_force": 200,
            "pull_force_instant": 200,
            "active": 1 if speed[i] > 0 else 0,
            # The first sensor record represents two seconds, matching the
            # real regression where Strava previously dropped two seconds.
            "sample_duration": 2 if i == 0 else 1,
        }
        for i in range(len(ts))
    ]
    return Table(columns, rows), base


class RecordingBuilder:
    def __init__(self):
        self.messages = []

    def add(self, message=None):
        self.messages.append(message)


def _make_exporter():
    exporter = FitExporter.__new__(FitExporter)
    exporter.pull_length_m = 0.600

    class _DummyLogger:
        def info(self, msg=""):
            pass

        def debug(self, msg=""):
            pass

        def warning(self, msg=""):
            pass

        def error(self, msg=""):
            pass

    exporter.logger = _DummyLogger()  # type: ignore[assignment]
    return exporter


def _emit_stream():
    df, _base = _build_workout()
    exporter = _make_exporter()
    builder = RecordingBuilder()
    segments = exporter._add_records(builder=builder, workout_df=df)
    return builder.messages, segments


def _classify(msg):
    name = type(msg).__name__
    if name == "EventMessage":
        return ("event", msg.event_type, msg.timer_trigger, msg.timestamp)
    if name == "RecordMessage":
        return ("record", None, None, msg.timestamp)
    return ("other", None, None, getattr(msg, "timestamp", None))


def test_three_segments_detected() -> None:
    _, segments = _emit_stream()
    assert len(segments) == 3, f"expected 3 segments, got {len(segments)}"
    print(f"  ok: two pauses produced {len(segments)} segments")


def test_events_balanced_and_typed() -> None:
    messages, _ = _emit_stream()
    events = [_classify(m) for m in messages if _classify(m)[0] == "event"]
    starts = [e for e in events if e[1] == EventType.START.value]
    stops = [e for e in events if e[1] == EventType.STOP_ALL.value]
    assert len(starts) == 3, f"expected 3 START events, got {len(starts)}"
    assert len(stops) == 3, f"expected 3 STOP_ALL events, got {len(stops)}"

    # First event is the manual workout start; last is the manual workout stop.
    assert events[0][1] == EventType.START.value
    assert events[0][2] == TimerTrigger.MANUAL.value
    assert events[-1][1] == EventType.STOP_ALL.value
    assert events[-1][2] == TimerTrigger.MANUAL.value

    # Internal pause/resume events are automatic.
    for _kind, etype, trigger, _ts in events[1:-1]:
        assert trigger == TimerTrigger.AUTO.value, (
            f"internal event not auto: {(etype, trigger)}"
        )

    # Events strictly alternate start, stop, start, stop, ...
    types_seq = [e[1] for e in events]
    assert types_seq == [
        EventType.START.value, EventType.STOP_ALL.value, EventType.START.value,
        EventType.STOP_ALL.value, EventType.START.value, EventType.STOP_ALL.value,
    ]
    print(f"  ok: balanced 3xSTART / 3xSTOP_ALL, correct triggers: {types_seq}")


def test_segments_bracketed_and_chronological() -> None:
    messages, _ = _emit_stream()
    stream = [_classify(m) for m in messages]

    # Open/close bracket counter: +1 on START, -1 on STOP_ALL. A record may only
    # appear while a segment is open (counter == 1).
    depth = 0
    last_ts = None
    for kind, etype, _trigger, ts in stream:
        if last_ts is not None and ts is not None:
            assert ts >= last_ts, f"non-chronological: {ts} after {last_ts}"
        if ts is not None:
            last_ts = ts
        if kind == "event" and etype == EventType.START.value:
            assert depth == 0, "START while a segment was already open"
            depth += 1
        elif kind == "event" and etype == EventType.STOP_ALL.value:
            assert depth == 1, "STOP_ALL without an open segment"
            depth -= 1
        elif kind == "record":
            assert depth == 1, "record emitted outside an open segment"
    assert depth == 0, "workout ended with an unclosed segment"
    print("  ok: every segment bracketed START..STOP_ALL; stream is chronological")


def test_records_are_sensor_samples_plus_gps_start_anchors() -> None:
    messages, segments = _emit_stream()
    records = [m for m in messages if type(m).__name__ == "RecordMessage"]
    record_count = len(records)
    sensor_count = sum(len(seg.record_indices) for seg in segments)
    expected = sensor_count + len(segments)
    assert record_count == expected, f"{record_count} records, expected {expected}"
    # Three moving blocks of eight plus one compatibility anchor per block.
    assert record_count == 27, f"expected 24 sensor + 3 anchor records, got {record_count}"

    anchors = [record for record in records if record.speed is None]
    assert len(anchors) == len(segments) == 3
    assert [record.timestamp for record in anchors] == [
        segment.start_time_ms for segment in segments
    ]
    assert [record.distance for record in anchors] == [0.0, 24.0, 48.0]


def test_gps_timeline_covers_every_segment_timer_window() -> None:
    """Start anchors prevent Strava from dropping each run's first sample window."""
    messages, segments = _emit_stream()
    records = [m for m in messages if type(m).__name__ == "RecordMessage"]
    for segment in segments:
        segment_records = [
            record
            for record in records
            if segment.start_time_ms <= record.timestamp <= segment.end_time_ms
        ]
        assert segment_records[0].timestamp == segment.start_time_ms
        assert segment_records[-1].timestamp == segment.end_time_ms
        assert (
            segment_records[-1].timestamp - segment_records[0].timestamp
            == segment.timer_time_ms
        )


def test_inactive_tail_excluded_even_with_stale_speed() -> None:
    """The activity flag excludes stale non-zero speed after paddling stops."""
    base = 1_700_000_000_000
    rows = []
    d = 0.0
    for i in range(10):  # moving: distance advances
        d += 3.0
        rows.append({"timestamp": base + (i + 1) * 1000,
                     "session_elapsed": i + 1, "distance": d, "speed_instant": 3.0,
                     "active": 1, "sample_duration": 1})
    for i in range(10, 13):  # frozen distance, speed still non-zero, sub-threshold
        rows.append({"timestamp": base + (i + 1) * 1000,
                     "session_elapsed": i + 1, "distance": d, "speed_instant": 1.2,
                     "active": 0, "sample_duration": 1})
    df = Table(
        ["timestamp", "session_elapsed", "distance", "speed_instant", "active", "sample_duration"],
        rows,
    )

    exporter = _make_exporter()
    builder = RecordingBuilder()
    segments = exporter._add_records(builder=builder, workout_df=df)

    last_moving_ts = base + 10 * 1000
    assert segments[-1].end_time_ms == last_moving_ts, segments[-1]
    record_ts = [
        m.timestamp for m in builder.messages if type(m).__name__ == "RecordMessage"
    ]
    assert record_ts and max(record_ts) == last_moving_ts, max(record_ts)
    stops = [
        m
        for m in builder.messages
        if type(m).__name__ == "EventMessage"
        and m.event_type == EventType.STOP_ALL.value
    ]
    assert stops and stops[-1].timestamp == last_moving_ts
    print("  ok: frozen-distance tail excluded from records, laps and final STOP_ALL")
