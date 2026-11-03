"""Bar view of a sample-count histogram with a plain-text count summary."""

from __future__ import annotations

import pyqtgraph as pg
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from ..core.histogram import Histogram

EMPTY_TITLE = "No histogram"


def format_ns(value: int) -> str:
    """Format integer nanoseconds as exact seconds, e.g. ``1500000000`` -> ``1.5 s``."""

    sign = "-" if value < 0 else ""
    seconds, remainder = divmod(abs(value), 1_000_000_000)
    fraction = f"{remainder:09d}".rstrip("0")
    return f"{sign}{seconds}{'.' + fraction if fraction else ''} s"


def interval_text(hist: Histogram) -> str:
    """Describe the closed time interval a histogram was computed over."""

    if hist.start_ns is None and hist.end_ns is None:
        return "whole log"
    start = "log start" if hist.start_ns is None else format_ns(hist.start_ns)
    end = "log end" if hist.end_ns is None else format_ns(hist.end_ns)
    return f"[{start}, {end}]"


def summary_text(hist: Histogram) -> str:
    """Spell out every count so nothing depends on color alone."""

    lines = [
        f"Valid samples: {hist.valid} in {len(hist.counts)} bins",
        f"Missing: {hist.missing}  Non-finite: {hist.non_finite}  "
        f"Below range: {hist.below_range}  Above range: {hist.above_range}",
        f"Interval: {interval_text(hist)}  Sample-count weighting",
    ]
    return "\n".join(lines)


class HistogramWidget(QWidget):
    """Show one :class:`Histogram` as bars spanning their bin edges."""

    def __init__(self) -> None:
        super().__init__()
        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setBackground("#3a3a3a")
        self.plot_widget.showGrid(x=True, y=True, alpha=0.2)
        self.plot_widget.getAxis("bottom").setPen(pg.mkPen("#cccccc"))
        self.plot_widget.getAxis("left").setPen(pg.mkPen("#cccccc"))
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.bars: pg.BarGraphItem | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.plot_widget)
        layout.addWidget(self.summary)
        self.setStyleSheet("background-color: #3a3a3a; color: #ffffff;")
        self.clear()

    def clear(self) -> None:
        """Remove any bars and summary."""

        self.plot_widget.clear()
        self.bars = None
        self.plot_widget.setTitle(EMPTY_TITLE, color="#aaaaaa")
        self.plot_widget.setLabel("bottom", "")
        self.plot_widget.setLabel("left", "samples")
        self.summary.setText("")

    def show_histogram(self, hist: Histogram, name: str) -> None:
        """Draw ``hist`` for the signal ``name``; empty results draw no bars."""

        self.clear()
        axis_label = f"{name} ({hist.unit})" if hist.unit else name
        self.plot_widget.setLabel("bottom", axis_label)
        self.summary.setText(summary_text(hist))
        if hist.is_empty:
            self.plot_widget.setTitle(f"{name}: no valid samples in interval", color="#aaaaaa")
            return
        self.plot_widget.setTitle(f"{name} histogram", color="#aaaaaa")
        self.bars = pg.BarGraphItem(
            x0=list(hist.edges[:-1]),
            x1=list(hist.edges[1:]),
            height=list(hist.counts),
            brush="#4c9be8",
            pen=pg.mkPen("#cccccc"),
        )
        self.plot_widget.addItem(self.bars)
        self.plot_widget.enableAutoRange()
