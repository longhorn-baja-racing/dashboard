"""Source actions and presentation; lifecycle decisions belong to the application."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core import SourceCapabilities

#: Choice key of the live USB serial source; the application registers it.
SERIAL_KEY = "serial"
NO_PORTS = (
    "No serial ports found. Connect the Central's USB cable (install the USB-UART "
    "driver, e.g. CP210x or CH340, if Windows shows no COM port), then click Refresh ports."
)


class SourcePicker(QGroupBox):
    connect_requested = Signal(str)
    disconnect_requested = Signal()
    retry_requested = Signal()
    seek_requested = Signal()
    rate_requested = Signal()
    ports_refresh_requested = Signal()
    record_toggled = Signal(bool)

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
        # USB serial: a port chooser shown while that source is selected.
        self.port_row = QWidget()
        self.ports = QComboBox()
        self.ports.setMinimumContentsLength(16)
        self.refresh_ports_button = QPushButton("Refresh ports")
        self.port_note = QLabel()
        self.port_note.setWordWrap(True)
        self.port_note.setTextFormat(Qt.TextFormat.PlainText)
        port_layout = QVBoxLayout(self.port_row)
        port_layout.setContentsMargins(0, 0, 0, 0)
        port_line = QHBoxLayout()
        port_line.addWidget(QLabel("Serial port:"))
        port_line.addWidget(self.ports, 1)
        port_line.addWidget(self.refresh_ports_button)
        port_layout.addLayout(port_line)
        port_layout.addWidget(self.port_note)
        self.port_row.hide()
        # Recording of a live source to a file.
        self.record_button = QPushButton("Record")
        self.record_button.setCheckable(True)
        self.recording = QLabel()
        self.recording.setWordWrap(True)
        self.recording.setTextFormat(Qt.TextFormat.PlainText)
        self.recording.hide()
        self._has_ports = False
        self._can_record = False
        self.seek_button = QPushButton("Source: seek to first reading")
        self.rate_button = QPushButton("Source: set rate to 2 Hz")
        self._busy = False
        row = QHBoxLayout()
        for widget in (
            self.choices,
            self.connect_button,
            self.disconnect_button,
            self.retry_button,
            self.record_button,
        ):
            row.addWidget(widget)
        layout = QVBoxLayout(self)
        layout.addLayout(row)
        layout.addWidget(self.port_row)
        layout.addWidget(self.recording)
        layout.addWidget(self.status)
        layout.addWidget(self.capabilities)
        layout.addWidget(self.controls)
        operations = QHBoxLayout()
        operations.addWidget(self.seek_button)
        operations.addWidget(self.rate_button)
        layout.addLayout(operations)
        hint = QLabel(
            "Disconnect releases the source and keeps readings for inspection. "
            "Use Fit plot for the full recording. Table: latest 100 readings. "
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
        self.refresh_ports_button.clicked.connect(self.ports_refresh_requested)
        self.record_button.toggled.connect(self._record_toggled)
        self.choices.currentIndexChanged.connect(self._choice_changed)
        self.set_actions(busy=False, connected=False, retry=False)
        self.show_capabilities(None)
        self.set_recording(available=False, active=False)

    def serial_selected(self) -> bool:
        return self.choices.currentData() == SERIAL_KEY

    def _choice_changed(self) -> None:
        serial = self.serial_selected()
        self.port_row.setVisible(serial)
        if serial:
            self.ports_refresh_requested.emit()
        self._update_connect()

    def _update_connect(self) -> None:
        self.connect_button.setEnabled(
            not self._busy and (self._has_ports or not self.serial_selected())
        )
        self.connect_button.setToolTip(
            NO_PORTS if self.serial_selected() and not self._has_ports else ""
        )

    def set_ports(self, ports: list[tuple[str, str]], note: str = "") -> None:
        """Offer ``(label, port name)`` pairs, keeping the current port if it is still there."""

        current = self.ports.currentData()
        self.ports.blockSignals(True)
        self.ports.clear()
        for label, name in ports:
            self.ports.addItem(label, name)
        index = self.ports.findData(current)
        self.ports.setCurrentIndex(index if index >= 0 else 0)
        self.ports.blockSignals(False)
        self._has_ports = bool(ports)
        self.ports.setEnabled(self._has_ports)
        if not ports:
            self.ports.addItem("No serial ports found", None)
        self.port_note.setText(
            note
            or (
                NO_PORTS
                if not ports
                else "Read-only: the dashboard only listens at 921600 baud and never sends "
                "data or commands to the vehicle."
            )
        )
        self._update_connect()

    def selected_port(self) -> str | None:
        data = self.ports.currentData()
        return data if self._has_ports and isinstance(data, str) else None

    def _record_toggled(self, checked: bool) -> None:
        self.record_toggled.emit(checked)

    def set_recording(
        self, *, available: bool, active: bool, text: str = "", tooltip: str = ""
    ) -> None:
        """Show the recording state without emitting ``record_toggled``.

        ``text`` is the recording line (file, size, duration, loss); it is
        hidden while empty so the panel stays compact for other sources.
        """

        self._can_record = available
        self.record_button.blockSignals(True)
        self.record_button.setChecked(active)
        self.record_button.blockSignals(False)
        self.record_button.setText("Stop recording" if active else "Record")
        self.record_button.setEnabled(active or (available and not self._busy))
        self.record_button.setToolTip(
            tooltip
            or (
                "Save what the live source receives to an .lbrlog file for later replay."
                if available
                else "Recording is available while a USB serial source is connected."
            )
        )
        if text != self.recording.text():
            self.recording.setText(text)
        self.recording.setVisible(bool(text))

    def set_actions(self, *, busy: bool, connected: bool, retry: bool) -> None:
        self._busy = busy
        self._update_connect()
        self.choices.setEnabled(not busy)
        self.ports.setEnabled(not busy and self._has_ports)
        self.refresh_ports_button.setEnabled(not busy)
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
            self.controls.setText(
                "Live USB serial: choose USB serial (firmware live) and a port. "
                "The link is read-only."
            )
            return
        self.capabilities.setText(
            f"{'Live' if caps.is_live else 'Finite replay'} | bounded queue: "
            f"{caps.buffer_capacity} events | source seek: "
            f"{'supported' if caps.supports_seek else 'unsupported'} | source rate control: "
            f"{'supported' if caps.supports_rate_control else 'unsupported'}"
        )
        self.controls.setText(
            (
                "Live source: no seek/rate operations. Retry reconnects."
                if caps.is_live
                else "This adapter has no seek/rate operations. Retry restarts finite input."
            )
            if not caps.supports_seek and not caps.supports_rate_control
            else "Source capabilities are reported above. Replay controls are a separate feature."
        )
