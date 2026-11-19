"""Draw a raw signal and its filtered version on the same plot.

The raw curve stays visible (thin, dashed) under the filtered one (thick,
solid), so filtering never hides or replaces the recorded data. Both curves
use ``connect="finite"``: missing samples and samples without filter output
leave a break instead of a bridged line.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pyqtgraph as pg
from pyqtgraph.graphicsItems.PlotItem import PlotItem
from PySide6.QtCore import Qt

from ..core.filters import FilteredSignal

RAW_COLOR = "#9a9a9a"
FILTERED_COLOR = "#ff9f1a"


def filter_label(result: FilteredSignal) -> str:
    """Short description of the filter and its parameters for a legend entry."""

    params = result.params
    text = f"{result.filter_name.replace('_', ' ')}, {params.window_samples} samples"
    text += f" {params.alignment}"
    if result.window_span_ns is not None:
        text += f" ({result.window_span_ns / 1e6:g} ms)"
    return text


def overlay_filtered(
    plot_item: PlotItem,
    raw_values: Sequence[float | None],
    result: FilteredSignal,
    *,
    name: str,
    raw_color: str = RAW_COLOR,
    filtered_color: str = FILTERED_COLOR,
) -> tuple[pg.PlotDataItem, pg.PlotDataItem]:
    """Add the raw and filtered curves of one signal to ``plot_item``.

    ``raw_values`` must be the values the filter was computed from, so both
    curves share ``result.times_ns``. The x axis is in seconds. Returns the
    raw and filtered curve items.
    """

    if len(raw_values) != result.input_count:
        raise ValueError(
            f"raw_values has {len(raw_values)} samples but the filter input had "
            f"{result.input_count}"
        )
    xs = result.times_ns / 1e9
    raw = np.array(raw_values, dtype=np.float64)
    unit = f" [{result.unit}]" if result.unit else ""
    raw_curve = plot_item.plot(
        xs,
        raw,
        connect="finite",
        pen=pg.mkPen(raw_color, width=1, style=Qt.PenStyle.DashLine),
        name=f"{name}{unit} raw",
    )
    filtered_curve = plot_item.plot(
        xs,
        np.array(result.values),
        connect="finite",
        pen=pg.mkPen(filtered_color, width=2),
        name=f"{name}{unit} {filter_label(result)}",
    )
    return raw_curve, filtered_curve
