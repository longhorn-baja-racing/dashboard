"""Read-only text readout of one source health snapshot."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.source_health import SourceHealthSnapshot, format_source_health, status_text

EMPTY_MESSAGE = "No source"
EMPTY_STATUS = "Status: unknown (no health data)"


class SourceHealthPanel(QWidget):
    """Show a snapshot as words: state, age, rate, loss, errors and unknowns.

    The status label carries a ``healthStatus`` property (for example
    ``"stale"``) that a style sheet may use, but the text alone always tells
    the states apart.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[tuple[str, str]] = []
        self.title = QLabel(EMPTY_MESSAGE)
        self.status = QLabel(EMPTY_STATUS)
        self.status.setProperty("healthStatus", "unknown")
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Health", "Value"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setWordWrap(True)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.title)
        layout.addWidget(self.status)
        layout.addWidget(self.table)

    def show_snapshot(self, snapshot: SourceHealthSnapshot) -> None:
        self.title.setText(f"Source: {snapshot.source_id}")
        self.status.setText(f"Status: {status_text(snapshot)}")
        self.status.setProperty("healthStatus", snapshot.status.value)
        self._set_rows(format_source_health(snapshot))

    def clear(self) -> None:
        self.title.setText(EMPTY_MESSAGE)
        self.status.setText(EMPTY_STATUS)
        self.status.setProperty("healthStatus", "unknown")
        self._set_rows([])

    def rows(self) -> list[tuple[str, str]]:
        """Return the displayed label/text pairs in order."""

        return list(self._rows)

    def _set_rows(self, rows: list[tuple[str, str]]) -> None:
        self._rows = rows
        self.table.setRowCount(len(rows))
        for index, (label, text) in enumerate(rows):
            self.table.setItem(index, 0, QTableWidgetItem(label))
            self.table.setItem(index, 1, QTableWidgetItem(text))
        self.table.resizeRowsToContents()
