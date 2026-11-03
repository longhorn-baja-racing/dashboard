"""Application-owned asynchronous import with transactional UI commits."""

from __future__ import annotations

import csv
from collections.abc import Callable
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Thread
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QTimer

from .core.log import LogImporter, LogSession

if TYPE_CHECKING:
    from .ui.main_window import MainWindow


@dataclass(frozen=True)
class ImportFailure:
    reason: str
    recovery: str
    details: str = ""


def demo_session() -> LogSession:
    """A small explicit fictional fixture until simulator/source integration lands."""
    return LogSession.from_rows(
        ("timestamp_ms", "lab.temperature", "bench.pressure", "status.note"),
        (
            ("0", "20", "101.2", "ready"),
            ("15", "21", "", "sampling"),
            ("45", "", "103.4", "sampling"),
            ("100", "22", "0", "done"),
        ),
    )


def validate(session: LogSession) -> str:
    """Validate the legacy presentation contract without inferring time units."""
    if not session.headers or not session.rows:
        raise ValueError("The CSV has no data rows. Choose a CSV containing a header and readings.")
    if any(not name.strip() for name in session.headers):
        raise ValueError("A column has no name. Give every column a nonempty header.")
    if len(set(session.headers)) != len(session.headers):
        raise ValueError("Duplicate column names are ambiguous. Give each column a distinct name.")
    for index, row in enumerate(session.rows, 2):
        if len(row) != len(session.headers):
            raise ValueError(
                f"CSV row {index} has {len(row)} fields; expected {len(session.headers)}."
            )
    warnings = []
    times = [name for name in session.headers if name.casefold() == "timestamp_ms"]
    if len(times) > 1:
        raise ValueError("Multiple timestamp_ms columns are ambiguous. Choose one time column.")
    time = session.numeric_column(times[0]) if times else None
    if time is None:
        warnings.append(
            "No timestamp_ms column: showing sample index, not measurement time. "
            "For this preview, use timestamp_ms for millisecond measurement times."
        )
    elif any(value is None or not isfinite(value) for value in time):
        raise ValueError(
            "timestamp_ms contains a missing, invalid or non-finite time. Fix the time column."
        )
    unsupported = [
        name
        for name in session.headers
        if all(value is None for value in session.numeric_column(name) or ())
    ]
    if unsupported:
        warnings.append(
            "Text/status columns remain in the table; this preview plots numeric values only."
        )
    nonfinite = 0
    for row in session.rows:
        for value in row:
            try:
                nonfinite += not isfinite(float(value))
            except ValueError:
                pass
    if nonfinite:
        warnings.append(
            f"{nonfinite} non-finite numeric tokens are unavailable in plots. "
            "Original text remains in the table."
        )
    return " ".join(warnings)


def classify(error: Exception) -> ImportFailure:
    if isinstance(error, FileNotFoundError):
        return ImportFailure(
            "The selected file was not found.", "Check its location and choose Open CSV again."
        )
    if isinstance(error, PermissionError):
        return ImportFailure(
            "The selected file cannot be read.", "Check file permissions or choose another CSV."
        )
    if isinstance(error, UnicodeError):
        return ImportFailure(
            "The file is not valid UTF-8 text.", "Save a UTF-8 CSV copy and open it again."
        )
    if isinstance(error, csv.Error):
        return ImportFailure(
            "CSV quoting or a field is malformed.",
            "Check commas, quotes and field lengths, then retry.",
        )
    if isinstance(error, ValueError):
        return ImportFailure(
            str(error)[:500], "Correct the CSV or choose another file. Previous data is unchanged."
        )
    return ImportFailure(
        "The CSV could not be imported.", "Check the file is readable and choose another CSV."
    )


class ImportController(QObject):
    def __init__(
        self, window: MainWindow, importer: LogImporter, present: Callable[[LogSession], None]
    ) -> None:
        super().__init__(window)
        self.window = window
        self.importer = importer
        self.present = present
        self.worker: Thread | None = None
        self.cancelled = Event()
        self.result: Queue[tuple[LogSession | None, str | ImportFailure]] = Queue(maxsize=1)
        self.rows_read = 0
        self.path: Path | None = None
        self.timer = QTimer(self)
        self.timer.setInterval(30)
        self.timer.timeout.connect(self.poll)

    def start(self, path: Path) -> None:
        if self.worker is not None:
            return
        self.cancelled.clear()
        self.path = path
        self.rows_read = 0
        self.window.set_import_state("loading", "Reading CSVâ€¦ Previous data remains available.")

        def progress(count: int) -> None:
            self.rows_read = count

        def work() -> None:
            try:
                if path.suffix.lower() != ".csv":
                    raise ValueError("Unsupported file type. Choose a UTF-8 .csv file.")
                session = self.importer.import_session(
                    path, cancelled=self.cancelled.is_set, progress=progress
                )
                final_warning = ""
                if session.rows and len(session.rows[-1]) < len(session.headers):
                    session = LogSession.from_rows(session.headers, session.rows[:-1])
                    final_warning = "Dropped a truncated final row. "
                warning = validate(session)
                self.result.put((session, final_warning + warning))
            except InterruptedError:
                self.result.put((None, "cancelled"))
            except Exception as error:
                failure = classify(error)
                self.result.put(
                    (
                        None,
                        ImportFailure(
                            f"{path.name}: {failure.reason}",
                            failure.recovery,
                            f"File: {path}\n{type(error).__name__}: {error}",
                        ),
                    )
                )

        self.worker = Thread(target=work, name="dashboard-csv-import", daemon=True)
        self.worker.start()
        self.timer.start()

    def cancel(self) -> None:
        self.cancelled.set()
        self.window.import_message.setText("Cancelling importâ€¦ Previous data remains available.")

    def poll(self) -> None:
        if self.worker is None:
            return
        if self.worker.is_alive():
            if not self.cancelled.is_set():
                self.window.import_message.setText(f"Reading CSVâ€¦ {self.rows_read:,} rows read.")
            return
        self.worker.join()
        self.worker = None
        self.timer.stop()
        try:
            session, detail = self.result.get_nowait()
        except Empty:
            session, detail = (
                None,
                ImportFailure("Import worker returned no result.", "Choose another CSV and retry."),
            )
        if self.cancelled.is_set() or detail == "cancelled":
            self.window.set_import_state(
                "cancelled",
                "Import cancelled. Previous data is unchanged; open another CSV or try demo data.",
            )
        elif isinstance(detail, ImportFailure):
            self.window.show_load_error(str(self.path), detail.reason)
            self.window.show_import_error(detail.reason, detail.recovery, detail.details)
        elif session is not None:
            self.present(session)
            self.window.set_import_state(
                "ready",
                "CSV loaded. Select a numeric column to plot." + (" " + detail if detail else ""),
            )

    def close(self) -> bool:
        self.cancelled.set()
        if self.worker is not None:
            self.worker.join(timeout=1)
            if self.worker.is_alive():
                return False
            self.poll()
        self.timer.stop()
        return True
