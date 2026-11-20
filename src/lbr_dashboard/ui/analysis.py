"""Shared analysis state and the docks that analysis tools open in.

Every tool reads the same session snapshot, the same selected signal, the same
replay cursor and the one session-level interval selection. Tools never own
their own copy of that state, so the plot region, statistics and export always
agree on what is selected.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from numbers import Real
from pathlib import Path

import pyqtgraph as pg
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QDockWidget, QMainWindow, QWidget

from ..core import SessionSnapshot, SignalDescriptor
from ..core.derived_session import DerivedSignalSet
from ..core.frames import FrameTransformSet
from ..core.interval import IntervalSelection, TimeInterval
from ..core.source_monitor import SourceMonitorReport
from .interval_region import IntervalRegion, PlotTimeScale
from .plot_style import apply_plot_colors


def is_numeric(descriptor: SignalDescriptor | None) -> bool:
    """Return whether a signal holds scalar numbers that analysis tools can compute on."""

    return descriptor is not None and descriptor.data_type == "numeric" and not descriptor.shape


class AnalysisContext(QObject):
    """The session, signal, cursor and interval that every analysis tool shares."""

    # Emitted when the applied coordinate-frame transforms change.
    frames_changed = Signal()
    snapshot_changed = Signal()
    signal_changed = Signal()
    cursor_changed = Signal()
    interval_changed = Signal()
    theme_changed = Signal()
    derived_changed = Signal()
    source_health_changed = Signal()
    plot_changed = Signal()
    seek_requested = Signal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.session: object | None = None
        self.snapshot: SessionSnapshot | None = None
        self.selected_id: str | None = None
        self.cursor_ns: int | None = None
        self.following_live = False
        self.log_path: Path | None = None
        self.plot: pg.PlotWidget | None = None
        self.plot_origin_ns: int | None = None
        self.plot_signal_id: str | None = None
        self.selection = IntervalSelection()
        self.plot_colors = ("#202020", "#eeeeee")
        # Applied frame transforms outlive any one session; the source controller
        # adds their results to every snapshot it shows.
        self.frames = FrameTransformSet()
        self.frames.subscribe(self.frames_changed.emit)
        self.source_health: SourceMonitorReport | None = None
        self._descriptors: dict[str, SignalDescriptor] = {}
        self.selection.subscribe(lambda _interval: self.interval_changed.emit())
        # Derived signal definitions outlive any one session; the source controller
        # evaluates them into every snapshot it shows.
        self.derived = DerivedSignalSet()
        self.derived.subscribe(self.derived_changed.emit)

    @property
    def interval(self) -> TimeInterval | None:
        return self.selection.interval

    @property
    def bounds(self) -> TimeInterval | None:
        """The time range covered by every recorded signal, or None without readings."""

        return self.selection.bounds

    @property
    def descriptors(self) -> tuple[SignalDescriptor, ...]:
        return self.snapshot.schema.signals if self.snapshot is not None else ()

    def descriptor(self, signal_id: str | None) -> SignalDescriptor | None:
        return self._descriptors.get(signal_id) if signal_id else None

    def set_snapshot(
        self,
        session: object | None,
        snapshot: SessionSnapshot | None,
        *,
        log_path: Path | None = None,
    ) -> None:
        """Show a new revision; a different session also clears the interval.

        ``log_path`` is the file the session was read from, or None for sources
        without a file such as simulators. It is taken when the session changes.
        """

        if session is not self.session:
            self.session = session
            self.log_path = log_path if session is not None else None
            self.selection.clear()
        if snapshot is self.snapshot:
            return
        self.snapshot = snapshot
        self._descriptors = (
            {item.identifier: item for item in snapshot.schema.signals} if snapshot else {}
        )
        self.selection.set_bounds(session_bounds(snapshot))
        if self.selection.bounds is None:
            self.selection.clear()
        self.snapshot_changed.emit()

    def set_selected(self, signal_id: str | None) -> None:
        if signal_id != self.selected_id:
            self.selected_id = signal_id
            self.signal_changed.emit()

    def set_cursor(self, time_ns: int | None, *, live: bool = False) -> None:
        """Move the replay cursor; ``live`` is true while the clock follows live data."""

        if time_ns != self.cursor_ns or live != self.following_live:
            self.cursor_ns = time_ns
            self.following_live = live
            self.cursor_changed.emit()

    def set_source_health(self, report: SourceMonitorReport | None) -> None:
        """Publish the latest health of the active (or last) source."""

        if report != self.source_health:
            self.source_health = report
            self.source_health_changed.emit()

    def request_seek(self, time_ns: int) -> None:
        """Ask the replay clock to move its cursor to ``time_ns`` (session time)."""

        self.seek_requested.emit(time_ns)

    def set_plot(
        self,
        plot: pg.PlotWidget | None,
        origin_ns: int | None,
        *,
        signal_id: str | None = None,
    ) -> None:
        """Record the main time plot, whose x axis is seconds since ``origin_ns``.

        ``signal_id`` is the signal the plot shows. ``plot_changed`` is emitted
        every time the plot is (re)attached, including after it was cleared, so
        tools can draw their items on it again.
        """

        self.plot, self.plot_origin_ns = plot, origin_ns
        self.plot_signal_id = signal_id if plot is not None else None
        self.plot_changed.emit()

    def set_plot_colors(self, background: str, text: str) -> None:
        """Record the theme's plot colors and restyle every tool plot."""

        if (background, text) != self.plot_colors:
            self.plot_colors = (background, text)
            self.theme_changed.emit()

    def style_plot(self, widget: pg.PlotWidget) -> None:
        """Color ``widget`` for the current theme now and after every theme change."""

        styler = _PlotStyler(widget, self)
        styler.apply()
        # The styler is a child of the plot, so Qt drops this connection with it.
        self.theme_changed.connect(styler.apply)

    def clear(self) -> None:
        """Forget the session, e.g. when a legacy table replaces it."""

        self.set_snapshot(None, None)
        self.set_cursor(None)
        self.set_source_health(None)

    def numeric_series(
        self, signal_id: str, interval: TimeInterval | None = None
    ) -> tuple[list[int], list[float | None]]:
        """Return measurement times and values; unavailable readings stay None.

        With an interval only the records inside it are returned (closed range),
        found by binary search so the cost follows the selection size.
        """

        snapshot = self.snapshot
        if snapshot is None or signal_id not in snapshot.series:
            return [], []
        records = (
            snapshot.series[signal_id]
            if interval is None
            else snapshot.range(signal_id, interval.start_ns, interval.end_ns)
        )
        times = [record.sample.timestamp_ns for record in records]
        values = [
            _as_float(record.sample.value) if record.sample.available else None
            for record in records
        ]
        return times, values

    def reading_at(self, signal_id: str, time_ns: int) -> tuple[list[int], list[float | None]]:
        """Return the last reading at or before ``time_ns`` (missing stays missing)."""

        snapshot = self.snapshot
        if snapshot is None or signal_id not in snapshot.series:
            return [], []
        record = snapshot.at_or_before(signal_id, time_ns)
        if record is None:
            return [], []
        sample = record.sample
        return [sample.timestamp_ns], [_as_float(sample.value) if sample.available else None]


class _PlotStyler(QObject):
    def __init__(self, widget: pg.PlotWidget, context: AnalysisContext) -> None:
        super().__init__(widget)
        self.widget = widget
        self.context = context

    def apply(self) -> None:
        apply_plot_colors(self.widget, *self.context.plot_colors)


def _as_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    return float(value)


def session_bounds(snapshot: SessionSnapshot | None) -> TimeInterval | None:
    """Return the earliest and latest measurement time over all signals."""

    if snapshot is None:
        return None
    starts: list[int] = []
    ends: list[int] = []
    for records in snapshot.series.values():
        if records:
            starts.append(records[0].sample.timestamp_ns)
            ends.append(records[-1].sample.timestamp_ns)
    return TimeInterval(min(starts), max(ends)) if starts else None


@dataclass(frozen=True)
class AnalysisTool:
    """A dockable view over the shared analysis context."""

    key: str
    label: str
    create: Callable[[AnalysisContext], QWidget]


class AnalysisDocks:
    """Add the Analysis menu, open tools as docks and draw the interval on the main plot."""

    def __init__(
        self,
        window: QMainWindow,
        context: AnalysisContext,
        tools: Sequence[AnalysisTool],
    ) -> None:
        self.window = window
        self.context = context
        self.tools = {tool.key: tool for tool in tools}
        self.docks: dict[str, QDockWidget] = {}
        self.region: IntervalRegion | None = None
        self._plot: pg.PlotWidget | None = None
        self._origin_ns: int | None = None
        menu_bar = window.menuBar()
        assert menu_bar is not None
        self.menu = menu_bar.addMenu("&Analysis")
        self.select_action = QAction("Select visible time range", window)
        self.select_action.setShortcut(QKeySequence("Ctrl+I"))
        self.select_action.triggered.connect(self.select_visible_range)
        self.clear_action = QAction("Clear time interval", window)
        self.clear_action.triggered.connect(context.selection.clear)
        self.menu.addAction(self.select_action)
        self.menu.addAction(self.clear_action)
        self.menu.addSeparator()
        for tool in tools:
            self.menu.addAction(tool.label, lambda key=tool.key: self.open(key))
        context.snapshot_changed.connect(self._update_actions)
        context.interval_changed.connect(self._update_actions)
        self._update_actions()

    def open(self, key: str) -> QDockWidget:
        """Show a tool's dock, creating it the first time."""

        dock = self.docks.get(key)
        if dock is None:
            tool = self.tools[key]
            dock = QDockWidget(tool.label, self.window)
            dock.setObjectName(f"analysis.{key}")
            dock.setWidget(tool.create(self.context))
            self.window.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)
            self.docks[key] = dock
        dock.show()
        dock.raise_()
        return dock

    def follow_plot(self, plot: pg.PlotWidget, origin_ns: int) -> None:
        """Keep the interval region on ``plot``, whose x axis is seconds since ``origin_ns``."""

        current = self.region
        attached = current is not None and current.region.scene() is not None
        if plot is self._plot and origin_ns == self._origin_ns and attached:
            return
        if current is not None:
            current.detach()
        self._plot, self._origin_ns = plot, origin_ns
        self.region = IntervalRegion(plot, self.context.selection, PlotTimeScale.seconds(origin_ns))
        self.context.set_plot(plot, origin_ns, signal_id=self.context.selected_id)

    def leave_plot(self) -> None:
        """Remove the region, e.g. while the plot shows something other than a session."""

        if self.region is not None:
            self.region.detach()
        self.region = None
        self._plot = self._origin_ns = None
        self.context.set_plot(None, None)

    def select_visible_range(self) -> TimeInterval | None:
        region = self.region
        if region is None or region.region.scene() is None or self.context.bounds is None:
            return None
        try:
            return region.select_visible_range()
        except ValueError:
            return None  # The view shows no part of the session.

    def _update_actions(self) -> None:
        self.select_action.setEnabled(self.context.bounds is not None)
        self.clear_action.setEnabled(self.context.interval is not None)
