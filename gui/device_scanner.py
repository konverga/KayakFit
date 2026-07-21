"""Bluetooth Low Energy device discovery for KayakFit.

Intentionally UI-toolkit agnostic: the BLE scan runs on a background thread
and reports results and log messages through callbacks that the caller
marshals onto the GUI thread. It does not touch any widgets directly.
"""

import asyncio
import contextlib
import threading
from collections.abc import Callable
from typing import Any

from bleak import BleakClient, BleakScanner

ScanResult = dict[str, Any]

# Standard BLE Heart Rate Service. Any compliant strap (Garmin, Polar, Wahoo,
# COROS, …) advertises this UUID, so we identify HRMs by service rather than by
# device name — no name pattern needed.
HEART_RATE_SERVICE_UUID = "0000180d-0000-1000-8000-00805f9b34fb"


def test_connection(
    address: str,
    on_log: Callable[[str], None],
    on_result: Callable[[dict[str, Any]], None],
    schedule: Callable[[Callable[[], None]], None],
    timeout: float = 15.0,
) -> threading.Thread:
    """Briefly connect to a device to confirm it is reachable, then disconnect.

    Runs on a background thread and reports the outcome through callbacks the
    caller marshals onto the GUI thread.

    Args:
        address: Bluetooth MAC/UUID to test.
        on_log: Called with progress messages (on the GUI thread).
        on_result: Called once with ``{"ok": bool, "error": str|None}``.
        schedule: Marshals a callable onto the GUI thread (e.g. ``window.after``).
        timeout: Connection timeout in seconds.

    Returns:
        The started daemon thread.
    """

    def run() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result: dict[str, Any] = {}
        try:
            result = loop.run_until_complete(_async_test(address=address, timeout=timeout))
        except Exception as e:
            result = {"ok": False, "error": str(e)}
        finally:
            _drain_loop(loop)
        schedule(lambda: on_result(result))

    schedule(lambda: on_log(f"Testing connection to {address}…\n"))
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


async def _async_test(address: str, timeout: float) -> dict[str, Any]:
    """Connect and immediately disconnect, reporting whether the link came up."""
    client = BleakClient(address_or_ble_device=address, timeout=timeout)
    try:
        await client.connect()
        ok = bool(client.is_connected)
    finally:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(client.disconnect(), timeout=2.0)
    return {"ok": ok, "error": None if ok else "Device did not connect"}


def scan_devices(
    erg_pattern: str,
    on_log: Callable[[str], None],
    on_result: Callable[[ScanResult], None],
    schedule: Callable[[Callable[[], None]], None],
    stop_event: threading.Event | None = None,
) -> threading.Thread:
    """Scan for Bluetooth devices on a background thread.

    Ergometers are matched by name (they are not a standard BLE profile); heart
    rate monitors are detected by the advertised Heart Rate Service UUID, so no
    HRM name pattern is required.

    Args:
        erg_pattern: Substring to match ergometer device names (case-insensitive).
        on_log: Called with human-readable progress messages (on the GUI thread).
        on_result: Called once with the final result dict (on the GUI thread).
        schedule: Marshals a callable onto the GUI thread (e.g. ``window.after``).
        stop_event: Optional event to cancel the scan early.

    Returns:
        The started scanner thread (daemon).

    The result dict has one of these shapes:
        {"erg_devices": [(name, addr), ...], "hrm_devices": [...], "total": int}
        {"cancelled": True}
        {"error": "..."}
    """

    def run_scan() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result: ScanResult = {}
        try:
            if stop_event and stop_event.is_set():
                result = {"cancelled": True}
            else:
                result = loop.run_until_complete(
                    _async_scan(
                        erg_pattern=erg_pattern,
                        stop_event=stop_event,
                    )
                )
        except Exception as e:
            result = {"error": str(e)}
        finally:
            _drain_loop(loop)

        schedule(lambda: on_result(result))

    schedule(lambda: on_log("Scanning for Bluetooth devices (~10s)…\n"))
    scan_thread = threading.Thread(target=run_scan, daemon=True)
    scan_thread.start()
    return scan_thread


def _drain_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Cancel pending tasks and close the event loop without raising."""
    try:
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            with contextlib.suppress(asyncio.TimeoutError, Exception):
                loop.run_until_complete(
                    asyncio.wait_for(
                        asyncio.gather(*pending, return_exceptions=True),
                        timeout=0.5,
                    )
                )
        loop.close()
    except Exception:
        pass


async def _async_scan(
    erg_pattern: str,
    stop_event: threading.Event | None = None,
) -> ScanResult:
    """Discover BLE devices, matching ergometers by name and HRMs by service."""
    if stop_event and stop_event.is_set():
        return {"cancelled": True}

    erg_pattern = erg_pattern.strip().casefold()

    # return_adv=True gives us the advertisement data, which carries the list of
    # advertised service UUIDs used to recognise heart-rate monitors.
    discovered = await BleakScanner.discover(timeout=10.0, return_adv=True)

    if stop_event and stop_event.is_set():
        return {"cancelled": True}

    erg_devices: list[tuple[str, str]] = []
    hrm_devices: list[tuple[str, str]] = []

    for device, adv in discovered.values():
        # Bleak does not expose the human-readable name consistently across
        # platforms. On some systems it is only present in the advertisement;
        # on others ``device.name`` is populated instead. Match both so the
        # name printed on the ergometer works everywhere.
        device_name = str(device.name or "").strip()
        advertised_name = str(adv.local_name or "").strip()
        name = advertised_name or device_name or "Unknown"
        # CoreBluetooth may return an NSString/UUID proxy (or a ``str``
        # subclass) that looks like text but cannot be serialized by PyYAML.
        # Convert it at the BLE boundary so configuration only sees built-ins.
        address = str(device.address)
        service_uuids = [u.lower() for u in (adv.service_uuids or [])]

        if HEART_RATE_SERVICE_UUID in service_uuids:
            hrm_devices.append((name, address))
        elif erg_pattern and any(
            erg_pattern in candidate.casefold()
            for candidate in (advertised_name, device_name)
        ):
            erg_devices.append((name, address))

    return {
        "erg_devices": erg_devices,
        "hrm_devices": hrm_devices,
        "total": len(discovered),
    }
