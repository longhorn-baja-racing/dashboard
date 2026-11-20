"""Temporary dashboard shell for the P0 foundation milestone."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QProgressDialog,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..core.lbr_session import LbrSession
from ..core.log import NumericColumn
from .analysis import AnalysisContext, AnalysisDocks
from .constants import MIN_PANEL_WIDTH
from .docks import WorkspaceDocks
from .lbr_session_view import LbrSessionView
from .left_panel import LeftPanel
from .replay_bar import ReplayBar
from .right_panel import RightPanel
from .source_picker import SourcePicker
from .tools import TOOLS
from .tools.export import PlotImageSaver
from .top_menu_bar import TopMenuBar


class CsvLoader(Protocol):
    def load_csv(self, file_path: str) -> None: ...
    def start_import(self, file_path: str) -> None: ...
    def cancel_import(self) -> None: ...
    def try_demo(self) -> None: ...
    def close(self) -> bool: ...


class ImportProgress:
    """A cancellable progress dialog that importers report into."""

    def __init__(self, parent: QMainWindow, file_name: str) -> None:
        self._dialog = QProgressDialog(f"Reading {file_name}...", "Cancel", 0, 1000, parent)
        self._dialog.setWindowTitle("Opening session")
        self._dialog.setWindowModality(Qt.WindowModality.WindowModal)
        self._dialog.setMinimumDuration(400)

    def report(self, done: int, total: int) -> bool:
        """Show progress and return False once the user has cancelled."""

        self._dialog.setValue(min(999, done * 1000 // max(total, 1)))
        QApplication.processEvents()
        return not self._dialog.wasCanceled()

    def close(self) -> None:
        self._dialog.close()


class SourceOwner(Protocol):
    def close(self) -> bool: ...
    def release_for_legacy(self) -> None: ...
    def has_session(self) -> bool: ...


class MainWindow(QMainWindow):
    """Main application window that coordinates imported data and presentation."""

    csv_requested = Signal(str)
    csv_loaded = Signal(str)
    theme_requested = Signal(str)
    clear_recent_requested = Signal()
    csv_load_failed = Signal(str, str)
    column_selected = Signal(int)
    import_failed = Signal(str, str)
    cell_edited = Signal(int, int, str)

    def __init__(self) -> None:
        super().__init__()
        self.setStyleSheet(
            "QFrame#importCard { border: 1px solid #555; border-radius: 6px; }"
            "QPushButton { padding: 6px 12px; }"
        )
        self._controller: CsvLoader | None = None
        self.preferences_controller: object | None = None
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
        root = QWidget()
        layout = QVBoxLayout(root)
        card = QFrame()
        card.setObjectName("importCard")
        controls = QVBoxLayout(card)
        title = QLabel("Explore a CSV or try fictional demo data")
        title.setStyleSheet("font-size: 18px; font-weight: bold;")
        controls.addWidget(title)
        self.import_message = QLabel()
        self.import_message.setWordWrap(True)
        controls.addWidget(self.import_message)
        self.source_label = QLabel("No data loaded. No sensor is connected.")
        controls.addWidget(self.source_label)
        actions = QHBoxLayout()
        self.open_button = QPushButton("Open CSV")
        self.demo_button = QPushButton("Try demo data")
        self.cancel_button = QPushButton("Cancel import")
        self.details_button = QPushButton("Show error details")
        self.copy_button = QPushButton("Copy error details")
        for button in (
            self.open_button,
            self.demo_button,
            self.cancel_button,
            self.details_button,
            self.copy_button,
        ):
            actions.addWidget(button)
        actions.addStretch()
        controls.addLayout(actions)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setMaximumHeight(8)
        controls.addWidget(self.progress)
        self.error_details = QPlainTextEdit()
        self.error_details.setReadOnly(True)
        self.error_details.setMaximumHeight(110)
        controls.addWidget(self.error_details)
        # Firmware lbr-csv exports get their own view; other CSVs keep the table view.
        self.lbr_view = LbrSessionView()
        self.pages = QStackedWidget()
        # The CSV start card belongs to the table page, so it does not take
        # height from the firmware session view.
        table_page = QWidget()
        table_layout = QVBoxLayout(table_page)
        table_layout.setContentsMargins(0, 0, 0, 0)
        table_layout.addWidget(card)
        table_layout.addWidget(splitter, 1)
        self.pages.addWidget(table_page)
        self.pages.addWidget(self.lbr_view)
        self.import_error: QMessageBox | None = None
        self.source_picker = SourcePicker()
        self.replay_bar = ReplayBar()
        layout.addWidget(self.source_picker)
        layout.addWidget(self.replay_bar)
        layout.addWidget(self.pages, 1)
        self.setCentralWidget(root)
        self.workspace_docks = WorkspaceDocks(
            self, lambda: self.left_panel.signal_browser.selected_id
        )
        self.analysis = AnalysisContext(self)
        self.analysis_docks = AnalysisDocks(self, self.analysis, TOOLS)
        self.plot_image = PlotImageSaver(self, self.right_panel.plot_widget)
        self.analysis_docks.menu.addSeparator()
        self.analysis_docks.menu.addAction(self.plot_image.action)
        self.open_button.clicked.connect(self.open_csv)
        self.demo_button.clicked.connect(self.try_demo)
        self.cancel_button.clicked.connect(self.cancel_import)
        self.details_button.clicked.connect(self.toggle_error_details)
        self.copy_button.clicked.connect(self.copy_error)
        self.set_import_state(
            "empty",
            "Open a UTF-8 CSV, select a column, and inspect its values. "
            "timestamp_ms is interpreted in milliseconds; other files use sample index.",
        )

        self.left_panel.list_widget.currentRowChanged.connect(self._select_column)
        self.right_panel.plot_widget.setTitle("No data loaded - open CSV or try demo data")
        self.right_panel.table.setEditTriggers(self.right_panel.table.EditTrigger.NoEditTriggers)
        self.right_panel.table.itemChanged.connect(
            lambda item: self.cell_edited.emit(item.row(), item.column(), item.text())
        )

    def attach_controller(self, controller: CsvLoader) -> None:
        self._controller = controller

    def set_preference_choices(self, theme: str, recent_paths: tuple[str, ...]) -> None:
        menu = self.menuBar()
        if isinstance(menu, TopMenuBar):
            menu.set_preference_choices(theme, recent_paths)

    def _select_column(self, column_index: int) -> None:
        if self.source_owner is not None and self.source_owner.has_session():
            return
        item = self.left_panel.list_widget.item(column_index)
        physical_index = item.data(Qt.ItemDataRole.UserRole) if item is not None else -1
        self.right_panel.highlight_column(physical_index)
        if physical_index >= 0:
            self.column_selected.emit(physical_index)

    def open_csv(self) -> None:
        """Open a CSV file and display its columns and rows."""

        file_path, _ = QFileDialog.getOpenFileName(
            self, "Open CSV File", "", "CSV and Text Files (*.csv *.txt)"
        )
        if file_path:
            self.csv_requested.emit(file_path)

    def load_csv(self, file_path: Path) -> None:
        """Load synchronously and propagate importer errors to callers."""
        if self._controller is None:
            raise RuntimeError("No CSV loader is attached to this window")
        self._controller.load_csv(str(file_path))

    def display_log(
        self,
        headers: tuple[str, ...],
        rows: tuple[tuple[str, ...], ...],
        columns: Sequence[tuple[int, str]],
        *,
        prefer_measurement: bool = False,
    ) -> None:
        """Present controller-selected columns without retaining the model."""
        if self.source_owner is not None:
            self.source_owner.release_for_legacy()
        self.statusBar().clearMessage()
        self.pages.setCurrentIndex(0)
        self.right_panel.table.setEditTriggers(self.right_panel.legacy_edit_triggers)
        self.right_panel.set_table(headers, rows)
        choices = self.left_panel.list_widget
        with QSignalBlocker(choices):
            self.left_panel.set_columns([name for _, name in columns])
            for row, (index, _) in enumerate(columns):
                choices.item(row).setData(Qt.ItemDataRole.UserRole, index)
            selected = next(
                (
                    i
                    for i, (_, name) in enumerate(columns)
                    if prefer_measurement and name.casefold() != "timestamp_ms"
                ),
                0 if columns else -1,
            )
            choices.setCurrentRow(selected)
        self._select_column(selected)
        if not columns:
            self.right_panel.plot_widget.setTitle(
                "No numeric readings to plot - view the raw data in the table"
            )
        self.source_label.setText("Local CSV loaded. No live sensor connection.")

    def plot_column(
        self, column_name: str, values: NumericColumn | None, timestamp_values: NumericColumn | None
    ) -> None:
        self.right_panel.plot_column(column_name, values, timestamp_values)

    def show_load_error(self, file_path: str, message: str) -> None:
        self.statusBar().showMessage(f"Could not load {file_path}: {message}")
        self.csv_load_failed.emit(file_path, message)

    def cancel_import(self) -> None:
        if self._controller is not None:
            self._controller.cancel_import()

    def set_import_state(self, state: str, message: str) -> None:
        self.import_state = state
        self.import_message.setText(message)
        loading = state == "loading"
        self.open_button.setEnabled(not loading)
        self.demo_button.setEnabled(not loading)
        self.cancel_button.setVisible(loading)
        self.progress.setVisible(loading)
        self.details_button.setVisible(state == "error")
        self.copy_button.setVisible(state == "error")
        self.error_details.hide()
        if state != "error":
            self.error_details.clear()
        menu = self.menuBar()
        if menu is not None:
            menu.actions()[0].setEnabled(not loading)

    def show_import_error(self, reason: str, recovery: str, details: str = "") -> None:
        self.set_import_state("error", reason + " " + recovery)
        self.error_details.setPlainText(
            "CSV import failed\nReason: " + reason + "\nRecovery: " + recovery + "\n" + details
        )
        self.error_details.show()
        self.details_button.setText("Hide error details")
        self.import_failed.emit(reason, recovery)

    def toggle_error_details(self) -> None:
        show = self.error_details.isHidden()
        self.error_details.setVisible(show)
        self.details_button.setText("Hide error details" if show else "Show error details")

    def copy_error(self) -> None:
        QApplication.clipboard().setText(self.error_details.toPlainText())
        self.statusBar().showMessage("Error details copied locally.")

    def try_demo(self) -> None:
        if self._controller is not None:
            self._controller.try_demo()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._controller is not None and not self._controller.close():
            self.import_message.setText(
                "Waiting for the file reader to stop. Try closing again shortly."
            )
            event.ignore()
            return
        if self.source_owner is not None and not self.source_owner.close():
            event.ignore()
            return
        super().closeEvent(event)

    def show_source_name(self, file_name: str) -> None:
        """Name the file whose data is currently shown."""

        self.setWindowTitle(f"LBR Dashboard - {file_name}")

    def display_telemetry_source(self) -> None:
        """Return to the generic telemetry view after a source starts."""
        self.pages.setCurrentIndex(0)
        self.statusBar().clearMessage()
        self.setWindowTitle("LBR Dashboard")
        self.source_label.setText("Readings from the telemetry source selected above.")
        if self.import_state != "loading":
            self.set_import_state("ready", "Browse a signal to inspect its readings and replay.")

    def import_progress(self, file_name: str) -> ImportProgress:
        """Progress for a long import; the caller closes it when done."""

        return ImportProgress(self, file_name)

    def display_lbr_session(self, session: LbrSession) -> None:
        """Show an imported firmware session in the session view."""

        if self.source_owner is not None:
            self.source_owner.release_for_legacy()
        self.statusBar().clearMessage()
        self.lbr_view.set_session(session)
        self.pages.setCurrentWidget(self.lbr_view)

    def show_lbr_import_error(self, message: str) -> None:
        """Explain a failed import without blocking; the current view is unchanged."""

        box = QMessageBox(QMessageBox.Icon.Warning, "Could not open file", message, parent=self)
        box.setInformativeText("The previously opened data is still shown.")
        box.setModal(False)
        box.show()
        self.import_error = box
