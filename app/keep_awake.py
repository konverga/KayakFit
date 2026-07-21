"""Keep the display awake during a workout.

During recording the athlete is on the ergometer, not touching the keyboard or
mouse, so the OS would normally dim and sleep the screen. This module holds a
platform-appropriate "stay awake" assertion for the duration of a workout and
releases it when the workout ends.

  - macOS:    a `caffeinate` child process (tied to our PID so it can never
              outlive the app).
  - Windows:  SetThreadExecutionState with DISPLAY + SYSTEM required.
  - Linux:    a best-effort `systemd-inhibit` child process; silently a no-op if
              that tool is unavailable.

Everything is best-effort: failing to keep the screen awake must never affect
data collection, so all errors are swallowed.
"""

import contextlib
import os
import subprocess
import sys
from collections.abc import Callable

# Windows SetThreadExecutionState flags.
_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001
_ES_DISPLAY_REQUIRED = 0x00000002


class ScreenAwake:
    """Hold a display-stay-awake assertion between :meth:`start` and :meth:`stop`."""

    def __init__(self, log: Callable[[str], None] | None = None) -> None:
        self._log = log
        self._active = False
        self._proc: subprocess.Popen[bytes] | None = None

    def _debug(self, message: str) -> None:
        if self._log:
            with contextlib.suppress(Exception):
                self._log(message)

    def start(self) -> None:
        """Begin keeping the screen awake (idempotent)."""
        if self._active:
            return
        try:
            if sys.platform == "darwin":
                # -d display, -i idle system, -s system (on AC); -w ties the
                # helper's lifetime to ours so it cannot leak if we crash.
                self._proc = subprocess.Popen(
                    ["caffeinate", "-d", "-i", "-s", "-w", str(os.getpid())],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self._active = True
            elif sys.platform.startswith("win"):
                import ctypes

                ctypes.windll.kernel32.SetThreadExecutionState(  # type: ignore[attr-defined]
                    _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED | _ES_DISPLAY_REQUIRED
                )
                self._active = True
            else:
                # Linux / other: best-effort inhibitor; no-op if not present.
                self._proc = subprocess.Popen(
                    [
                        "systemd-inhibit",
                        "--what=idle:sleep",
                        "--who=KayakFit",
                        "--why=Workout in progress",
                        # Like caffeinate -w on macOS: the helper exits when we
                        # do, so a crash can never leak an eternal inhibitor.
                        "tail",
                        f"--pid={os.getpid()}",
                        "-f",
                        "/dev/null",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self._active = True
            if self._active:
                self._debug("Screen will stay awake during the workout.\n")
        except FileNotFoundError:
            # Helper binary not installed; nothing we can do, stay silent-ish.
            self._active = False
            self._proc = None
        except Exception:
            self._active = False
            self._proc = None

    def stop(self) -> None:
        """Release the stay-awake assertion (idempotent, safe to over-call)."""
        if not self._active:
            return
        try:
            if sys.platform.startswith("win"):
                import ctypes

                ctypes.windll.kernel32.SetThreadExecutionState(  # type: ignore[attr-defined]
                    _ES_CONTINUOUS
                )
            elif self._proc is not None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=2)
                except Exception:
                    with contextlib.suppress(Exception):
                        self._proc.kill()
        except Exception:
            pass
        finally:
            self._proc = None
            self._active = False

    @property
    def active(self) -> bool:
        """Return True while a display-sleep inhibitor is currently held."""
        return self._active
