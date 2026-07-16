"""KayakFit Main Application Entry Point.

Launches the KayakFit GUI with workout and export workers, and handles signals
for clean shutdown. Initializes logging and crash diagnostics before any GUI
code runs, so failures in a windowed (no-console) build are still recorded.
"""

import faulthandler
import logging
import signal
import sys
from pathlib import Path
from types import FrameType, TracebackType

import customtkinter as ctk

from app.logger import Logger
from export_worker import run_export_worker
from gui.main_gui import KayakFitGUI
from gui.window_utils import normalize_tk_scaling
from workout_worker import run_workout_worker

log = logging.getLogger(__name__)

# Interval at which the mainloop wakes so pending Python signal handlers
# (SIGINT/SIGTERM) run promptly. Tk's C event loop otherwise delays them
# until the next GUI event (mouse move, timer), which makes Ctrl+C appear
# dead while the window is idle.
_SIGNAL_HEARTBEAT_MS = 200


def _enable_faulthandler() -> None:
    """Write native-crash tracebacks (e.g. from BLE backends) to a log file.

    Best-effort: never blocks startup. The file handle intentionally stays
    open for the lifetime of the process (faulthandler requires it).
    """
    try:
        log_dir = Path.home() / "KayakFit" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        crash_file = open(  # noqa: SIM115 - must outlive this function
            log_dir / "faulthandler.log", "a", encoding="utf-8"
        )
        faulthandler.enable(file=crash_file)
    except Exception:
        pass


def _log_callback_exception(
    exc: type[BaseException],
    val: BaseException,
    tb: TracebackType | None,
) -> None:
    """Log unhandled Tk-callback exceptions.

    Tk's default handler prints to stderr, which does not exist in a windowed
    PyInstaller build — without this hook such errors vanish silently.
    """
    log.error("Unhandled exception in Tk callback", exc_info=(exc, val, tb))


def main() -> None:
    # Logging (console + rotating file sink) must be up before any GUI code
    # runs so startup problems are captured in ~/KayakFit/logs.
    Logger.setup()
    _enable_faulthandler()

    ctk.set_appearance_mode("system")
    ctk.set_default_color_theme("blue")

    root = ctk.CTk()
    root.report_callback_exception = _log_callback_exception
    # Correct a mis-detected display DPI (common in VMs/remote sessions) before
    # any window is sized, so the UI doesn't render oversized and overflow.
    normalize_tk_scaling(root)
    app = KayakFitGUI(
        root, workout_worker=run_workout_worker, export_worker=run_export_worker
    )

    def signal_handler(sig: int, frame: FrameType | None) -> None:
        # Schedule the hard-exit fallback before on_closing: when no worker is
        # running on_closing destroys the root immediately, after which
        # root.after would raise TclError.
        try:
            root.after(3000, lambda: sys.exit(0))
            # Defer shutdown to the event loop instead of re-entering Tk from
            # whatever point the interpreter happened to be interrupted at.
            root.after_idle(app.on_closing)
        except Exception:
            pass

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    def _heartbeat() -> None:
        # No-op wakeup so Python-level signal handlers get a chance to run
        # while the Tk mainloop is otherwise idle in C code.
        root.after(_SIGNAL_HEARTBEAT_MS, _heartbeat)

    _heartbeat()

    root.mainloop()
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Last-resort capture of fatal startup errors (invisible on stderr in
        # a windowed build); re-raise so the process still exits non-zero.
        logging.getLogger(__name__).critical("Fatal startup error", exc_info=True)
        raise
