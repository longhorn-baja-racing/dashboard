"""Temporary dashboard shell for the P0 foundation milestone."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QFileDialog, QMainWindow, QSplitter, QVBoxLayout, QWidget

from ..core.log import NumericColumn
from .constants import MIN_PANEL_WIDTH
from .left_panel import LeftPanel
from .replay_bar import ReplayBar
from .right_panel import RightPanel
from .source_picker import SourcePicker
from .top_menu_bar import TopMenuBar


class CsvLoader(Protocol):
    """Synchronous application operation exposed to the window."""

    def load_csv(self, file_path: str) -> None: ...


class SourceOwner(Protocol):
    def close(self) -> bool: ...
    def release_for_legacy(self) -> None: ...


class MainWindow(QMainWindow):
    """Main application window that coordinates imported data and presentation."""

    csv_requested = Signal(str)
    csv_load_failed = Signal(str, str)
    column_selected = Signal(int)

    def __init__(self) -> None:
        super().__init__()
        self._controller: CsvLoader | None = None
        self.source_owner: SourceOwner | None = None
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
        self.source_picker = SourcePicker()
        self.replay_bar = ReplayBar()
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.addWidget(self.source_picker)
        layout.addWidget(self.replay_bar)
        layout.addWidget(splitter, 1)
        self.setCentralWidget(container)

        self.left_panel.list_widget.currentRowChanged.connect(self._select_column)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.source_owner is not None and not self.source_owner.close():
            event.ignore()
            return
        super().closeEvent(event)

    def attach_controller(self, controller: CsvLoader) -> None:
        """Keep the application controller alive without owning its model data."""

        self._controller = controller

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

        if self.source_owner is not None:
            self.source_owner.release_for_legacy()
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
