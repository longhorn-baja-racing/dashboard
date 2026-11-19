"""Plot and raw-data panel backed by PyQtGraph."""

from __future__ import annotations

from collections.abc import Sequence
from math import isfinite

import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget

from ...core.log import NumericColumn


class RightPanel(QWidget):
    """Display a selected column as a plot and retain the CSV table view."""

    def __init__(self) -> None:
        super().__init__()
        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setBackground("#3a3a3a")
        self.plot_widget.showGrid(x=True, y=True, alpha=0.2)
        self.plot_widget.setTitle("Select a column to plot", color="#aaaaaa")
        self.plot_widget.getAxis("bottom").setPen(pg.mkPen("#cccccc"))
        self.plot_widget.getAxis("left").setPen(pg.mkPen("#cccccc"))

        self.table = QTableWidget()
        self.table.setStyleSheet(
            """
            QTableWidget { background-color: #2a2a2a; color: #ffffff; gridline-color: #555555; }
            QHeaderView::section {
                background-color: #3a3a3a;
                color: #ffffff;
                border: 1px solid #555555;
            }
            """
        )

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.plot_widget)
        splitter.addWidget(self.table)
        splitter.setSizes([350, 350])
        splitter.setStyleSheet("QSplitter::handle { background-color: #555555; }")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)
        self.setStyleSheet("background-color: #3a3a3a; color: #ffffff;")

    def set_table(self, headers: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
        """Render rows without retaining the imported model or numeric arrays."""

        self.table.clearContents()
        self.table.setColumnCount(len(headers))
        self.table.setRowCount(len(rows))
        self.table.setHorizontalHeaderLabels(list(headers))
        for row_index, row in enumerate(rows):
            for column_index, value in enumerate(row[: len(headers)]):
                self.table.setItem(row_index, column_index, QTableWidgetItem(value))

    def highlight_column(self, column_index: int) -> None:
        self._reset_plot()
        if column_index < 0:
            return
        self.table.clearSelection()
        self.table.selectColumn(column_index)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectColumns)
        self._reset_plot()

    def _reset_plot(self) -> None:
        self.plot_widget.clear()
        self.plot_widget.enableAutoRange()
        self.plot_widget.setTitle("Select a column to plot", color="#aaaaaa")

    def plot_column(
        self,
        column_name: str,
        values: NumericColumn | None,
        timestamp_values: NumericColumn | None,
    ) -> None:
        """Plot the named numeric column."""

        if values is None:
            return

        if timestamp_values is not None and column_name != "timestamp_ms":
            timestamps = [
                value for value in timestamp_values if value is not None and isfinite(value)
            ]
            start_time = min(timestamps) if timestamps else 0.0
            x_values = [
                value - start_time if value is not None and isfinite(value) else None
                for value in timestamp_values
            ]
            x_label = "Time since start (ms)"
        else:
            x_values = [float(index) for index in range(len(values))]
            x_label = "Sample"

        pairs = [
            (x_value, y_value)
            for x_value, y_value in zip(x_values, values)
            if x_value is not None
            and y_value is not None
            and isfinite(x_value)
            and isfinite(y_value)
        ]
        self._reset_plot()
        if not pairs:
            self.plot_widget.setTitle(f"{column_name} (no numeric data)", color="#aaaaaa")
            return

        xs, ys = zip(*pairs)
        self.plot_widget.plot(
            list(xs),
            list(ys),
            pen=pg.mkPen("#00aaff", width=2),
            symbol="o",
            symbolSize=6,
            symbolBrush="#00aaff",
        )
        self.plot_widget.enableAutoRange()
        self.plot_widget.setTitle(column_name, color="#ffffff")
        self.plot_widget.setLabel("bottom", x_label)
        self.plot_widget.setLabel("left", column_name)
