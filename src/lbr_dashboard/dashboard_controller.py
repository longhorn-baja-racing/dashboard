"""Application-owned state and coordination for the dashboard window."""

from __future__ import annotations

from pathlib import Path

from .core.log import LogImporter, LogSessionStore
from .ui.main_window import MainWindow


class DashboardController:
    """Own the imported session and translate UI requests into presentation."""

    def __init__(self, window: MainWindow, importer: LogImporter) -> None:
        self._window = window
        self._log_store = LogSessionStore(importer)
        window.csv_requested.connect(self._on_csv_requested)
        window.column_selected.connect(self.select_column)

    def _on_csv_requested(self, file_path: str) -> None:
        """Report UI request failures before Qt can discard slot exceptions."""

        try:
            self.load_csv(file_path)
        except Exception as error:
            self._window.show_load_error(file_path, str(error))

    def load_csv(self, file_path: str) -> None:
        """Decode and retain the current session outside the widget layer."""

        self._log_store.load(Path(file_path))
        self._window.display_log(self._log_store.headers, self._log_store.rows)
        self._window.csv_loaded.emit(str(file_path))

    def select_column(self, column_index: int) -> None:
        """Pass the selected numeric series to the presentation layer."""

        if not 0 <= column_index < len(self._log_store.headers):
            return
        column_name = self._log_store.headers[column_index]
        self._window.plot_column(
            column_name,
            self._log_store.numeric_column(column_index),
            None
            if column_name == "timestamp_ms"
            else self._log_store.numeric_column("timestamp_ms"),
        )
