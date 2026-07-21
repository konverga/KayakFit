"""Fixed-rate KayakFirst polling contracts."""

import asyncio
from unittest.mock import patch

from app.kayakfirst_ergometer_bluetooth import (
    POLL_DATA_CMD,
    KayakFirstErgometer,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay


class _Client:
    is_connected = True


class _Logger:
    def warning(self, msg: str = "") -> None:
        pass


def _ergometer(
    clock: _Clock, send_duration_s: float
) -> tuple[KayakFirstErgometer, list[float]]:
    ergometer = KayakFirstErgometer.__new__(KayakFirstErgometer)
    ergometer._should_run = True
    ergometer.client = _Client()  # type: ignore[assignment]
    ergometer.logger = _Logger()  # type: ignore[assignment]
    starts: list[float] = []

    async def _send(command: str) -> None:
        assert command == POLL_DATA_CMD
        starts.append(clock.time())
        clock.now += send_duration_s
        if len(starts) == 4:
            ergometer._should_run = False

    ergometer._send_command = _send  # type: ignore[method-assign]
    return ergometer, starts


def test_poll_writes_stay_on_fixed_one_second_deadlines() -> None:
    clock = _Clock()
    ergometer, starts = _ergometer(clock, send_duration_s=0.05)

    with (
        patch(
            "app.kayakfirst_ergometer_bluetooth.asyncio.get_running_loop",
            return_value=clock,
        ),
        patch(
            "app.kayakfirst_ergometer_bluetooth.asyncio.sleep",
            side_effect=clock.sleep,
        ),
    ):
        asyncio.run(ergometer.poll_data())

    assert starts == [0.0, 1.0, 2.0, 3.0]
    assert len(clock.sleeps) == 3
    assert all(abs(delay - 0.95) < 1e-9 for delay in clock.sleeps)


def test_late_poll_resumes_without_catch_up_burst() -> None:
    clock = _Clock()
    ergometer, starts = _ergometer(clock, send_duration_s=1.25)

    with (
        patch(
            "app.kayakfirst_ergometer_bluetooth.asyncio.get_running_loop",
            return_value=clock,
        ),
        patch(
            "app.kayakfirst_ergometer_bluetooth.asyncio.sleep",
            side_effect=clock.sleep,
        ),
    ):
        asyncio.run(ergometer.poll_data())

    assert starts == [0.0, 2.25, 4.5, 6.75]
    assert clock.sleeps == [1.0, 1.0, 1.0]
