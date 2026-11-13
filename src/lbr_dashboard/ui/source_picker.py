"""Source actions and presentation; lifecycle decisions belong to the application."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from ..core import SourceCapabilities


class SourcePicker(QGroupBox):
    connect_requested = Signal(str)
    disconnect_requested = Signal()
    retry_requested = Signal()
    seek_requested = Signal()
    rate_requested = Signal()

    def __init__(self) -> None:
        super().__init__("Telemetry source")
        self.choices = QComboBox()
        self.choices.addItem("CSV replay - timestamp_ms preset", "csv")
        self.choices.addItem("Simulator - process (finite)", "process")
        self.choices.addItem("Simulator - motion (finite)", "motion")
        self.choices.addItem("Simulator - process (live)", "live")
        self.connect_button = QPushButton("Open CSV / Connect")
        self.disconnect_button = QPushButton("Disconnect")
        self.retry_button = QPushButton("Retry / Restart")
        self.status = QLabel("Idle - choose a CSV or fictional simulator. No hardware connected.")
        self.status.setWordWrap(True)
        self.capabilities = QLabel("Capabilities will appear when a source is selected.")
        self.capabilities.setWordWrap(True)
        self.controls = QLabel()
        self.controls.setWordWrap(True)
        for label in (self.status, self.capabilities, self.controls):
            label.setTextFormat(Qt.TextFormat.PlainText)
        self.seek_button = QPushButton("Source: seek to first reading")
        self.rate_button = QPushButton("Source: set rate to 2 Hz")
        self._busy = False
        row = QHBoxLayout()
        for widget in (
            self.choices,
            self.connect_button,
            self.disconnect_button,
            self.retry_button,
        ):
            row.addWidget(widget)
        layout = QVBoxLayout(self)
        layout.addLayout(row)
        layout.addWidget(self.status)
        layout.addWidget(self.capabilities)
        layout.addWidget(self.controls)
        operations = QHBoxLayout()
        operations.addWidget(self.seek_button)
        operations.addWidget(self.rate_button)
        layout.addLayout(operations)
        hint = QLabel(
            "Disconnect releases the source and keeps readings for inspection. "
            "Plot: latest 1,000 selected readings; table: latest 100. "
            "Live history is limited to 1,000 readings per signal."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.connect_button.clicked.connect(
            lambda: self.connect_requested.emit(self.choices.currentData())
        )
        self.disconnect_button.clicked.connect(self.disconnect_requested)
        self.retry_button.clicked.connect(self.retry_requested)
        self.seek_button.clicked.connect(self.seek_requested)
        self.rate_button.clicked.connect(self.rate_requested)
        self.set_actions(busy=False, connected=False, retry=False)
        self.show_capabilities(None)

    def set_actions(self, *, busy: bool, connected: bool, retry: bool) -> None:
        self._busy = busy
        self.connect_button.setEnabled(not busy)
        self.choices.setEnabled(not busy)
        self.disconnect_button.setEnabled(connected)
        self.retry_button.setEnabled(retry and not busy)

    def show_capabilities(self, caps: SourceCapabilities | None) -> None:
        for button, supported, name in (
            (self.seek_button, caps is not None and caps.supports_seek, "seek"),
            (self.rate_button, caps is not None and caps.supports_rate_control, "rate control"),
        ):
            button.setEnabled(supported and not self._busy)
            button.setToolTip(
                f"Adapter {name} operation."
                if supported
                else f"This source does not support {name}. Replay controls are separate."
            )
        if caps is None:
            self.capabilities.setText("No active source.")
            self.controls.setText("USB/serial is not available; no physical port is selected.")
            return
        self.capabilities.setText(
            f"{'Live' if caps.is_live else 'Finite replay'} | bounded queue: "
            f"{caps.buffer_capacity} events | source seek: "
            f"{'supported' if caps.supports_seek else 'unsupported'} | source rate control: "
            f"{'supported' if caps.supports_rate_control else 'unsupported'}"
        )
        self.controls.setText(
            "This adapter has no seek/rate operations. Retry restarts finite input. "
            "USB/serial is not available."
            if not caps.supports_seek and not caps.supports_rate_control
            else "Source capabilities are reported above. Replay controls are a separate feature."
        )
