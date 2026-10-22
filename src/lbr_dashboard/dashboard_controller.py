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
        window.csv_requested.connect(self.load_csv)
        window.column_selected.connect(self.select_column)

    def load_csv(self, file_path: str) -> None:
        """Decode and retain the current session outside the widget layer."""

        if not self._log_store.load(Path(file_path)):
            return
        self._window.display_log(self._log_store.headers, self._log_store.rows)

    def select_column(self, column_name: str) -> None:
        """Pass the selected numeric series to the presentation layer."""

        self._window.plot_column(
            column_name,
            self._log_store.numeric_column(column_name),
            self._log_store.numeric_column("timestamp_ms"),
        )
