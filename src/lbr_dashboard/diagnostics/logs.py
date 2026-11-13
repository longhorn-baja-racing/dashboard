"""Bounded, redacted local logging that never interrupts the application.

Qt-free: the UI reads :class:`DiagnosticsSession` to show recent problems, but
nothing here imports Qt, so data sources and workers can log the same way.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
import sys
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

APP_LOGGER = "lbr_dashboard"
LOG_FILE_NAME = "dashboard.log"
MAX_LOG_BYTES = 1_048_576
LOG_BACKUP_COUNT = 3
MAX_MESSAGE_CHARS = 4000
RECENT_LIMIT = 50

LOG_DIR_ENV = "LBR_DASHBOARD_LOG_DIR"
LOG_LEVEL_ENV = "LBR_DASHBOARD_LOG_LEVEL"

_FORMAT = "%(asctime)s %(levelname)-8s %(threadName)s %(name)s: %(message)s"
_SECRET = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization|auth)\b(\s*[=:]\s*)"
    r"(\"[^\"]*\"|'[^']*'|bearer\s+\S+|\S+)"
)


def default_log_directory(environ: Mapping[str, str] | None = None) -> Path:
    """Return the per-user log folder for this platform.

    ``LBR_DASHBOARD_LOG_DIR`` overrides it. Windows uses
    ``%LOCALAPPDATA%\\LBR Dashboard\\logs``; macOS uses
    ``~/Library/Logs/LBR Dashboard``; other systems use
    ``$XDG_STATE_HOME/lbr-dashboard/logs`` (default ``~/.local/state``).
    """

    env = os.environ if environ is None else environ
    if env.get(LOG_DIR_ENV):
        return Path(env[LOG_DIR_ENV])
    if sys.platform == "win32":
        base = env.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "LBR Dashboard" / "logs"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "LBR Dashboard"
    state = env.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / "lbr-dashboard" / "logs"


def redact(text: str, home: str | None = None) -> str:
    """Hide credential values and the user's home folder in log text."""

    text = _SECRET.sub(lambda match: f"{match.group(1)}{match.group(2)}<redacted>", text)
    home = str(Path.home()) if home is None else home
    if len(home) > 1:
        flags = re.IGNORECASE if sys.platform == "win32" else 0
        text = re.sub(re.escape(home), "~", text, flags=flags)
    return text


class RedactingFormatter(logging.Formatter):
    """Format, bound and redact a record, including any traceback."""

    def __init__(self, home: str | None = None) -> None:
        super().__init__(_FORMAT)
        self._home = home

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if len(message) > MAX_MESSAGE_CHARS:
            omitted = len(message) - MAX_MESSAGE_CHARS
            record = logging.makeLogRecord(record.__dict__)
            record.msg = f"{message[:MAX_MESSAGE_CHARS]}... [{omitted} characters omitted]"
            record.args = None
        return redact(super().format(record), self._home)


class SafeRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """Rotating file handler that counts write failures instead of raising.

    A full disk, a locked file during rotation (common on Windows when two
    copies run) or a removed folder must never stop telemetry handling.
    """

    def __init__(self, path: Path, *, max_bytes: int, backup_count: int, stderr: TextIO) -> None:
        super().__init__(
            path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8", delay=True
        )
        self.failures = 0
        self.last_failure: str | None = None
        self._stderr = stderr

    def handleError(self, record: logging.LogRecord) -> None:
        error = sys.exc_info()[1]
        self.failures += 1
        self.last_failure = "unknown error" if error is None else f"{type(error).__name__}: {error}"
        if self.failures == 1:
            try:
                self._stderr.write(f"lbr-dashboard: log file write failed: {self.last_failure}\n")
            except Exception:  # pragma: no cover - nothing left to report to
                pass


class RecentRecordsHandler(logging.Handler):
    """Keep the newest formatted warnings and errors in memory for reports."""

    def __init__(self, limit: int = RECENT_LIMIT, home: str | None = None) -> None:
        super().__init__(logging.WARNING)
        self.records: deque[str] = deque(maxlen=limit)
        self.total = 0
        self.setFormatter(RedactingFormatter(home))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.records.append(self.format(record))
            self.total += 1
        except Exception:
            self.handleError(record)


@dataclass
class DiagnosticsSession:
    """Handles owned by :func:`configure_logging`; call :meth:`close` on exit."""

    log_directory: Path
    log_file: Path | None
    file_error: str | None
    recent: RecentRecordsHandler
    file_handler: SafeRotatingFileHandler | None = None
    _installed: list[logging.Handler] = field(default_factory=list)
    _previous_level: int = logging.WARNING
    _closed: bool = False

    def recent_records(self) -> tuple[str, ...]:
        return tuple(self.recent.records)

    def log_health(self) -> str:
        """One line describing whether logs are being written."""

        if self.log_file is None:
            return f"not writing a log file ({self.file_error})"
        handler = self.file_handler
        if handler is not None and handler.failures:
            return f"{handler.failures} write failure(s), last: {handler.last_failure}"
        return "ok"

    def close(self) -> None:
        """Remove installed handlers and restore the previous logging state."""

        if self._closed:
            return
        self._closed = True
        root = logging.getLogger()
        for handler in self._installed:
            root.removeHandler(handler)
            try:
                handler.close()
            except OSError:
                pass  # A full or vanished disk must not turn shutdown into a crash.
        root.setLevel(self._previous_level)
        logging.captureWarnings(False)


def _level(environ: Mapping[str, str], default: int) -> int:
    name = environ.get(LOG_LEVEL_ENV, "").strip().upper()
    value = logging.getLevelName(name) if name else default
    return value if isinstance(value, int) else default


def configure_logging(
    directory: Path | None = None,
    *,
    level: int = logging.INFO,
    max_bytes: int = MAX_LOG_BYTES,
    backup_count: int = LOG_BACKUP_COUNT,
    environ: Mapping[str, str] | None = None,
    stderr: TextIO | None = None,
    home: str | None = None,
) -> DiagnosticsSession:
    """Send application logs to a bounded rotating file, stderr and memory.

    Disk usage is at most ``max_bytes * (backup_count + 1)`` (4 MiB by
    default). If the folder cannot be created or opened, logging continues to
    stderr and memory and the reason is kept in ``file_error``.
    """

    env = os.environ if environ is None else environ
    stream = sys.stderr if stderr is None else stderr
    log_directory = default_log_directory(env) if directory is None else directory
    root = logging.getLogger()
    session = DiagnosticsSession(
        log_directory=log_directory,
        log_file=None,
        file_error=None,
        recent=RecentRecordsHandler(home=home),
        _previous_level=root.level,
    )

    formatter = RedactingFormatter(home)
    console = logging.StreamHandler(stream)
    console.setLevel(logging.WARNING)
    console.setFormatter(formatter)
    handlers: list[logging.Handler] = [console, session.recent]

    log_file = log_directory / LOG_FILE_NAME
    try:
        log_directory.mkdir(parents=True, exist_ok=True)
        with log_file.open("a", encoding="utf-8"):
            pass
    except OSError as error:
        session.file_error = redact(f"{type(error).__name__}: {error}", home)
    else:
        file_handler = SafeRotatingFileHandler(
            log_file, max_bytes=max_bytes, backup_count=backup_count, stderr=stream
        )
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)
        session.file_handler = file_handler
        session.log_file = log_file

    for handler in handlers:
        root.addHandler(handler)
    session._installed = handlers
    root.setLevel(_level(env, level))
    logging.captureWarnings(True)
    if session.file_error is not None:
        logging.getLogger(APP_LOGGER).warning(
            "Log file unavailable in %s; continuing without it (%s)",
            redact(str(log_directory), home),
            session.file_error,
        )
    return session
