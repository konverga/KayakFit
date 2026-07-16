"""KayakFirst Bull ergometer BLE handler.

Built on ``BleDevice`` (retrying connect, auto-reconnect, graceful
disconnect): sends the reset / handshake / display-config initialization
sequence, polls for data once per second, and reassembles the
semicolon-separated notification stream into complete lines — several lines
per packet and a line split across packets are both handled, with an overflow
guard and a buffer clear on every reconnect. Parsed rows (distance, speed,
pace, cadence, pull force, …) are delivered through a callback at 1 Hz.
Parsing is defensive: a garbled field drops one line rather than raising
inside the BLE notification callback. A mid-session reconnect deliberately
does not re-run ``initialize()``, which would reset the in-progress workout.

Compatible with KayakFirst Bull (Blue) ergometer firmware.
"""

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from .ble_device import BleDevice
from .events import StatusPayload
from .field_mapping import COLUMN_TYPES, get_field_name

KAYAK_FIRST_SERVICE = "0000ffe0-0000-1000-8000-00805f9b34fb"
KAYAK_FIRST_CHARACTERISTIC = "0000ffe1-0000-1000-8000-00805f9b34fb"

RESET_CMD = "1\r\n"
HANDSHAKE_CMD = "21"
DISPLAY_CONFIG_CMD = "5"
POLL_DATA_CMD = "6\r\n"
START_CMD = "9;1\r\n"
STOP_CMD = "9;3\r\n"


class ErgometerConnectionError(Exception):
    """Raised when ergometer connection fails."""


class KayakFirstErgometer(BleDevice):
    """KayakFirst Bull ergometer handler."""

    NOTIFY_UUID = KAYAK_FIRST_CHARACTERISTIC
    device_key = "ergometer"
    display_name = "Ergometer"

    def __init__(
        self,
        address: str,
        person_weight: int = 75,
        boat_weight: int = 12,
        display_config: list[int] | None = None,
        log_level: str = "INFO",
        connect_timeout: float = 15.0,
        connect_retries: int = 3,
        auto_reconnect: bool = True,
        reconnect_delay: float = 5.0,
        status_callback: Callable[[StatusPayload], None] | None = None,
    ) -> None:
        """Initialize ergometer handler.

        Args:
            address: Bluetooth device address.
            person_weight: Person weight in kg.
            boat_weight: Boat weight in kg.
            display_config: Display configuration slots.
            log_level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL).
            connect_timeout: Per-attempt BLE connection timeout in seconds.
            connect_retries: Number of connection attempts before giving up.
            auto_reconnect: Reconnect automatically if the link drops mid-session.
            reconnect_delay: Delay between reconnect cycles in seconds.
            status_callback: Optional callback receiving connection status events
                (dicts with ``event`` and ``device`` keys).
        """
        super().__init__(
            address=address,
            log_level=log_level,
            connect_timeout=connect_timeout,
            connect_retries=connect_retries,
            auto_reconnect=auto_reconnect,
            reconnect_delay=reconnect_delay,
            status_callback=status_callback,
        )
        self.person_weight = person_weight
        self.boat_weight = boat_weight
        self.display_config = (
            display_config if display_config is not None else [0, 2, 4, 14, 17]
        )
        self.packet_buffer = bytearray()
        self._consecutive_malformed_packets = 0

    async def connect(self, callback: Callable[[dict[str, Any]], None]) -> None:
        """Connect to ergometer.

        Args:
            callback: Function to call with data updates (dict).

        Raises:
            ErgometerConnectionError: If connection fails after all retries.
        """
        try:
            await super().connect(callback=callback)
        except Exception as e:
            self.logger.error(
                msg=f"Failed to connect to ergometer at {self.address}: {e}"
            )
            raise ErgometerConnectionError(str(e)) from e

    def _prepare_link(self) -> None:
        """Reset the packet buffer before a reconnect.

        This prevents a reconnect from ever parsing a half-received packet
        straddling the disconnect.
        Reconnection re-opens the link and re-subscribes to notifications only;
        it deliberately does not re-run ``initialize()``, which would reset the
        in-progress workout.
        """
        self.packet_buffer.clear()

    def _on_notify(self, sender: Any, data: bytearray) -> None:
        r"""Handle incoming data packets from ergometer.

        Data format: '6;field1;field2;...;fieldN\r\n'

        Args:
            sender: Characteristic handle that sent the notification.
            data: Raw data packet.
        """
        self.packet_buffer.extend(data)

        # Guard against unbounded growth if a terminator never arrives (garbled
        # stream / wrong characteristic).
        if len(self.packet_buffer) > 4096:
            self.logger.debug(msg="Packet buffer overflow without terminator; clearing")
            self.packet_buffer.clear()
            return

        # A single notification may carry several complete packets (each ending
        # in CRLF) and/or a trailing partial one. Process every complete line and
        # keep the remainder buffered for the next notification.
        while b"\r\n" in self.packet_buffer:
            line, _, rest = self.packet_buffer.partition(b"\r\n")
            self.packet_buffer = bytearray(rest)

            try:
                packet_str = line.decode(encoding="utf-8").strip()
            except UnicodeDecodeError as e:
                self.logger.debug(msg=f"Error decoding packet: {e}")
                continue

            if packet_str.startswith("6"):
                parsed_data = self._parse_data_packet(packet=packet_str)
                if parsed_data is None:
                    self._consecutive_malformed_packets += 1
                    if self._consecutive_malformed_packets == 5:
                        self.logger.warning(
                            msg="Five consecutive malformed ergometer packets"
                        )
                        self._emit_status(
                            event="malformed_data",
                            device="ergometer",
                            count=self._consecutive_malformed_packets,
                        )
                    continue
                self._consecutive_malformed_packets = 0
                if self.callback:
                    self.callback(parsed_data)

    @staticmethod
    def _display_config_list_to_str(lst: list[int]) -> str:
        """Convert display config list to semicolon-separated string.

        Args:
            lst: List of display configuration values.

        Returns:
            Semicolon-separated string of values.

        Raises:
            ValueError: If list length is invalid or contains invalid values.
        """
        allowed = {0, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 17, 18}

        if not (1 <= len(lst) <= 5):
            raise ValueError("List must contain between 1 and 5 items.")

        invalid = set(lst) - allowed
        if invalid:
            raise ValueError(f"Invalid values in list: {sorted(invalid)}")

        return ";".join(str(x) for x in lst)

    def _parse_data_packet(self, packet: str) -> dict[str, Any] | None:
        """Parse data stream packet and return ALL fields.

        KayakFirst data format (semicolon-separated):
        Returns all fields from the packet with known field names where available
        and col_x for unknown fields (only proceed with first row of the packet).

        Args:
            packet: Raw packet string.

        Returns:
            Dictionary of all parsed values or None.
        """
        self.logger.debug(msg=f"Raw packet: {packet}")
        parts = packet.split(";")

        if len(parts) != 24 or parts[0] != "6":
            return None

        data = {}
        try:
            parsed: list[int | Decimal] = []
            for i, part in enumerate(parts[:24]):
                if i == 0:
                    continue
                elif "." in part:
                    parsed.append(Decimal(value=part))
                else:
                    parsed.append(int(part))

            data = {
                get_field_name(index=i): value
                for i, value in enumerate(iterable=parsed, start=1)
            }
            for field, value in data.items():
                expected = COLUMN_TYPES.get(field)
                if expected == "int" and not isinstance(value, int):
                    return None
                if expected == "decimal" and not isinstance(value, (int, Decimal)):
                    return None
            if data["active_paddling"] not in (0, 1):
                return None
            if int(data["window_size__s"]) <= 0:
                return None
            if int(data["elapsed_time__s"]) < 0:
                return None
            if Decimal(data["distance__m"]) < 0:
                return None
            self.logger.debug(msg=f"Parsed data: {data}")
            return data
        except (ValueError, IndexError, InvalidOperation) as e:
            # InvalidOperation: Decimal() rejects garbled fields like "12.3.4"
            # and is NOT a ValueError — without it a corrupt packet would
            # crash the BLE notification callback.
            self.logger.debug(msg=f"Error parsing data values: {e}")
            return None

    async def _send_command(self, command: str) -> None:
        """Send command to ergometer.

        Commands are sent in chunks to respect MTU size.

        Args:
            command: Command string to send.
        """
        if not self.client or not self.client.is_connected:
            raise ConnectionError("Cannot send command: ergometer is not connected")

        try:
            command_bytes = command.encode(encoding="utf-8")
            mtu_size = 20

            for i in range(0, len(command_bytes), mtu_size):
                chunk = command_bytes[i : i + mtu_size]
                await self.client.write_gatt_char(
                    char_specifier=KAYAK_FIRST_CHARACTERISTIC,
                    data=chunk,
                    response=False,
                )
                await asyncio.sleep(delay=0.05)

            self.logger.debug(msg=f"Sent command: {command.strip()}")

        except Exception as e:
            self.logger.error(msg=f"Error sending command: {e}")
            raise

    async def initialize(self) -> None:
        """Initialize ergometer with handshake and configuration.

        Initialization sequence:
        1. Reset (twice)
        2. Handshake with time, timezone, person weight and boat weight
        3. Display configuration
        """
        self.logger.info(msg="Initializing KayakFirst Bull...")

        self.logger.info(msg="Sending reset command...")
        await self._send_command(command=RESET_CMD)
        await asyncio.sleep(delay=2)

        self.logger.info(msg="Sending second reset...")
        await self._send_command(command=RESET_CMD)
        await asyncio.sleep(delay=3)

        now = datetime.now()
        unix_time = int(now.timestamp())
        offset = now.astimezone().utcoffset() or timedelta(0)
        tz_offset = int(offset.total_seconds() / 60)

        handshake = (
            f"{HANDSHAKE_CMD};{unix_time};{tz_offset};"
            f"{self.person_weight};{self.boat_weight}\r\n"
        )
        self.logger.info(
            msg=f"Sending handshake (person: {self.person_weight}kg, boat: {self.boat_weight}kg)..."
        )
        await self._send_command(command=handshake)
        await asyncio.sleep(delay=3)

        display_config = (
            f"{DISPLAY_CONFIG_CMD};"
            f"{self._display_config_list_to_str(lst=self.display_config)}\r\n"
        )
        self.logger.info(msg="Sending display configuration...")
        await self._send_command(command=display_config)
        await asyncio.sleep(delay=3)

        self.logger.info(msg="Initialization complete")

    async def start_workout(self) -> None:
        """Start workout session on ergometer."""
        self.logger.info(msg="Starting workout...")
        await self._send_command(command=START_CMD)
        await asyncio.sleep(delay=1)

    async def stop_workout(self) -> None:
        """Stop workout session on ergometer."""
        self.logger.debug(msg="Stopping workout...")
        await self._send_command(command=STOP_CMD)
        # Reduced sleep for faster shutdown
        await asyncio.sleep(delay=0.2)

    async def poll_data(self) -> None:
        """Continuously poll ergometer for data.

        This should be run as a background task. Exits on its own once
        ``disconnect()`` clears the run flag (it is normally also cancelled).
        """
        while self._should_run:
            if self.client and self.client.is_connected:
                try:
                    await self._send_command(command=POLL_DATA_CMD)
                except Exception as e:
                    # A single failed poll is recoverable; required lifecycle
                    # commands propagate, while the BLE reconnect/watchdog path
                    # owns transient streaming failures.
                    self.logger.warning(msg=f"Ergometer poll failed: {e}")
            await asyncio.sleep(delay=1)
