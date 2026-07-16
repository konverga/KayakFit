"""Reliability contracts for atomic state, configuration, and workers."""

import asyncio
import json
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from app import recovery
from app.atomic_json import write_atomic_json
from app.ble_device import BleDevice
from app.events import UiEvent
from app.worker_result import WorkerResult
from app.workout_session import _packet_timestamp_ms
from gui.config_manager import ConfigError, ConfigManager
from gui.worker_manager import WorkerManager


def test_worker_result_has_one_explicit_terminal_outcome() -> None:
    result = WorkerResult(
        outcome="partial",
        stage="persistence",
        message="disk full",
        csv_path="workout.csv",
        durable_rows=42,
        retryable=True,
    )
    event = result.to_event("workout_result")
    assert event["outcome"] == "partial"
    assert event["success"] is False
    assert event["pending"] is False
    assert event["durable_rows"] == 42


def test_atomic_json_preserves_previous_file_when_replace_fails() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state.json"
        path.write_text('{"version": 1}', encoding="utf-8")
        with (
            patch("app.atomic_json.os.replace", side_effect=OSError("disk error")),
            pytest.raises(OSError, match="disk error"),
        ):
            write_atomic_json(path, {"version": 2})
        assert json.loads(path.read_text(encoding="utf-8")) == {"version": 1}
        assert not path.with_name("state.json.tmp").exists()


def test_recovery_marker_failure_is_not_swallowed() -> None:
    with tempfile.TemporaryDirectory() as tmp, patch.object(
        recovery, "_MARKER_PATH", Path(tmp) / "active.json"
    ), patch(
        "app.recovery.write_atomic_json", side_effect=OSError("no space")
    ), pytest.raises(OSError, match="no space"):
        recovery.mark_active("workout.csv")


def test_config_validation_rejects_invalid_explicit_values() -> None:
    with tempfile.TemporaryDirectory() as tmp, patch.object(Path, "home", return_value=Path(tmp)):
        manager = ConfigManager()
        cfg = dict(manager.DEFAULTS)
        cfg["data_points"] = "smooth"
        with pytest.raises(ConfigError, match="data_points"):
            manager.validate_config(cfg)
        cfg = dict(manager.DEFAULTS)
        cfg["ble_connect_retries"] = 1.5
        with pytest.raises(ConfigError, match="whole number"):
            manager.validate_config(cfg)


def test_config_save_normalizes_platform_string_subclasses() -> None:
    class PlatformString(str):
        pass

    with tempfile.TemporaryDirectory() as tmp, patch.object(Path, "home", return_value=Path(tmp)):
        manager = ConfigManager()
        cfg = dict(manager.DEFAULTS)
        cfg["ergometer_name"] = PlatformString("Kayak ABC")
        cfg["ergometer_mac"] = PlatformString("9CE10062-4038-920C-8FB2-9C8A95AD36EC")

        manager.save_config(cfg)
        loaded = manager.load_config()

        assert loaded["ergometer_name"] == "Kayak ABC"
        assert loaded["ergometer_mac"] == "9CE10062-4038-920C-8FB2-9C8A95AD36EC"
        assert type(loaded["ergometer_mac"]) is str


def test_invalid_config_is_preserved_and_reported() -> None:
    with tempfile.TemporaryDirectory() as tmp, patch.object(Path, "home", return_value=Path(tmp)):
        manager = ConfigManager()
        manager.config_file.write_text("data_points: [broken", encoding="utf-8")
        cfg = manager.load_config()
        assert cfg["data_points"] == "averaged"
        assert manager.config_warning is not None
        assert not manager.config_file.exists()
        assert len(list(manager.config_folder.glob("config.invalid-*.yml"))) == 1


def test_queued_ergometer_packets_follow_source_time_without_overlapping() -> None:
    first = _packet_timestamp_ms(
        {"elapsed_time__s": 1, "window_size__s": 1},
        epoch_anchor_ms=100_000,
        monotonic_anchor=10.0,
        now_monotonic=20.0,
        last_timestamp_ms=None,
    )
    delayed = _packet_timestamp_ms(
        {"elapsed_time__s": 3, "window_size__s": 2},
        epoch_anchor_ms=100_000,
        monotonic_anchor=10.0,
        now_monotonic=20.0,
        last_timestamp_ms=first,
    )
    stalled_counter = _packet_timestamp_ms(
        {"elapsed_time__s": 3, "window_size__s": 1},
        epoch_anchor_ms=100_000,
        monotonic_anchor=10.0,
        now_monotonic=20.0,
        last_timestamp_ms=delayed,
    )

    assert (first, delayed, stalled_counter) == (101_000, 103_000, 104_000)

    reset_after_reconnect = _packet_timestamp_ms(
        {"elapsed_time__s": 1, "window_size__s": 1},
        epoch_anchor_ms=100_000,
        monotonic_anchor=10.0,
        now_monotonic=120.0,
        last_timestamp_ms=200_000,
    )
    assert reset_after_reconnect == 210_000


def test_runtime_config_can_validate_export_without_ergometer() -> None:
    with tempfile.TemporaryDirectory() as tmp, patch.object(Path, "home", return_value=Path(tmp)):
        manager = ConfigManager()
        manager.save_config(dict(manager.DEFAULTS))
        assert manager.load_runtime_config(require_ergometer=False)["ergometer_mac"] == ""
        with pytest.raises(ConfigError, match="No ergometer configured"):
            manager.load_runtime_config()


def test_worker_manager_emits_exactly_one_workout_terminal_result() -> None:
    class _Widget:
        def after(self, _delay: int, callback: Callable[[], None]) -> None:
            callback()

    events: list[UiEvent] = []
    manager = WorkerManager(lambda _message: None, _Widget())
    manager.set_event_callback(events.append)

    async def worker(**_kwargs: Any) -> WorkerResult:
        return WorkerResult(
            outcome="failed", stage="configuration", message="invalid config"
        )

    manager.workout_worker = worker
    manager._run_workout_worker(None)
    terminal = [event for event in events if event["type"] == "workout_result"]
    assert len(terminal) == 1
    assert terminal[0]["outcome"] == "failed"
    assert terminal[0]["success"] is False


def test_ble_open_link_cancellation_releases_partial_client() -> None:
    class _Client:
        is_connected = False

        def __init__(self, **_kwargs: Any) -> None:
            self.disconnected = False

        async def connect(self) -> None:
            raise asyncio.CancelledError

        async def disconnect(self) -> None:
            self.disconnected = True

    created: list[_Client] = []

    def make_client(**kwargs: Any) -> _Client:
        client = _Client(**kwargs)
        created.append(client)
        return client

    device = BleDevice("test-address")
    with (
        patch("app.ble_device.BleakClient", side_effect=make_client),
        pytest.raises(asyncio.CancelledError),
    ):
        asyncio.run(device._open_link())

    assert created[0].disconnected is True
    assert device.client is None
