"""Application-owned CSV state, worker lifecycle and presentation coordination."""

from __future__ import annotations

from pathlib import Path

from .core.log import LogImporter, LogSession, LogSessionStore
from .import_controller import ImportController, demo_session
from .ui.main_window import MainWindow


class DashboardController:
    """Own imported data and translate UI requests into presentation."""

    def __init__(self, window: MainWindow, importer: LogImporter) -> None:
        self._window = window
        self._log_store = LogSessionStore(importer)
        self.import_controller = ImportController(window, importer, self.present_session)
        window.csv_requested.connect(self.start_import)
        window.column_selected.connect(self.select_column)

    def start_import(self, file_path: str) -> None:
        self.import_controller.start(Path(file_path))

    def cancel_import(self) -> None:
        self.import_controller.cancel()

    def close(self) -> bool:
        return self.import_controller.close()

    def load_csv(self, file_path: str) -> None:
        """Synchronous compatibility operation; errors propagate to the caller."""
        self._log_store.load(Path(file_path))
        self._display()
        self._window.set_import_state("ready", "CSV loaded. Select a numeric column to plot.")

    def present_session(self, session: LogSession) -> None:
        self._log_store.replace(session)
        self._display(prefer_measurement=True)

    def _display(self, *, prefer_measurement: bool = False) -> None:
        columns = [
            (index, name)
            for index, name in enumerate(self._log_store.headers)
            if any(value is not None for value in self._log_store.numeric_column(index) or ())
        ]
        self._window.display_log(
            self._log_store.headers,
            self._log_store.rows,
            columns,
            prefer_measurement=prefer_measurement,
        )

    def select_column(self, column_index: int) -> None:
        if not 0 <= column_index < len(self._log_store.headers):
            return
        column_name = self._log_store.headers[column_index]
        self._window.plot_column(
            column_name,
            self._log_store.numeric_column(column_index),
            None
            if column_name.casefold() == "timestamp_ms"
            else self._log_store.numeric_column("timestamp_ms"),
        )

    def try_demo(self) -> None:
        if self.import_controller.worker is not None:
            return
        self.present_session(demo_session())
        self._window.source_label.setText("Fictional static demo CSV. No sensor is connected.")
        self._window.set_import_state(
            "ready",
            "Demo loaded: select temperature or pressure. "
            "Empty cells mean missing readings; 0 is a real value. "
            "This is a static preview, not a live sensor.",
        )
