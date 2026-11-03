"""Application-owned state and coordination for the dashboard window."""

from __future__ import annotations

from pathlib import Path

from .core.log import LogImporter, LogSessionStore
from .importers.lbr_csv import LbrCsvCancelled, LbrCsvError, LbrCsvImporter, is_lbr_csv
from .ui.main_window import MainWindow


class DashboardController:
    """Own the imported session and translate UI requests into presentation."""

    def __init__(
        self,
        window: MainWindow,
        importer: LogImporter,
        lbr_importer: LbrCsvImporter | None = None,
    ) -> None:
        self._window = window
        self._log_store = LogSessionStore(importer)
        self._lbr_importer = lbr_importer or LbrCsvImporter()
        window.csv_requested.connect(self._on_csv_requested)
        window.column_selected.connect(self.select_column)

    def _on_csv_requested(self, file_path: str) -> None:
        """Report UI request failures before Qt can discard slot exceptions."""

        try:
            self.load_csv(file_path)
        except Exception as error:
            self._window.show_load_error(file_path, str(error))

    def load_csv(self, file_path: str) -> None:
        """Decode and retain the current session outside the widget layer.

        Firmware lbr-csv exports open in the session view; other CSVs keep the
        table view.
        """

        path = Path(file_path)
        if is_lbr_csv(path):
            self.load_lbr_csv(path)
            return
        self._log_store.load(path)
        self._window.display_log(self._log_store.headers, self._log_store.rows)
        self._window.show_source_name(path.name)

    def load_lbr_csv(self, path: Path) -> bool:
        """Open a firmware export; on failure keep what is shown and explain."""

        progress = self._window.import_progress(path.name)
        try:
            session = self._lbr_importer.import_session(path, progress=progress.report)
        except LbrCsvCancelled:
            return False
        except LbrCsvError as error:
            self._window.show_import_error(str(error))
            return False
        finally:
            progress.close()
        self._window.display_lbr_session(session)
        self._window.show_source_name(path.name)
        return True

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
