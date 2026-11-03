"""Route uncaught exceptions from any thread to the log and an optional notifier."""

from __future__ import annotations

import logging
import sys
import threading
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

from .logs import APP_LOGGER

logger = logging.getLogger(f"{APP_LOGGER}.errors")

MAX_SUMMARY_CHARS = 300


@dataclass(frozen=True, slots=True)
class ErrorReport:
    """Short, user-presentable description of one uncaught exception."""

    summary: str
    location: str
    thread: str

    @property
    def signature(self) -> str:
        """Identity used to show the same failure to the user only once."""

        return f"{self.summary.split(':', 1)[0]}@{self.location}"


def describe(
    error_type: type[BaseException], error: BaseException | None, tb: TracebackType | None
) -> ErrorReport:
    """Build an :class:`ErrorReport` without full paths or local variable values."""

    message = str(error) if error is not None else ""
    summary = f"{error_type.__name__}: {message}" if message else error_type.__name__
    if len(summary) > MAX_SUMMARY_CHARS:
        summary = summary[: MAX_SUMMARY_CHARS - 3] + "..."
    frames = traceback.extract_tb(tb) if tb is not None else []
    if frames:
        last = frames[-1]
        location = f"{Path(last.filename).name}:{last.lineno} in {last.name}"
    else:
        location = "unknown location"
    return ErrorReport(summary, location, threading.current_thread().name)


ErrorCallback = Callable[[ErrorReport], None]


def install_exception_hooks(on_error: ErrorCallback | None = None) -> Callable[[], None]:
    """Install ``sys.excepthook`` and ``threading.excepthook``; return an undo function.

    Qt slots that raise also reach ``sys.excepthook`` in PySide6. Every
    uncaught exception is logged with its traceback, then passed to
    ``on_error``. A failing callback is logged but never re-raised, and
    ``KeyboardInterrupt`` keeps its default behavior.
    """

    previous_sys = sys.excepthook
    previous_thread = threading.excepthook

    def handle(
        error_type: type[BaseException],
        error: BaseException | None,
        tb: TracebackType | None,
        thread_name: str | None = None,
    ) -> None:
        where = f" in thread {thread_name}" if thread_name else ""
        if error is None:
            logger.critical("Uncaught %s%s", error_type.__name__, where)
        else:
            logger.critical("Uncaught exception%s", where, exc_info=(error_type, error, tb))
        if on_error is None:
            return
        report = describe(error_type, error, tb)
        if thread_name:
            report = ErrorReport(report.summary, report.location, thread_name)
        try:
            on_error(report)
        except Exception:
            logger.exception("Error notifier failed")

    def sys_hook(
        error_type: type[BaseException], error: BaseException, tb: TracebackType | None
    ) -> None:
        if issubclass(error_type, KeyboardInterrupt):
            previous_sys(error_type, error, tb)
            return
        handle(error_type, error, tb)

    def thread_hook(args: threading.ExceptHookArgs) -> None:
        if issubclass(args.exc_type, SystemExit):
            return
        name = args.thread.name if args.thread is not None else "unknown"
        handle(args.exc_type, args.exc_value, args.exc_traceback, name)

    sys.excepthook = sys_hook
    threading.excepthook = thread_hook

    def restore() -> None:
        sys.excepthook = previous_sys
        threading.excepthook = previous_thread

    return restore
