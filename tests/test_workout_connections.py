"""Regression coverage for workout-start BLE connection ordering."""

import asyncio
from typing import Any, cast
from unittest.mock import patch

import pytest
from bleak import BleakScanner
from bleak.backends.device import BLEDevice

from app import workout_session as workout_session_mod
from app.ble_device import BleDevice
from app.events import StatusPayload
from app.workout_session import WorkoutAbortedError, WorkoutSession
from gui.config_manager import ConfigManager


def _config(*, hrm_enabled: str = "yes") -> dict[str, Any]:
    cfg = dict(ConfigManager.DEFAULTS)
    cfg.update(
        {
            "ergometer_mac": "ERG-ADDRESS",
            "hrm_mac": "HRM-ADDRESS",
            "hrm_enabled": hrm_enabled,
            "log_level": "error",
        }
    )
    return cfg


def _resolved_device() -> BLEDevice:
    return cast(BLEDevice, object())


def test_ble_device_uses_resolved_device() -> None:
    resolved = _resolved_device()
    received_target: list[object] = []

    class _Client:
        is_connected = True

        def __init__(self, *, address_or_ble_device: object, **_kwargs: Any) -> None:
            received_target.append(address_or_ble_device)

        async def connect(self) -> None:
            pass

        async def start_notify(self, **_kwargs: Any) -> None:
            pass

        async def disconnect(self) -> None:
            pass

    device = BleDevice("HRM-ADDRESS", ble_device=resolved)
    with patch("app.ble_device.BleakClient", _Client):
        asyncio.run(device._open_link())

    assert received_target == [resolved]


def test_hrm_is_discovered_before_ergometer_connects(monkeypatch) -> None:
    order: list[str] = []
    resolved = _resolved_device()
    received_hrm_device: list[BLEDevice | None] = []

    async def find_device(*_args: Any, **_kwargs: Any) -> BLEDevice:
        order.append("discover_hrm")
        return resolved

    class _Ergometer:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def connect(self, **_kwargs: Any) -> None:
            order.append("connect_ergometer")

    class _Hrm:
        def __init__(self, *, ble_device: BLEDevice | None = None, **_kwargs: Any) -> None:
            received_hrm_device.append(ble_device)

        async def connect(self, **_kwargs: Any) -> None:
            order.append("connect_hrm")

    monkeypatch.setattr(
        BleakScanner, "find_device_by_address", find_device
    )
    monkeypatch.setattr(workout_session_mod, "KayakFirstErgometer", _Ergometer)
    monkeypatch.setattr(workout_session_mod, "HeartRateMonitor", _Hrm)

    session = WorkoutSession(_config(), prompt_callback=lambda *_args: False)
    asyncio.run(session._connect_devices())

    assert order == ["discover_hrm", "connect_ergometer", "connect_hrm"]
    assert received_hrm_device == [resolved]


def test_hrm_discovery_retries_once(monkeypatch) -> None:
    resolved = _resolved_device()
    results: list[BLEDevice | None] = [None, resolved]

    async def find_device(*_args: Any, **_kwargs: Any) -> BLEDevice | None:
        return results.pop(0)

    monkeypatch.setattr(
        BleakScanner, "find_device_by_address", find_device
    )
    monkeypatch.setattr(workout_session_mod, "_HRM_DISCOVERY_RETRY_DELAY_S", 0.0)

    session = WorkoutSession(_config())
    found = asyncio.run(session._resolve_hrm_device("HRM-ADDRESS"))

    assert found is resolved
    assert results == []


def test_missing_hrm_can_continue(monkeypatch) -> None:
    statuses: list[StatusPayload] = []
    ergometer_connected: list[bool] = []

    async def find_device(*_args: Any, **_kwargs: Any) -> None:
        return None

    class _Ergometer:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def connect(self, **_kwargs: Any) -> None:
            ergometer_connected.append(True)

    class _UnexpectedHrm:
        def __init__(self, **_kwargs: Any) -> None:
            raise AssertionError("HRM must not be created when discovery failed")

    monkeypatch.setattr(
        BleakScanner, "find_device_by_address", find_device
    )
    monkeypatch.setattr(workout_session_mod, "_HRM_DISCOVERY_RETRY_DELAY_S", 0.0)
    monkeypatch.setattr(workout_session_mod, "KayakFirstErgometer", _Ergometer)
    monkeypatch.setattr(workout_session_mod, "HeartRateMonitor", _UnexpectedHrm)

    session = WorkoutSession(
        _config(),
        status_callback=statuses.append,
        prompt_callback=lambda *_args: True,
    )
    asyncio.run(session._connect_devices())

    assert ergometer_connected == [True]
    assert session.hrm is None
    assert {"event": "absent", "device": "hrm"} in statuses


def test_missing_hrm_can_cancel_before_ergometer_connects(monkeypatch) -> None:
    async def find_device(*_args: Any, **_kwargs: Any) -> None:
        return None

    class _UnexpectedErgometer:
        def __init__(self, **_kwargs: Any) -> None:
            raise AssertionError("ergometer must not be created after HRM cancellation")

    monkeypatch.setattr(
        BleakScanner, "find_device_by_address", find_device
    )
    monkeypatch.setattr(workout_session_mod, "_HRM_DISCOVERY_RETRY_DELAY_S", 0.0)
    monkeypatch.setattr(
        workout_session_mod, "KayakFirstErgometer", _UnexpectedErgometer
    )

    session = WorkoutSession(_config(), prompt_callback=lambda *_args: False)
    with pytest.raises(WorkoutAbortedError, match="heart-rate monitor not reachable"):
        asyncio.run(session._connect_devices())


def test_disabled_hrm_does_not_scan(monkeypatch) -> None:
    ergometer_connected: list[bool] = []

    async def unexpected_scan(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("disabled HRM must not trigger discovery")

    class _Ergometer:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def connect(self, **_kwargs: Any) -> None:
            ergometer_connected.append(True)

    monkeypatch.setattr(
        BleakScanner, "find_device_by_address", unexpected_scan
    )
    monkeypatch.setattr(workout_session_mod, "KayakFirstErgometer", _Ergometer)

    session = WorkoutSession(_config(hrm_enabled="no"))
    asyncio.run(session._connect_devices())

    assert ergometer_connected == [True]


def test_stop_cancels_hrm_discovery(monkeypatch) -> None:
    async def scenario() -> None:
        scan_started = asyncio.Event()

        async def blocked_scan(*_args: Any, **_kwargs: Any) -> None:
            scan_started.set()
            await asyncio.Future()

        class _UnexpectedErgometer:
            def __init__(self, **_kwargs: Any) -> None:
                raise AssertionError("ergometer must not be created after Stop")

        monkeypatch.setattr(
            BleakScanner,
            "find_device_by_address",
            blocked_scan,
        )
        monkeypatch.setattr(
            workout_session_mod, "KayakFirstErgometer", _UnexpectedErgometer
        )

        session = WorkoutSession(_config())
        task = asyncio.create_task(
            session._run_until_stop(
                session._connect_devices(), "connecting to devices"
            )
        )
        await scan_started.wait()
        session.stop_event.set()

        with pytest.raises(WorkoutAbortedError, match="connecting to devices"):
            await task

    asyncio.run(scenario())
