"""Signal-list panel."""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QListWidget, QVBoxLayout, QWidget

from ..signal_browser import SignalBrowser


class LeftPanel(QWidget):
    """Display the currently available telemetry columns."""

    column_selected = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.list_widget = QListWidget()
        self.signal_browser = SignalBrowser()
        self.signal_browser.hide()
        self.list_widget.setStyleSheet(
            """
            QListWidget::item:selected { background-color: #0078d4; }
            QListWidget::item:hover { background-color: #444444; }
            """
        )
        self.list_widget.currentTextChanged.connect(self.column_selected.emit)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.list_widget)
        layout.addWidget(self.signal_browser)
        self.setStyleSheet("background-color: #2e2e2e; color: #ffffff;")

    def set_columns(self, columns: list[str]) -> None:
        """Replace the displayed column names."""

        self.signal_browser.hide()
        self.list_widget.show()
        self.list_widget.clear()
        self.list_widget.addItems(columns)
