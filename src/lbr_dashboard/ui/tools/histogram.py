"""Histogram tool: sample counts per value bin for the selected signal and interval."""

from __future__ import annotations

from collections.abc import Mapping

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core.histogram import DEFAULT_BINS, EDGE_RULE, Histogram, histogram
from ..analysis import AnalysisContext, AnalysisTool, is_numeric, refresh_soon
from ..histogram_widget import HistogramWidget

MAX_BINS = 500


def format_edge(value: float) -> str:
    """Show an edge with enough digits that neighbouring edges stay distinct."""

    return f"{value:.10g}"


def bin_text(start: float, end: float, closed: str) -> str:
    """Spell a bin with its closed sides, e.g. ``[1, 2)`` or ``[2, 3]``."""

    right = "]" if closed == "both" else ")"
    return f"[{format_edge(start)}, {format_edge(end)}{right}"


def edges_text(hist: Histogram) -> str:
    """Describe the bin edges in one line, including the unit."""

    if not hist.edges:
        return "Bin edges: none (no valid samples to build bins from)"
    unit = f" {hist.unit}" if hist.unit else ""
    return (
        f"Bin edges: {format_edge(hist.edges[0])} to {format_edge(hist.edges[-1])}{unit}, "
        f"{len(hist.counts)} bins of width "
        f"{format_edge((hist.edges[-1] - hist.edges[0]) / len(hist.counts))}{unit}. "
        f"{EDGE_RULE}"
    )


class HistogramTool(QWidget):
    """Histogram the selected numeric signal over the shared interval, or the whole session.

    Missing (no value or NaN) and non-finite readings are counted beside the
    bins and never enter them. The bin count is the tool's only setting; it is
    available as a plain dict through :meth:`configuration`.
    """

    def __init__(self, context: AnalysisContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self.result: Histogram | None = None
        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.bins = QSpinBox()
        self.bins.setRange(1, MAX_BINS)
        self.bins.setValue(DEFAULT_BINS)
        self.bins.setAccessibleName("Number of bins")
        bins_label = QLabel("Bins:")
        bins_label.setBuddy(self.bins)
        self.edges = QLabel()
        self.edges.setWordWrap(True)
        self.view = HistogramWidget()
        self.view.setMinimumSize(320, 200)
        context.style_plot(self.view.plot_widget)
        self.table = QTableWidget(0, 2)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        self._set_headers("")

        controls = QHBoxLayout()
        controls.addWidget(bins_label)
        controls.addWidget(self.bins)
        controls.addStretch(1)
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.view)
        splitter.addWidget(self.table)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        splitter.setChildrenCollapsible(False)
        layout = QVBoxLayout(self)
        layout.addWidget(self.hint)
        layout.addLayout(controls)
        layout.addWidget(self.edges)
        layout.addWidget(splitter, 1)

        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.refresh)
        for change in (
            context.snapshot_changed,
            context.signal_changed,
            context.interval_changed,
        ):
            change.connect(refresh_soon(self.timer))
        self.bins.valueChanged.connect(refresh_soon(self.timer))
        self.refresh()

    def configuration(self) -> dict[str, object]:
        """Return the tool's settings as plain data, e.g. for a saved workspace."""

        return {"bins": self.bins.value()}

    def restore_configuration(self, config: Mapping[str, object]) -> None:
        """Apply settings from :meth:`configuration`; unknown or invalid entries are ignored."""

        bins = config.get("bins")
        if isinstance(bins, int) and not isinstance(bins, bool) and 1 <= bins <= MAX_BINS:
            self.bins.setValue(bins)

    def rows(self) -> list[tuple[str, str]]:
        """Return the bin table as text, in display order."""

        result: list[tuple[str, str]] = []
        for row in range(self.table.rowCount()):
            cells = [self.table.item(row, column) for column in range(2)]
            first, second = (cell.text() if cell is not None else "" for cell in cells)
            result.append((first, second))
        return result

    def refresh(self) -> None:
        self.timer.stop()
        context = self.context
        descriptor = context.descriptor(context.selected_id)
        if descriptor is None:
            self._show_nothing("Select a numeric signal in the browser to see its histogram.")
            return
        if not is_numeric(descriptor):
            self._show_nothing(
                f"{descriptor.display_name} is {descriptor.data_type}; "
                "a histogram needs a numeric signal."
            )
            return
        interval = context.interval
        span = interval if interval is not None else context.bounds
        times, values = context.numeric_series(descriptor.identifier, interval)
        unit = descriptor.canonical_unit or ""
        bins = self.bins.value()
        try:
            result = histogram(
                times,
                values,
                bins=bins,
                start_ns=span.start_ns if span is not None else None,
                end_ns=span.end_ns if span is not None else None,
                unit=unit,
            )
        except ValueError as error:
            self._show_nothing(
                f"{descriptor.display_name}: cannot split the values into {bins} bins "
                f"({error}). Choose fewer bins or a wider interval."
            )
            return
        scope = (
            "the selected time interval"
            if interval is not None
            else "the whole session (no time interval selected)"
        )
        unit_text = unit if unit else "no unit"
        self.hint.setText(
            f"{descriptor.display_name} ({descriptor.identifier}), unit: {unit_text}, "
            f"over {scope}. Each valid sample counts once."
        )
        self.result = result
        self.view.show_histogram(result, descriptor.display_name)
        self.edges.setText(edges_text(result))
        self._set_headers(unit)
        self.table.setRowCount(len(result.counts))
        for index, row in enumerate(result.to_rows()):
            self.table.setItem(
                index, 0, QTableWidgetItem(bin_text(row.bin_start, row.bin_end, row.closed))
            )
            self.table.setItem(index, 1, QTableWidgetItem(str(row.count)))

    def _show_nothing(self, message: str) -> None:
        self.hint.setText(message)
        self.result = None
        self.view.clear()
        self.edges.setText("")
        self._set_headers("")
        self.table.setRowCount(0)

    def _set_headers(self, unit: str) -> None:
        self.table.setHorizontalHeaderLabels([f"Bin ({unit})" if unit else "Bin", "Samples"])


TOOL = AnalysisTool("histogram", "Histogram", HistogramTool)
