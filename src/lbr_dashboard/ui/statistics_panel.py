"""Read-only text readout for a current value and interval statistics."""

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

from ..core.statistics import (
    CurrentValue,
    IntervalStatistics,
    format_current_value,
    format_statistics,
)

EMPTY_MESSAGE = "No interval selected"


class StatisticsPanel(QWidget):
    """Show formatted statistics rows; every state is spelled out as text."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[tuple[str, str]] = []
        self.title = QLabel(EMPTY_MESSAGE)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Quantity", "Value"])
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
        layout.addWidget(self.table)

    def show_statistics(
        self, stats: IntervalStatistics, current: CurrentValue | None = None
    ) -> None:
        rows = format_current_value(current) if current is not None else []
        rows.extend(format_statistics(stats))
        self.title.setText("Interval statistics")
        self._set_rows(rows)

    def clear(self) -> None:
        self.title.setText(EMPTY_MESSAGE)
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
