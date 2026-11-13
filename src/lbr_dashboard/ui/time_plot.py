"""Windowed rendering of measured time series, independent of source ingestion."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from html import escape
from math import ceil, floor, isfinite, nan

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QObject, QTimer

from ..core.session import SessionRecord

BUCKETS = 500
POINT_LIMIT = 5 * BUCKETS
NANOSECONDS = 1_000_000_000


def _value(record: SessionRecord) -> float:
    sample = record.sample
    if not sample.available or not isinstance(sample.value, (int, float)):
        return nan
    try:
        value = float(sample.value)
    except OverflowError:
        return nan
    return value if isfinite(value) else nan


def render_points(records: Sequence[SessionRecord], origin_ns: int):
    """Keep original endpoints, extrema and gap boundaries within a point budget."""
    values = np.fromiter((_value(record) for record in records), dtype=float, count=len(records))
    finite = np.isfinite(values)
    if len(records) <= POINT_LIMIT:
        indices = np.arange(len(records))
    else:
        keep: list[int] = []
        width = ceil(len(records) / BUCKETS)
        for start in range(0, len(records), width):
            end = min(start + width, len(records))
            valid = np.flatnonzero(finite[start:end]) + start
            invalid = np.flatnonzero(~finite[start:end])
            bucket = {start, end - 1}
            if valid.size:
                bucket.update(
                    (int(valid[np.argmin(values[valid])]), int(valid[np.argmax(values[valid])]))
                )
            if invalid.size:
                bucket.add(start + int(invalid[0]))
            keep.extend(sorted(bucket))
        indices = np.asarray(keep)
    xs = np.fromiter(
        ((records[int(i)].sample.timestamp_ns - origin_ns) / NANOSECONDS for i in indices),
        dtype=float,
        count=len(indices),
    )
    # Even an omitted missing sample must break the line between retained points.
    missing = np.cumsum(~finite)
    connects = np.zeros(len(indices), dtype=bool)
    connects[:-1] = (
        finite[indices[:-1]] & finite[indices[1:]] & (missing[indices[:-1]] == missing[indices[1:]])
    )
    return xs, values[indices], connects


class TimePlot(QObject):
    def __init__(self, widget: pg.PlotWidget) -> None:
        super().__init__(widget)
        self.widget = widget
        plot = widget.getPlotItem()
        assert plot is not None
        self.plot = plot
        self.view = plot.getViewBox()
        self.curve: pg.PlotDataItem | None = None
        self.identity: tuple[object, str] | None = None
        self.origin_ns = 0
        self.bounds = (0, 0)
        self.query: Callable[[int, int], Sequence[SessionRecord]] | None = None
        self.fitted = True
        self._setting_range = False
        self._fit_visible_y = False
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(80)
        self.timer.timeout.connect(self.refresh)
        self.view.sigXRangeChanged.connect(self._view_changed)

    def clear(self) -> None:
        self.timer.stop()
        self._fit_visible_y = False
        self.query = None
        self.identity = None
        if self.curve is not None:
            self.plot.removeItem(self.curve)
            self.curve = None

    def set_trace(
        self,
        identity: tuple[object, str],
        name: str,
        unit: str | None,
        bounds: tuple[int, int],
        query: Callable[[int, int], Sequence[SessionRecord]],
    ) -> None:
        changed = self.identity != identity
        if changed:
            self.clear()
            self.identity = identity
            self.origin_ns = bounds[0]
            self.fitted = True
            self.view.disableAutoRange()
            self.plot.setTitle(escape(name))
            self.plot.setLabel("bottom", "Measurement time since first reading", units="s")
            self.plot.setLabel("left", escape(name), units=escape(unit or ""))
            self.curve = self.plot.plot(pen=pg.mkPen("#00aaff", width=2))
        self.bounds, self.query = bounds, query
        if changed:
            self.refresh()
        else:
            self._schedule()

    def fit(self) -> None:
        self.fitted = True
        self.refresh()

    def _schedule(self) -> None:
        if not self.timer.isActive():
            self.timer.start()

    def _view_changed(self) -> None:
        if self.query is not None and not self._setting_range:
            self.fitted = False
            self._fit_visible_y = True
            self._schedule()

    def refresh(self) -> None:
        self.timer.stop()
        if self.query is None or self.curve is None:
            return
        start, end = self.bounds
        if not self.fitted:
            low, high = self.view.viewRange()[0]
            first = (start - self.origin_ns) / NANOSECONDS
            last = (end - self.origin_ns) / NANOSECONDS
            if low > last or high < first:
                start = end + 1
            else:
                if low > first:
                    start = self.origin_ns + floor(low * NANOSECONDS)
                if high < last:
                    end = self.origin_ns + ceil(high * NANOSECONDS)
        records = self.query(start, end) if start <= end else ()
        xs, ys, connects = render_points(records, self.origin_ns)
        self.curve.setData(
            xs,
            ys,
            connect=connects,
            symbol="o",
            symbolSize=5 if len(xs) <= 100 else 2,
            symbolBrush="#00aaff",
        )
        if self.fitted or self._fit_visible_y:
            visible = np.isfinite(ys)
            if not self.fitted:
                visible &= (xs >= low) & (xs <= high)
            valid = ys[visible]
            self._fit_visible_y = False
            self._setting_range = True
            try:
                if self.fitted:
                    left, right = [(t - self.origin_ns) / NANOSECONDS for t in self.bounds]
                    self.view.setXRange(left, right if right > left else left + 0.001, padding=0.02)
                if valid.size:
                    self.view.setYRange(float(valid.min()), float(valid.max()), padding=0.05)
            finally:
                self._setting_range = False
