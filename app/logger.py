"""Centralized logging configuration (singleton ``Logger``).

Wraps stdlib ``logging`` with one process-wide, thread-safe setup: a console
sink or — while the GUI runs — a ``CallbackHandler`` that streams formatted
lines into the on-screen activity log, plus a persistent rotating file handler
at ``~/KayakFit/logs/kayakfit.log`` that is kept open across re-setups
(closing/reopening would race concurrent writers). ``setup()`` may be called
repeatedly; it only reconfigures when a new ``log_callback`` is supplied or
``force=True``.

Logging only: process signal handling (SIGINT/SIGTERM) is owned by the
entrypoint (see ``kayakfit_gui.main``), not by this module.
"""

import contextlib
import logging
import sys
import threading
from collections.abc import Callable
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import IO


def _build_file_handler() -> logging.Handler | None:
    """Create a rotating file handler under ~/KayakFit/logs (cross-platform).

    Returns None if the log directory cannot be created, so logging setup never
    fails because of the file sink.
    """
    try:
        log_dir = Path.home() / "KayakFit" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            filename=str(log_dir / "kayakfit.log"),
            maxBytes=1_000_000,
            backupCount=5,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        return handler
    except Exception:
        return None


class CallbackHandler(logging.Handler):
    """Custom logging handler that sends messages to a callback function."""

    def __init__(self, callback: Callable[[str], None]) -> None:
        super().__init__()
        self.callback = callback

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            self.callback(msg + "\n")
        except Exception:
            self.handleError(record)


class Logger:
    """Centralized logger configuration for the application."""

    _initialized = False
    _log_level = logging.INFO
    # Guards reconfiguration: setup()/set_level() run from worker threads
    # (workout_session, strava_uploader attach the GUI callback off the main
    # thread) while other threads emit records, so handler swaps must be atomic.
    # Reentrant because get_logger() may call setup() while a caller already
    # holds the lock.
    _lock = threading.RLock()
    # Single process-wide rotating file sink, created once and kept across
    # re-setups so we don't close and reopen the same log file on every
    # workout/export (which races with concurrent writes and drops buffered
    # records).
    _file_handler: logging.Handler | None = None

    @classmethod
    def setup(
        cls,
        log_level: str = "INFO",
        force: bool = False,
        stream: IO[str] | None = None,
        log_callback: Callable[[str], None] | None = None
    ) -> None:
        """Setup application-wide logging configuration.

        Args:
            log_level: Log level as string (DEBUG, INFO, WARNING, ERROR, CRITICAL).
            force: Force reconfiguration even if already initialized.
            stream: Optional custom stream for log output (defaults to sys.stdout).
            log_callback: Optional callback function for log messages.
        """
        with cls._lock:
            # Reconfigure when a GUI log_callback is supplied even if already
            # initialized, so workout/export log lines reach the activity log
            # regardless of which component initialized logging first (e.g. a
            # prior CSV read that set up a stdout-only sink).
            if cls._initialized and not force and log_callback is None:
                return

            root_logger = logging.getLogger()
            # Drop the console/callback handlers we manage, but keep the
            # persistent rotating file sink open (see _file_handler). Closing it
            # here would reopen the same file on every workout/export and could
            # lose records buffered by another thread mid-write.
            for handler in list(root_logger.handlers):
                if handler is cls._file_handler:
                    continue
                with contextlib.suppress(Exception):
                    handler.close()
                root_logger.removeHandler(handler)

            # If log_callback is provided, use CallbackHandler
            if log_callback:
                callback_handler = CallbackHandler(callback=log_callback)
                log_level_str = log_level.upper()
                cls._log_level = getattr(logging, log_level_str, logging.INFO)
                callback_handler.setLevel(level=cls._log_level)

                formatter = logging.Formatter(fmt="%(message)s")
                callback_handler.setFormatter(fmt=formatter)

                root_logger.addHandler(hdlr=callback_handler)
            else:
                log_level_str = log_level.upper()
                cls._log_level = getattr(logging, log_level_str, logging.INFO)

                # Use custom stream if provided, otherwise stdout (or stderr).
                # In a windowed/frozen build (PyInstaller --noconsole on Windows)
                # sys.stdout/sys.stderr can be None — skip the console sink then;
                # the rotating file handler below still captures everything.
                output_stream = stream if stream is not None else (sys.stdout or sys.stderr)
                if output_stream is not None:
                    console_handler = logging.StreamHandler(stream=output_stream)
                    console_handler.setLevel(level=cls._log_level)
                    console_handler.setFormatter(fmt=logging.Formatter(fmt="%(message)s"))
                    root_logger.addHandler(hdlr=console_handler)

                # Only reconfigure stdout if we're using it and it has a
                # reconfigure method. Fetched via getattr so this stays
                # type-clean regardless of how the active typeshed types
                # sys.stdout (TextIO vs TextIOWrapper) -- no `type: ignore`
                # that could later be flagged unused.
                reconfigure = getattr(sys.stdout, "reconfigure", None)
                if stream is None and reconfigure is not None:
                    reconfigure(line_buffering=True)

            # Always keep a persistent rotating file sink so field issues (BLE
            # drops, upload failures) can be diagnosed after the fact. Created
            # once and reused; best-effort, so a failed creation is retried on a
            # later setup().
            if cls._file_handler is None:
                cls._file_handler = _build_file_handler()
                if cls._file_handler is not None:
                    root_logger.addHandler(hdlr=cls._file_handler)
            if cls._file_handler is not None:
                cls._file_handler.setLevel(level=cls._log_level)

            root_logger.setLevel(level=cls._log_level)

            cls._initialized = True

    @classmethod
    def get_logger(cls, name: str | None = None) -> logging.Logger:
        """Get a logger instance.

        Args:
            name: Logger name (typically __name__ of the calling module).

        Returns:
            Configured logger instance.
        """
        if not cls._initialized:
            cls.setup()

        return logging.getLogger(name=name)

    @classmethod
    def set_level(cls, log_level: str) -> None:
        """Change the log level after initialization.

        Args:
            log_level: New log level as string (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        """
        log_level_str = log_level.upper()
        new_level = getattr(logging, log_level_str, logging.INFO)

        with cls._lock:
            cls._log_level = new_level

            root_logger = logging.getLogger()
            root_logger.setLevel(level=new_level)
            for handler in list(root_logger.handlers):
                handler.setLevel(level=new_level)

