"""Temporary dashboard shell for the P0 foundation milestone."""

from __future__ import annotations

from math import isfinite
from pathlib import Path

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtGui import QCloseEvent, QColor, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..core.log import LogImporter, LogSession, LogSessionStore
from ..import_controller import ImportController, demo_session
from .constants import MIN_PANEL_WIDTH
from .left_panel import LeftPanel
from .right_panel import RightPanel
from .top_menu_bar import TopMenuBar


class MainWindow(QMainWindow):
    """Main application window that coordinates imported data and presentation."""

    import_failed = Signal(str, str)

    def __init__(self, importer: LogImporter) -> None:
        super().__init__()
        palette = self.palette()
        for role in (QPalette.ColorRole.Window, QPalette.ColorRole.Button):
            palette.setColor(role, QColor("#303030"))
        palette.setColor(QPalette.ColorRole.Base, QColor("#202020"))
        for role in (
            QPalette.ColorRole.WindowText,
            QPalette.ColorRole.Text,
            QPalette.ColorRole.ButtonText,
        ):
            palette.setColor(role, QColor("#eeeeee"))
        self.setPalette(palette)
        self.setStyleSheet(
            "QFrame#importCard { border: 1px solid #555; border-radius: 6px; }"
            "QPushButton { padding: 6px 12px; }"
        )
        self._log_store = LogSessionStore(importer)
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
        layout.addWidget(card)
        layout.addWidget(splitter, 1)
        self.setCentralWidget(root)
        self.import_controller = ImportController(self)
        self.open_button.clicked.connect(self.open_csv)
        self.demo_button.clicked.connect(self.try_demo)
        self.cancel_button.clicked.connect(self.import_controller.cancel)
        self.details_button.clicked.connect(self.toggle_error_details)
        self.copy_button.clicked.connect(self.copy_error)
        self.set_import_state(
            "empty",
            "Open a UTF-8 CSV, select a column, and inspect its values. "
            "timestamp_ms is interpreted in milliseconds; other files use sample index.",
        )

        self.left_panel.list_widget.currentRowChanged.connect(self._select_column)
        self.right_panel.plot_widget.setTitle("No data loaded — open CSV or try demo data")
        self.right_panel.table.setEditTriggers(self.right_panel.table.EditTrigger.NoEditTriggers)

    def _select_column(self, column_index: int) -> None:
        """Highlight and plot the selected column, including the first selection."""

        if column_index < 0:
            self.right_panel.plot_widget.clear()
            return

        item = self.left_panel.list_widget.item(column_index)
        if item is not None:
            self.right_panel.highlight_column(item.data(Qt.ItemDataRole.UserRole))
            column_name = item.text()
            self.right_panel.plot_column(
                column_name,
                self._log_store.numeric_column(column_name),
                self._log_store.numeric_column("timestamp_ms"),
            )
            values = self._log_store.numeric_column(column_name)
            if values and all(value is None for value in values):
                self.statusBar().showMessage(
                    "This column has no numeric readings. Inspect its raw values in the table."
                )
            else:
                self.statusBar().clearMessage()

    def open_csv(self) -> None:
        """Open a CSV file and display its columns and rows."""

        file_path, _ = QFileDialog.getOpenFileName(self, "Open CSV File", "", "CSV Files (*.csv)")
        if file_path:
            self.import_controller.start(Path(file_path))

    def load_csv(self, file_path: Path) -> None:
        """Ask the importer to decode a CSV, then pass its model to the panels."""

        if not self._log_store.load(file_path):
            return

        self.right_panel.set_table(self._log_store.headers, self._log_store.rows)
        self._set_plot_columns()
        self.left_panel.list_widget.setCurrentRow(0)
        self._select_column(0)
        self.source_label.setText("Local CSV loaded. No live sensor connection.")
        self.set_import_state("ready", "CSV loaded. Select a numeric column to plot.")

    def present_session(self, session: LogSession) -> None:
        self._log_store.replace(session)
        self.right_panel.set_table(session.headers, session.rows)
        self._set_plot_columns()
        choices = self.left_panel.list_widget
        column = next(
            (i for i in range(choices.count()) if choices.item(i).text() != "timestamp_ms"),
            0 if choices.count() else -1,
        )
        self.left_panel.list_widget.setCurrentRow(column)
        self._select_column(column)
        self.source_label.setText("Local CSV loaded. No live sensor connection.")

    def _set_plot_columns(self) -> None:
        choices = self.left_panel.list_widget
        columns = [
            (index, name)
            for index, name in enumerate(self._log_store.headers)
            if any(
                value is not None and isfinite(value)
                for value in self._log_store.numeric_column(name) or ()
            )
        ]
        with QSignalBlocker(choices):
            self.left_panel.set_columns([name for _, name in columns])
            for row, (index, _) in enumerate(columns):
                choices.item(row).setData(Qt.ItemDataRole.UserRole, index)
        self.right_panel.plot_widget.clear()
        if not columns:
            self.right_panel.plot_widget.setTitle(
                "No numeric readings to plot — view the raw data in the table"
            )

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

    def show_import_error(self, reason: str, recovery: str) -> None:
        self.set_import_state("error", reason + " " + recovery)
        self.error_details.setPlainText(
            "CSV import failed\nReason: " + reason + "\nRecovery: " + recovery
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
        if self.import_controller.worker is not None:
            return
        session = demo_session()
        self.present_session(session)
        self.source_label.setText("Fictional static demo CSV. No sensor is connected.")
        self.set_import_state(
            "ready",
            "Demo loaded: select temperature or pressure. "
            "Empty cells mean missing readings; 0 is a real value. "
            "This is a static preview, not a live sensor.",
        )

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self.import_controller.close():
            self.import_message.setText(
                "Waiting for the file reader to stop. Try closing again shortly."
            )
            event.ignore()
            return
        super().closeEvent(event)
