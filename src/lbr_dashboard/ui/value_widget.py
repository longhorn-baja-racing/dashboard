"""Current-value readout for one signal, selected by its generic signal ID."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QFormLayout, QLabel, QVBoxLayout, QWidget

from ..core.value_display import EMPTY_TEXT, STATE_LABELS, ValueDisplay, ValueState

NO_UNIT_TEXT = "(none)"


class SignalValueWidget(QWidget):
    """Show one ``ValueDisplay`` record for the signal ID assigned to it.

    The state is always written out as text (``[STALE]``, ``[NO DATA]``, ...)
    and values that are not current are shown in italics, so the readout never
    depends on color. The widget knows nothing about sources or adapters; it
    only renders records produced by ``describe_value``.
    """

    def __init__(self, signal_id: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._signal_id = signal_id
        self._display: ValueDisplay | None = None

        self.name_label = QLabel()
        self.state_label = QLabel()
        self.value_label = QLabel()
        self.identity_label = QLabel()
        self.type_label = QLabel()
        self.unit_label = QLabel()
        self.time_label = QLabel()
        self.quality_label = QLabel()

        title_font = QFont(self.name_label.font())
        title_font.setBold(True)
        self.name_label.setFont(title_font)
        self.state_label.setFont(title_font)
        self._value_font = QFont(self.value_label.font())
        self._value_font.setPointSizeF(max(self._value_font.pointSizeF(), 1.0) * 1.5)
        self._value_font.setBold(True)
        self.quality_label.setWordWrap(True)
        self.value_label.setWordWrap(True)
        for label in (self.value_label, self.identity_label, self.time_label):
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        details = QFormLayout()
        details.addRow("Signal", self.identity_label)
        details.addRow("Type", self.type_label)
        details.addRow("Unit", self.unit_label)
        details.addRow("Time", self.time_label)
        details.addRow("Quality", self.quality_label)

        layout = QVBoxLayout(self)
        layout.addWidget(self.name_label)
        layout.addWidget(self.state_label)
        layout.addWidget(self.value_label)
        layout.addLayout(details)
        self.clear()

    @property
    def signal_id(self) -> str:
        return self._signal_id

    @property
    def display(self) -> ValueDisplay | None:
        """The record currently shown, or None before the first update."""

        return self._display

    def set_signal_id(self, signal_id: str) -> None:
        """Assign another signal; the readout returns to the no-data state."""

        self._signal_id = signal_id
        self.clear()

    def show_display(self, display: ValueDisplay) -> bool:
        """Show ``display`` if it belongs to this widget's signal.

        Records for other signal IDs are ignored and False is returned, so one
        update can be offered to every value widget.
        """

        if display.signal_id != self._signal_id:
            return False
        self._display = display
        self.name_label.setText(display.name_text)
        self.identity_label.setText(display.identity_text)
        self.type_label.setText(display.type_text)
        self.unit_label.setText(display.unit_text or NO_UNIT_TEXT)
        self._show_state(display.state, display.value_text)
        self.time_label.setText(display.time_text)
        self.quality_label.setText(display.quality_text)
        return True

    def clear(self) -> None:
        """Show the assigned signal ID with no data."""

        self._display = None
        self.name_label.setText(self._signal_id)
        self.identity_label.setText(self._signal_id)
        self.type_label.setText(EMPTY_TEXT)
        self.unit_label.setText(EMPTY_TEXT)
        self._show_state(ValueState.NO_DATA, EMPTY_TEXT)
        self.time_label.setText(EMPTY_TEXT)
        self.quality_label.setText("no sample yet")

    def _show_state(self, state: ValueState, value_text: str) -> None:
        self.state_label.setText(f"[{STATE_LABELS[state]}]")
        font = QFont(self._value_font)
        font.setItalic(state is not ValueState.OK)
        self.value_label.setFont(font)
        self.value_label.setText(value_text)
        self.setProperty("valueState", state.value)
        self.setAccessibleName(f"{self.name_label.text()}: {value_text}, {STATE_LABELS[state]}")
