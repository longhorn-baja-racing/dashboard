"""Application-owned CSV state, worker lifecycle and presentation coordination."""

from __future__ import annotations

import logging
from pathlib import Path

from .core.log import LogImporter, LogSession, LogSessionStore
from .import_controller import ImportController, demo_session
from .importers.lbr_csv import LbrCsvCancelled, LbrCsvError, LbrCsvImporter, is_lbr_csv
from .ui.main_window import MainWindow

logger = logging.getLogger(__name__)


class DashboardController:
    """Own imported data and translate UI requests into presentation."""

    def __init__(
        self,
        window: MainWindow,
        importer: LogImporter,
        lbr_importer: LbrCsvImporter | None = None,
    ) -> None:
        self._window = window
        self._log_store = LogSessionStore(importer)
        self._lbr_importer = lbr_importer or LbrCsvImporter()
        self._selected_column = -1
        self.import_controller = ImportController(window, importer, self.present_session)
        window.csv_requested.connect(self.start_import)
        window.column_selected.connect(self.select_column)
        window.cell_edited.connect(self.edit_cell)

    def edit_cell(self, row: int, column: int, text: str) -> None:
        """Commit the displayed edit to the model before refreshing the selected plot."""
        self._log_store.edit_cell(row, column, text)
        self.select_column(self._selected_column)
        self._window.statusBar().showMessage("Edited in memory; the source CSV file is unchanged.")

    def start_import(self, file_path: str) -> None:
        path = Path(file_path)
        if is_lbr_csv(path):
            try:
                self.load_lbr_csv(path)
            except OSError as error:
                logger.warning("Could not load %s", file_path, exc_info=True)
                self._window.show_load_error(file_path, str(error))
            return
        self.import_controller.start(path)

    def cancel_import(self) -> None:
        self.import_controller.cancel()

    def close(self) -> bool:
        return self.import_controller.close()

    def load_csv(self, file_path: str) -> None:
        """Synchronous compatibility operation; errors propagate to the caller.

        Firmware lbr-csv exports open in the session view; other CSVs keep the
        table view.
        """
        path = Path(file_path)
        if is_lbr_csv(path):
            self.load_lbr_csv(path)
            return
        self._log_store.load(path)
        self._display()
        self._window.set_import_state("ready", "CSV loaded. Select a numeric column to plot.")
        self._window.show_source_name(path.name)
        self._window.csv_loaded.emit(str(file_path))

    def load_lbr_csv(self, path: Path) -> bool:
        """Open a firmware export; on failure keep what is shown and explain."""

        progress = self._window.import_progress(path.name)
        try:
            session = self._lbr_importer.import_session(path, progress=progress.report)
        except LbrCsvCancelled:
            return False
        except LbrCsvError as error:
            self._window.show_lbr_import_error(str(error))
            return False
        finally:
            progress.close()
        self._window.display_lbr_session(session)
        self._window.show_source_name(path.name)
        self._window.csv_loaded.emit(str(path))
        return True

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
        self._selected_column = column_index
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
