"""Shared BLE device plumbing.

Base class for the ergometer and heart-rate monitor handlers: retrying
connection with exponential backoff, notification subscription, automatic
mid-session reconnect, status-event emission, and graceful disconnect.

Subclasses set ``NOTIFY_UUID``, ``device_key`` (status-event identifier) and
``display_name`` (log text), and implement ``_on_notify``. Optional hooks:
``_prepare_link`` runs before every connection attempt, ``_after_connect``
runs after notifications start (also on reconnect).
"""

import asyncio
import contextlib
from collections.abc import Callable
from typing import Any, cast

from bleak import BleakClient
from bleak.backends.device import BLEDevice

from .events import StatusPayload
from .logger import Logger


class BleDevice:
    """A BLE peripheral with retrying connect and auto-reconnect."""

    NOTIFY_UUID: str = ""
    device_key: str = "device"  # status-event identifier (e.g. "ergometer")
    display_name: str = "Device"  # human-readable, used in log lines

    def __init__(
        self,
        address: str,
        log_level: str = "INFO",
        connect_timeout: float = 15.0,
        connect_retries: int = 3,
        auto_reconnect: bool = True,
        reconnect_delay: float = 5.0,
        status_callback: Callable[[StatusPayload], None] | None = None,
        ble_device: BLEDevice | None = None,
    ) -> None:
        """Initialize the device handler.

        Args:
            address: Bluetooth device address.
            log_level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL).
            connect_timeout: Per-attempt BLE connection timeout in seconds.
            connect_retries: Number of connection attempts before giving up.
            auto_reconnect: Reconnect automatically if the link drops mid-session.
            reconnect_delay: Delay between reconnect cycles in seconds.
            status_callback: Optional callback receiving connection status events
                (dicts with ``event`` and ``device`` keys).
            ble_device: Already-discovered device. Supplying this avoids Bleak's
                implicit address lookup when opening the connection.
        """
        self.address = address
        self._ble_device = ble_device
        self.log_level = log_level
        self.connect_timeout = connect_timeout
        self.connect_retries = max(1, connect_retries)
        self.auto_reconnect = auto_reconnect
        self.reconnect_delay = reconnect_delay
        self.status_callback = status_callback

        self.client: BleakClient | None = None
        self.callback: Callable[..., Any] | None = None

        self._should_run = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._reconnect_task: asyncio.Task[None] | None = None

        self.logger = Logger.get_logger(name=type(self).__module__)

    # ---- hooks ---------------------------------------------------------
    def _prepare_link(self) -> None:
        """Called before every connection attempt (e.g. reset parse buffers)."""

    async def _after_connect(self) -> None:
        """Called after notifications start, on connect and reconnect."""

    def _on_notify(self, sender: Any, data: bytearray) -> None:
        """Handle an incoming notification. Must be implemented by subclasses."""
        raise NotImplementedError

    # ---- status --------------------------------------------------------
    def _emit_status(self, event: str, device: str, **payload: Any) -> None:
        """Send a connection status event to the UI if a callback is registered."""
        if self.status_callback:
            # Never let UI plumbing break the BLE link.
            with contextlib.suppress(Exception):
                self.status_callback(
                    cast(StatusPayload, {"event": event, "device": device, **payload})
                )

    # ---- connection ----------------------------------------------------
    async def connect(self, callback: Callable[..., Any]) -> None:
        """Connect and start receiving data.

        Retries with exponential backoff and, once connected, automatically
        reconnects if the link drops mid-session (when ``auto_reconnect`` is
        set).

        Args:
            callback: Function to call with data updates.

        Raises:
            Exception: The last connection error if all attempts fail.
        """
        self.callback = callback
        self._should_run = True
        self._loop = asyncio.get_running_loop()

        last_error: Exception | None = None
        for attempt in range(1, self.connect_retries + 1):
            if not self._should_run:
                return
            try:
                await self._open_link()
                if not self._should_run:
                    # Stop was requested while the link was opening — don't
                    # leave a connected device streaming into a dead session.
                    await self.disconnect()
                    return
                self.logger.info(
                    msg=f"Connected to {self.display_name} at {self.address}"
                )
                await self._after_connect()
                return
            except Exception as e:
                last_error = e
                self.logger.warning(
                    msg=f"{self.display_name} connect attempt "
                    f"{attempt}/{self.connect_retries} failed: {e}"
                )
                if attempt < self.connect_retries and self._should_run:
                    await asyncio.sleep(delay=min(2**attempt, 10))

        assert last_error is not None
        raise last_error

    async def _open_link(self) -> None:
        """Open the BLE link and subscribe to notifications (single attempt)."""
        self._prepare_link()
        # Close any previous (half-open) link first: a failed start_notify or
        # an unnoticed drop can leave the old client connected, and adapters
        # commonly refuse a second connection to the same device. Detach it
        # from ``self.client`` before disconnecting so its disconnected
        # callback is recognized as stale and doesn't trigger a reconnect.
        old, self.client = self.client, None
        if old is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(old.disconnect(), timeout=2.0)
        client = BleakClient(
            address_or_ble_device=(
                self._ble_device if self._ble_device is not None else self.address
            ),
            timeout=self.connect_timeout,
            disconnected_callback=self._handle_disconnect,
        )
        self.client = client
        try:
            await client.connect()
            await client.start_notify(
                char_specifier=self.NOTIFY_UUID,
                callback=self._on_notify,
            )
        except BaseException:
            # Cancellation can arrive while connect() or start_notify() is in
            # flight. Always give the backend a chance to release its adapter
            # resources, even if it does not yet report itself as connected.
            if self.client is client:
                self.client = None
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(client.disconnect(), timeout=2.0)
            raise

    # ---- auto-reconnect --------------------------------------------------
    def _handle_disconnect(self, client: BleakClient) -> None:
        """Bleak disconnected callback - schedule a reconnect if unexpected.

        Runs in the BLE event-loop thread, so the reconnect is scheduled back
        onto the session loop in a thread-safe way.
        """
        if client is not self.client:
            return  # stale callback from a superseded connection attempt
        if not self._should_run or not self.auto_reconnect:
            return
        self.logger.warning(
            msg=f"{self.display_name} connection lost - attempting to reconnect..."
        )
        self._emit_status(event="reconnecting", device=self.device_key)
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._schedule_reconnect)

    def _schedule_reconnect(self) -> None:
        """Start the reconnect loop if one is not already running."""
        if self._reconnect_task and not self._reconnect_task.done():
            return
        if self._loop is None:
            return
        self._reconnect_task = self._loop.create_task(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        """Keep retrying the link until it succeeds or shutdown is requested."""
        while self._should_run:
            try:
                await self._open_link()
                await self._after_connect()
                self.logger.info(msg=f"{self.display_name} reconnected")
                self._emit_status(event="connected", device=self.device_key)
                return
            except Exception as e:
                self.logger.warning(
                    msg=f"{self.display_name} reconnect failed, retrying: {e}"
                )
                await asyncio.sleep(delay=self.reconnect_delay)

    # ---- teardown --------------------------------------------------------
    async def disconnect(self) -> None:
        """Disconnect and stop any auto-reconnect attempts."""
        # Signal intentional shutdown so the disconnected callback does not
        # trigger a reconnect.
        self._should_run = False

        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            with contextlib.suppress(BaseException):
                await self._reconnect_task

        client, self.client = self.client, None
        if client is not None:
            try:
                # Do not gate cleanup on is_connected: a cancelled or failed
                # connection can still own backend resources while reporting
                # False. Bounded cleanup keeps shutdown responsive.
                await asyncio.wait_for(client.disconnect(), timeout=1.0)
                self.logger.debug(msg=f"Disconnected from {self.display_name}")
            except TimeoutError:
                self.logger.warning(msg="Disconnect timed out - forcing close")
            except Exception as e:
                self.logger.error(msg=f"Error during disconnect: {e}")

    @property
    def is_connected(self) -> bool:
        """Check if currently connected."""
        return self.client is not None and self.client.is_connected
