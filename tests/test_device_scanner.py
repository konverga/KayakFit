"""Tests for platform-independent BLE device-name matching."""

import asyncio
from types import SimpleNamespace

from bleak import BleakScanner

from gui import device_scanner
from gui.setup_wizard import _save_devices


def _device(name: str | None, address: str) -> SimpleNamespace:
    return SimpleNamespace(name=name, address=address)


def _advertisement(
    local_name: str | None, service_uuids: list[str] | None = None
) -> SimpleNamespace:
    return SimpleNamespace(local_name=local_name, service_uuids=service_uuids or [])


def test_ergometer_matches_advertised_name(monkeypatch) -> None:
    class PlatformAddress(str):
        pass

    async def discover(**_kwargs):
        return {
            "erg": (
                _device("Generic BLE Device", PlatformAddress("ERG-ADDRESS")),
                _advertisement("KF-BOAT-42"),
            )
        }

    monkeypatch.setattr(BleakScanner, "discover", discover)

    result = asyncio.run(device_scanner._async_scan("boat-42"))

    assert result["erg_devices"] == [("KF-BOAT-42", "ERG-ADDRESS")]
    assert type(result["erg_devices"][0][1]) is str


def test_ergometer_matches_platform_device_name(monkeypatch) -> None:
    async def discover(**_kwargs):
        return {
            "erg": (
                _device("Kayak-ABC", "ERG-ADDRESS"),
                _advertisement(None),
            )
        }

    monkeypatch.setattr(BleakScanner, "discover", discover)

    result = asyncio.run(device_scanner._async_scan("KAYAK-abc"))

    assert result["erg_devices"] == [("Kayak-ABC", "ERG-ADDRESS")]


def test_heart_rate_service_remains_authoritative(monkeypatch) -> None:
    async def discover(**_kwargs):
        return {
            "hrm": (
                _device("Kayak-ABC HRM", "HRM-ADDRESS"),
                _advertisement(None, [device_scanner.HEART_RATE_SERVICE_UUID.upper()]),
            )
        }

    monkeypatch.setattr(BleakScanner, "discover", discover)

    result = asyncio.run(device_scanner._async_scan("kayak-abc"))

    assert result["erg_devices"] == []
    assert result["hrm_devices"] == [("Kayak-ABC HRM", "HRM-ADDRESS")]


def test_wizard_saves_selected_device_values() -> None:
    cfg: dict[str, object] = {}
    erg_map = {"Kayak ABC (ERG-ID)": ("Kayak ABC", "ERG-ID")}
    hrm_map = {"Polar H10 (HRM-ID)": ("Polar H10", "HRM-ID")}

    _save_devices(
        cfg,
        erg_map,
        "Kayak ABC (ERG-ID)",
        hrm_map,
        "Polar H10 (HRM-ID)",
    )

    assert cfg == {
        "ergometer_name": "Kayak ABC",
        "ergometer_mac": "ERG-ID",
        "hrm_name": "Polar H10",
        "hrm_mac": "HRM-ID",
    }


def test_wizard_requires_an_ergometer_selection() -> None:
    cfg: dict[str, object] = {}

    try:
        _save_devices(cfg, {}, "— scan to find devices —", {}, "None (no heart-rate monitor)")
    except ValueError as exc:
        assert "Select an ergometer" in str(exc)
    else:
        raise AssertionError("expected setup to reject a missing ergometer selection")
