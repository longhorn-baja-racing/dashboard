"""Draw a raw signal and its filtered version on the same plot.

The raw curve stays visible (thin, dashed) under the filtered one (thick,
solid), so filtering never hides or replaces the recorded data. Both curves
use ``connect="finite"``: missing samples and samples without filter output
leave a break instead of a bridged line.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import numpy.typing as npt
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
    if params.alignment == "trailing" and result.delay_ns is not None:
        text += f", delay {result.delay_ns / 1e6:g} ms"
    return text


def filtered_name(result: FilteredSignal, name: str) -> str:
    """Curve name: signal name, unit and the filter with its parameters."""

    unit = f" [{result.unit}]" if result.unit else ""
    return f"{name}{unit} {filter_label(result)}"


def filtered_points(
    result: FilteredSignal, origin_ns: int = 0
) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
    """x in seconds since ``origin_ns`` and the filtered values (NaN = no output)."""

    return (result.times_ns - np.int64(origin_ns)) / 1e9, np.array(result.values)


def plot_filtered(
    plot_item: PlotItem,
    result: FilteredSignal,
    *,
    name: str,
    origin_ns: int = 0,
    color: str = FILTERED_COLOR,
) -> pg.PlotDataItem:
    """Add only the filtered curve; x is seconds since ``origin_ns``.

    The curve breaks wherever there is no filter output and is named after the
    signal, its unit and the filter parameters.
    """

    xs, ys = filtered_points(result, origin_ns)
    return plot_item.plot(
        xs,
        ys,
        connect="finite",
        pen=pg.mkPen(color, width=2),
        name=filtered_name(result, name),
    )


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
    filtered_curve = plot_filtered(plot_item, result, name=name, color=filtered_color)
    return raw_curve, filtered_curve
