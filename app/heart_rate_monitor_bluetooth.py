"""Standard Bluetooth heart-rate monitor handler.

Built on ``BleDevice`` (retrying connect, auto-reconnect, graceful
disconnect): subscribes to the standard BLE Heart Rate Service (UUID 180D)
measurement notifications, parses the uint8/uint16 heart-rate flag per spec,
tolerates truncated frames from flaky straps, reads the battery level once
after every (re)connect, and delivers each reading through a callback.
Compatible with any heart-rate monitor implementing the standard service.
"""

from collections.abc import Callable
from typing import Any

from bleak.backends.device import BLEDevice

from .ble_device import BleDevice
from .events import StatusPayload

HEART_RATE_SERVICE = "0000180d-0000-1000-8000-00805f9b34fb"
HEART_RATE_MEASUREMENT = "00002a37-0000-1000-8000-00805f9b34fb"
# Standard BLE Battery Service / Battery Level characteristic.
BATTERY_LEVEL = "00002a19-0000-1000-8000-00805f9b34fb"


class HeartRateMonitor(BleDevice):
    """Bluetooth Heart Rate Service monitor handler."""

    NOTIFY_UUID = HEART_RATE_MEASUREMENT
    device_key = "hrm"
    display_name = "HRM"

    def __init__(
        self,
        address: str,
        log_level: str,
        connect_timeout: float = 15.0,
        connect_retries: int = 3,
        auto_reconnect: bool = True,
        reconnect_delay: float = 5.0,
        status_callback: Callable[[StatusPayload], None] | None = None,
        ble_device: BLEDevice | None = None,
    ) -> None:
        """Initialize HRM handler.

        Args:
            address: Bluetooth device address.
            log_level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL).
            connect_timeout: Per-attempt BLE connection timeout in seconds.
            connect_retries: Number of connection attempts before giving up.
            auto_reconnect: Reconnect automatically if the link drops mid-session.
            reconnect_delay: Delay between reconnect cycles in seconds.
            status_callback: Optional callback receiving connection status events
                (dicts with ``event`` and ``device`` keys).
            ble_device: Already-discovered HRM used to avoid an implicit scan.
        """
        super().__init__(
            address=address,
            ble_device=ble_device,
            log_level=log_level,
            connect_timeout=connect_timeout,
            connect_retries=connect_retries,
            auto_reconnect=auto_reconnect,
            reconnect_delay=reconnect_delay,
            status_callback=status_callback,
        )

    async def _after_connect(self) -> None:
        """Read the battery level once after every (re)connect."""
        await self._read_battery_level()

    async def _read_battery_level(self) -> None:
        """Read the strap's battery level once (best-effort; not all expose it)."""
        if self.client is None:
            return
        try:
            raw = await self.client.read_gatt_char(BATTERY_LEVEL)
            if raw:
                level = int(raw[0])
                self.logger.info(msg=f"HRM battery level: {level}%")
                self._emit_status(event="battery", device="hrm", level=level)
        except Exception as e:
            self.logger.debug(msg=f"HRM battery level unavailable: {e}")

    def _on_notify(self, sender: Any, data: bytearray) -> None:
        """Parse heart rate measurement data.

        Heart Rate Measurement format (BLE standard):
        - Byte 0: Flags
            - Bit 0: HR format (0 = uint8, 1 = uint16)
            - Bit 1-2: Sensor contact status
            - Bit 3: Energy expended present
            - Bit 4: RR-Interval present
        - Byte 1+: Heart rate value

        Args:
            sender: Characteristic handle that sent the notification.
            data: Raw heart rate measurement data.
        """
        # Guard against truncated packets from flaky straps: a short read here
        # would raise inside the BLE notification callback.
        if len(data) < 2:
            self.logger.debug(msg=f"Ignoring malformed HR packet: {data.hex()}")
            return
        flags = data[0]
        hr_format = flags & 0x01

        if hr_format == 0:
            heart_rate = data[1]
        else:
            if len(data) < 3:
                self.logger.debug(msg=f"Ignoring malformed HR packet: {data.hex()}")
                return
            heart_rate = int.from_bytes(bytes=data[1:3], byteorder="little")

        self.logger.debug(msg=f"Heart rate: {heart_rate} bpm")

        if self.callback:
            self.callback(heart_rate)
