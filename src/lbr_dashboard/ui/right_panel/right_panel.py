"""Plot and raw-data panel backed by PyQtGraph."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QLabel,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core.log import NumericColumn

SYMBOL_LIMIT = 5_000


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
        self.table_summary = QLabel()
        layout.addWidget(self.table_summary)
        layout.addWidget(splitter)
        self.setStyleSheet("background-color: #3a3a3a; color: #ffffff;")

    def set_table(self, headers: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
        """Render rows without retaining the imported model or numeric arrays."""

        columns = min(len(headers), 256)
        shown = min(len(rows), 1000, 50000 // max(columns, 1))
        self.table.clearContents()
        self.table.setColumnCount(columns)
        self.table.setRowCount(shown)
        self.table.setHorizontalHeaderLabels(list(headers[:columns]))
        self.table_summary.setText(
            f"Showing {shown:,} of {len(rows):,} rows and {columns:,} of {len(headers):,} columns. "
            "Plots use the full imported series."
        )
        for row_index, row in enumerate(rows[:shown]):
            for column_index, value in enumerate(row[:columns]):
                self.table.setItem(row_index, column_index, QTableWidgetItem(value))

    def highlight_column(self, column_index: int) -> None:
        self._reset_plot()
        if column_index < 0:
            return
        self.table.clearSelection()
        self.table.selectColumn(column_index)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectColumns)

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

        ys = np.asarray(values, dtype=np.float64)
        if timestamp_values is not None and column_name.casefold() != "timestamp_ms":
            xs = np.asarray(timestamp_values, dtype=np.float64)
            valid_times = np.isfinite(xs)
            if valid_times.any():
                with np.errstate(over="ignore", invalid="ignore"):
                    xs -= np.min(xs, where=valid_times, initial=np.inf)
            x_label = "Time since start (ms)"
        else:
            xs = np.arange(ys.size, dtype=np.float64)
            x_label = "Sample"

        count = min(xs.size, ys.size)
        xs, ys = xs[:count], ys[:count]
        valid = np.isfinite(xs) & np.isfinite(ys)
        self._reset_plot()
        if not valid.any():
            self.plot_widget.setTitle(f"{column_name} (no numeric data)", color="#aaaaaa")
            return

        self.plot_widget.plot(
            xs[valid],
            ys[valid],
            pen=pg.mkPen("#00aaff", width=2),
            symbol="o" if np.count_nonzero(valid) <= SYMBOL_LIMIT else None,
            symbolSize=6,
            symbolBrush="#00aaff",
        )
        self.plot_widget.enableAutoRange()
        self.plot_widget.setTitle(column_name, color="#ffffff")
        self.plot_widget.setLabel("bottom", x_label)
        self.plot_widget.setLabel("left", column_name)
