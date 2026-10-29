"""Temporary dashboard shell for the P0 foundation milestone."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFileDialog, QMainWindow, QSplitter

from ..core.log import NumericColumn
from .constants import MIN_PANEL_WIDTH
from .left_panel import LeftPanel
from .right_panel import RightPanel
from .top_menu_bar import TopMenuBar


class CsvLoader(Protocol):
    """Synchronous application operation exposed to the window."""

    def load_csv(self, file_path: str) -> None: ...


class MainWindow(QMainWindow):
    """Main application window that coordinates imported data and presentation."""

    csv_requested = Signal(str)
    csv_loaded = Signal(str)
    theme_requested = Signal(str)
    clear_recent_requested = Signal()
    csv_load_failed = Signal(str, str)
    column_selected = Signal(int)
    cell_edited = Signal(int, int, str)

    def __init__(self) -> None:
        super().__init__()
        self._controller: CsvLoader | None = None
        self.preferences_controller: object | None = None
        self.setWindowTitle("LBR Dashboard")
        self.resize(1200, 700)

        self.setMenuBar(TopMenuBar(self))

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.left_panel = LeftPanel()
        self.right_panel = RightPanel()
        splitter.addWidget(self.left_panel)
        splitter.addWidget(self.right_panel)
        splitter.setCollapsible(0, False)
        splitter.setCollapsible(1, False)
        splitter.setSizes([300, 900])
        splitter.setOpaqueResize(True)
        splitter.setHandleWidth(4)
        splitter.setStyleSheet(
            """
            QSplitter::handle { background-color: #c0c0c0; }
            QSplitter::handle:hover { background-color: #a0a0a0; }
            """
        )

        self.left_panel.setMinimumWidth(MIN_PANEL_WIDTH)
        self.right_panel.setMinimumWidth(MIN_PANEL_WIDTH)
        self.setCentralWidget(splitter)

        self.left_panel.list_widget.currentRowChanged.connect(self._select_column)
        self.right_panel.table.itemChanged.connect(
            lambda item: self.cell_edited.emit(item.row(), item.column(), item.text())
        )

    def attach_controller(self, controller: CsvLoader) -> None:
        """Keep the application controller alive without owning its model data."""

        self._controller = controller

    def set_preference_choices(self, theme: str, recent_paths: tuple[str, ...]) -> None:
        menu = self.menuBar()
        if isinstance(menu, TopMenuBar):
            menu.set_preference_choices(theme, recent_paths)

    def _select_column(self, column_index: int) -> None:
        """Highlight and plot the selected column, including the first selection."""

        self.right_panel.highlight_column(column_index)
        if column_index < 0:
            return

        self.column_selected.emit(column_index)

    def open_csv(self) -> None:
        """Open a CSV file and display its columns and rows."""

        file_path, _ = QFileDialog.getOpenFileName(self, "Open CSV File", "", "CSV Files (*.csv)")
        if file_path:
            self.csv_requested.emit(file_path)

    def load_csv(self, file_path: Path) -> None:
        """Load synchronously so callers receive decoding and filesystem errors."""

        if self._controller is None:
            raise RuntimeError("No CSV loader is attached to this window")
        self._controller.load_csv(str(file_path))

    def show_load_error(self, file_path: str, message: str) -> None:
        """Expose a failed UI request to users and signal observers."""

        self.statusBar().showMessage(f"Could not load {file_path}: {message}")
        self.csv_load_failed.emit(file_path, message)

    def display_log(self, headers: tuple[str, ...], rows: tuple[tuple[str, ...], ...]) -> None:
        """Render an already imported session."""

        self.statusBar().clearMessage()
        self.right_panel.set_table(headers, rows)
        self.left_panel.set_columns(list(headers))
        self.left_panel.list_widget.setCurrentRow(0)

    def plot_column(
        self,
        column_name: str,
        values: NumericColumn | None,
        timestamp_values: NumericColumn | None,
    ) -> None:
        """Forward model values to the presentation widget for plotting."""

        self.right_panel.plot_column(column_name, values, timestamp_values)
