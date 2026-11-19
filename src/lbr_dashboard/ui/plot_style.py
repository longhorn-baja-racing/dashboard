"""Colors shared by telemetry plots and replay cursors."""

import pyqtgraph as pg

TRACE_COLOR = "#007ba3"
CURSOR_COLOR = "#a86c00"


def apply_plot_colors(widget: pg.PlotWidget, background: str, text: str) -> None:
    widget.setBackground(background)
    plot = widget.getPlotItem()
    if plot is not None:
        if plot.titleLabel.isVisible():
            plot.setTitle(plot.titleLabel.text, color=text)
        for name in ("bottom", "left"):
            axis = plot.getAxis(name)
            axis.setPen(pg.mkPen(text))
            axis.setTextPen(pg.mkPen(text))
            axis.setLabel(axis.labelText, units=axis.labelUnits, color=text)
