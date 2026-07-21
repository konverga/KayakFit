"""
Tests for ergometer notification parsing (CRLF line framing).

Verifies that a single BLE notification carrying multiple complete packets is
fully parsed, that a packet split across two notifications is reassembled, and
that the buffer cannot grow without bound.
"""

from typing import Any

from app.kayakfirst_ergometer_bluetooth import KayakFirstErgometer


def _make_erg():
    erg = KayakFirstErgometer.__new__(KayakFirstErgometer)
    erg.packet_buffer = bytearray()
    erg._consecutive_malformed_packets = 0
    erg.status_callback = None
    received: list[Any] = []
    erg.callback = received.append

    class _Logger:
        def debug(self, msg=""):
            pass

        def warning(self, msg=""):
            pass

    erg.logger = _Logger()  # type: ignore[assignment]
    return erg, received


def _packet(marker: int) -> bytes:
    values = [0] * 23
    values[0] = marker
    values[6] = 1
    values[8] = marker * 3
    values[21] = marker
    values[22] = 1
    return ("6;" + ";".join(str(value) for value in values)).encode()


def test_multiple_packets_in_one_notification() -> None:
    erg, received = _make_erg()
    erg._on_notify(
        None,
        bytearray(b"\r\n".join((_packet(1), _packet(2), _packet(3))) + b"\r\n"),
    )
    assert len(received) == 3, f"expected 3 packets, got {len(received)}"
    assert erg.packet_buffer == bytearray(), "buffer should be empty"
    print("  ok: 3 concatenated packets all parsed")


def test_packet_split_across_notifications() -> None:
    erg, received = _make_erg()
    packet = _packet(7)
    erg._on_notify(None, bytearray(packet[:20]))
    assert received == [], "no complete packet yet"
    erg._on_notify(None, bytearray(packet[20:] + b"\r\n"))
    assert len(received) == 1, f"expected 1 reassembled packet, got {len(received)}"
    print("  ok: packet split across two notifications reassembled")


def test_trailing_partial_kept() -> None:
    erg, received = _make_erg()
    partial = _packet(3)[:12]
    erg._on_notify(None, bytearray(_packet(1) + b"\r\n" + partial))
    assert len(received) == 1, "one complete packet"
    assert erg.packet_buffer == bytearray(partial), "partial remainder kept"
    print("  ok: complete packet parsed, partial remainder buffered")


def test_buffer_overflow_guard() -> None:
    erg, _received = _make_erg()
    erg._on_notify(None, bytearray(b"6" * 5000))  # no terminator
    assert erg.packet_buffer == bytearray(), "oversized buffer should be cleared"
    print("  ok: buffer overflow without terminator is cleared")


def test_incomplete_and_semantically_invalid_packets_are_rejected() -> None:
    erg, received = _make_erg()
    erg._on_notify(None, bytearray(b"6;1;2\r\n"))
    invalid_active = _packet(1).decode().split(";")
    invalid_active[7] = "2"
    erg._on_notify(None, bytearray((";".join(invalid_active) + "\r\n").encode()))
    invalid_window = _packet(2).decode().split(";")
    invalid_window[23] = "0"
    erg._on_notify(None, bytearray((";".join(invalid_window) + "\r\n").encode()))
    decimal_integer = _packet(3).decode().split(";")
    decimal_integer[7] = "1.0"
    erg._on_notify(None, bytearray((";".join(decimal_integer) + "\r\n").encode()))
    assert received == []
    assert erg._consecutive_malformed_packets == 4


def test_five_malformed_packets_emit_device_health_event() -> None:
    erg, _received = _make_erg()
    events: list[dict[str, Any]] = []
    erg.status_callback = events.append
    for _ in range(5):
        erg._on_notify(None, bytearray(b"6;1;2\r\n"))
    assert events == [
        {"event": "malformed_data", "device": "ergometer", "count": 5}
    ]
