"""Show and edit the shared interval selection as a draggable plot region."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import pyqtgraph as pg
from pyqtgraph.graphicsItems.PlotItem.PlotItem import PlotItem
from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QKeyEvent

from ..core.interval import IntervalSelection, TimeInterval

NS_PER_SECOND = 1_000_000_000
NS_PER_MILLISECOND = 1_000_000


@dataclass(frozen=True, slots=True)
class PlotTimeScale:
    """Convert between integer session nanoseconds and float plot x values.

    ``x = (time_ns - origin_ns) / ns_per_unit``. Plot x values are float64, so a
    round trip ``to_ns(to_x(t))`` is exact only while ``|t - origin_ns|`` is below
    about 2**50 ns (about 13 days). Set ``origin_ns`` to the session start; raw
    epoch times (about 1.7e18 ns) with ``origin_ns=0`` lose up to a few hundred ns.
    """

    ns_per_unit: int
    origin_ns: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.ns_per_unit, bool) or not isinstance(self.ns_per_unit, int):
            raise TypeError("ns_per_unit must be an integer")
        if self.ns_per_unit <= 0:
            raise ValueError("ns_per_unit must be positive")
        if isinstance(self.origin_ns, bool) or not isinstance(self.origin_ns, int):
            raise TypeError("origin_ns must be integer nanoseconds")

    @classmethod
    def seconds(cls, origin_ns: int = 0) -> PlotTimeScale:
        return cls(NS_PER_SECOND, origin_ns)

    @classmethod
    def milliseconds(cls, origin_ns: int = 0) -> PlotTimeScale:
        return cls(NS_PER_MILLISECOND, origin_ns)

    def to_x(self, time_ns: int) -> float:
        return (time_ns - self.origin_ns) / self.ns_per_unit

    def to_ns(self, x: float) -> int:
        return self.origin_ns + round(float(x) * self.ns_per_unit)


class IntervalRegion(QObject):
    """Bind one ``IntervalSelection`` to a ``pg.LinearRegionItem`` on a plot.

    The region is hidden while nothing is selected. Finishing a drag commits the
    region to the selection (clamped to its bounds); selection changes move the
    region. Escape clears the selection while the plot widget has focus.
    """

    def __init__(
        self,
        plot_widget: pg.PlotWidget,
        selection: IntervalSelection,
        scale: PlotTimeScale,
    ) -> None:
        super().__init__(plot_widget)
        self._plot_widget = plot_widget
        self._plot_item = cast(PlotItem, plot_widget.getPlotItem())
        self._selection = selection
        self._scale = scale
        self._syncing = False
        self.region = pg.LinearRegionItem(movable=True)
        self.region.setZValue(10)
        self._plot_item.addItem(self.region, ignoreBounds=True)
        self.region.sigRegionChangeFinished.connect(self._commit_region)
        plot_widget.installEventFilter(self)
        self._unsubscribe = selection.subscribe(self._show)
        # The selection can outlive the plot; stop notifying a deleted region.
        plot_widget.destroyed.connect(self._unsubscribe)
        self._show(selection.interval)

    def select_visible_range(self) -> TimeInterval:
        """Select the plot's visible x range (clamped to the session bounds)."""

        (x_min, x_max), _ = self._plot_item.getViewBox().viewRange()
        return self._selection.select(self._scale.to_ns(x_min), self._scale.to_ns(x_max))

    def detach(self) -> None:
        """Stop following the selection and remove the region from the plot."""

        self._unsubscribe()
        self._plot_widget.removeEventFilter(self)
        self._plot_item.removeItem(self.region)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            isinstance(event, QKeyEvent)
            and event.type() == QEvent.Type.KeyPress
            and event.key() == Qt.Key.Key_Escape
            and self._selection.interval is not None
        ):
            self._selection.clear()
            return True
        return super().eventFilter(watched, event)

    def _show(self, interval: TimeInterval | None) -> None:
        self._syncing = True
        try:
            if interval is None:
                self.region.hide()
            else:
                self.region.setRegion(
                    (self._scale.to_x(interval.start_ns), self._scale.to_x(interval.end_ns))
                )
                self.region.show()
        finally:
            self._syncing = False

    def _commit_region(self) -> None:
        if self._syncing:
            return  # Our own setRegion call, not a user edit.
        low, high = cast("tuple[float, float]", self.region.getRegion())
        requested = TimeInterval.between(self._scale.to_ns(low), self._scale.to_ns(high))
        bounds = self._selection.bounds
        try:
            if bounds is None or requested.overlaps(bounds):
                self._selection.select(requested.start_ns, requested.end_ns)
        finally:
            # Snap the region to the stored value, e.g. after clamping or a rejected drag.
            self._show(self._selection.interval)
