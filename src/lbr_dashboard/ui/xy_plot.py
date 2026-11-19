"""Scatter view of paired signals with alignment rules shown as text."""

from __future__ import annotations

from bisect import bisect_left

import pyqtgraph as pg
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from ..core.xy_pairing import AlignmentPolicy, PairedSeries, limit_points

DEFAULT_MAX_POINTS = 20_000

_POLICY_TEXT = {
    AlignmentPolicy.NEAREST: "nearest {other} within {tolerance} of each {driver}",
    AlignmentPolicy.PREVIOUS: "latest {other} at or before each {driver}, at most {tolerance} old",
}


def axis_label(name: str, unit: str | None) -> str:
    """Return ``name (unit)``, or just ``name`` when the unit is unknown."""

    return f"{name} ({unit})" if unit else name


def format_duration_ns(duration_ns: int) -> str:
    """Format nanoseconds in the largest unit that keeps the value exact."""

    for size, suffix in ((1_000_000_000, "s"), (1_000_000, "ms"), (1_000, "us")):
        if duration_ns and duration_ns % size == 0:
            return f"{duration_ns // size} {suffix}"
    return f"{duration_ns} ns"


def pairing_note(series: PairedSeries, shown: int) -> str:
    """Describe how ``series`` was paired, what was excluded, and what is drawn."""

    driver, other = ("X", "Y") if series.driver == "x" else ("Y", "X")
    rule = _POLICY_TEXT[series.policy].format(
        driver=driver, other=other, tolerance=format_duration_ns(series.tolerance_ns)
    )
    excluded = series.excluded
    lines = [
        f"Pairing: {rule}.",
        f"Showing {shown} of {len(series)} pairs.",
        f"Excluded: {excluded.x_missing} X missing, {excluded.y_missing} Y missing, "
        f"{excluded.no_match} no {other} within tolerance.",
    ]
    return "\n".join(lines)


class XYPlotWidget(QWidget):
    """Scatter plot of one :class:`PairedSeries` with a text note below it.

    The scatter draws at most ``max_points`` pairs (see ``limit_points``).
    Pan and zoom use the PyQtGraph defaults.
    """

    def __init__(self, parent: QWidget | None = None, *, max_points: int = DEFAULT_MAX_POINTS):
        if max_points < 2:
            raise ValueError(f"max_points must be >= 2, got {max_points}")
        super().__init__(parent)
        self._max_points = max_points
        self._series: PairedSeries | None = None

        self.plot_widget = pg.PlotWidget()
        self.plot_widget.showGrid(x=True, y=True, alpha=0.2)
        self.scatter = pg.ScatterPlotItem(size=5, pen=None, brush=pg.mkBrush("#00aaff"))
        self.marker = pg.ScatterPlotItem(
            size=14, symbol="o", pen=pg.mkPen("#ffcc00", width=2), brush=None
        )
        self.marker.setVisible(False)
        self.plot_widget.addItem(self.scatter)
        self.plot_widget.addItem(self.marker)

        self.note_label = QLabel("No pairs to plot.")
        self.note_label.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.plot_widget)
        layout.addWidget(self.note_label)

    def show_pairs(self, series: PairedSeries, x_label: str, y_label: str) -> None:
        """Plot ``series`` with axis names and units; clears any highlight."""

        limited, _dropped = limit_points(series, self._max_points)
        self._series = series
        self.scatter.setData(x=list(limited.x), y=list(limited.y))
        self.marker.setVisible(False)
        self.plot_widget.setLabel("bottom", axis_label(x_label, series.x_unit))
        self.plot_widget.setLabel("left", axis_label(y_label, series.y_unit))
        self.plot_widget.enableAutoRange()
        self.note_label.setText(pairing_note(series, len(limited)))

    def highlight_time(self, t_ns: int | None) -> int | None:
        """Mark the pair whose driver time is nearest ``t_ns``; return that time.

        Ties pick the earlier pair. The marker is hidden and ``None`` returned
        when ``t_ns`` is ``None`` or there are no pairs. The search covers every
        pair, including pairs not drawn because of the point limit.
        """

        series = self._series
        if t_ns is None or series is None or not series.times_ns:
            self.marker.setVisible(False)
            return None
        times = series.times_ns
        index = bisect_left(times, t_ns)
        if index == len(times) or (index > 0 and t_ns - times[index - 1] <= times[index] - t_ns):
            index -= 1
        self.marker.setData(x=[series.x[index]], y=[series.y[index]])
        self.marker.setVisible(True)
        return times[index]
