"""Labeled state bars and sample markers for one discrete signal on a time axis."""

from __future__ import annotations

import zlib

import pyqtgraph as pg
from pyqtgraph.graphicsItems.PlotItem import PlotItem
from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from ..core.state_timeline import SegmentKind, StateSegment, StateTimeline

EMPTY_TITLE = "No state timeline"
MAX_LABELS = 200
LEFT_AXIS_WIDTH = 120
BAR_Y0 = 0.0
BAR_HEIGHT = 1.0
EVENT_Y = -0.3
STATE_COLORS = (
    "#4c9be8",
    "#5cb85c",
    "#e8c14c",
    "#b07cd8",
    "#4cc9c0",
    "#e87c9b",
    "#9bb84c",
    "#d89a5c",
)
NO_DATA_COLOR = "#8a8a8a"
UNKNOWN_COLOR = "#e8743c"
MIXED_COLOR = "#c8c8c8"
CURSOR_COLOR = "#ffd84c"


def ns_to_seconds(time_ns: int, origin_ns: int) -> float:
    """Seconds from ``origin_ns`` to ``time_ns``."""

    return (time_ns - origin_ns) / 1_000_000_000


def state_color(label: str) -> str:
    """Stable color for a state label (the same label always gets the same color)."""

    return STATE_COLORS[zlib.crc32(label.encode("utf-8")) % len(STATE_COLORS)]


def segment_brush(segment: StateSegment) -> QBrush:
    """Solid fill for known states, hatched fills for everything else.

    No data, unknown/unsupported and mixed segments differ in pattern as well
    as color, and each also carries a text label.
    """

    if segment.kind is SegmentKind.NO_DATA:
        return QBrush(QColor(NO_DATA_COLOR), Qt.BrushStyle.BDiagPattern)
    if segment.kind in (SegmentKind.UNKNOWN, SegmentKind.UNSUPPORTED):
        return QBrush(QColor(UNKNOWN_COLOR), Qt.BrushStyle.DiagCrossPattern)
    if segment.kind is SegmentKind.MIXED:
        return QBrush(QColor(MIXED_COLOR), Qt.BrushStyle.Dense5Pattern)
    return QBrush(QColor(state_color(segment.label)))


def summary_text(signal_id: str, timeline: StateTimeline) -> str:
    """Spell out the counts and the hold policy so nothing depends on color."""

    hold = (
        "until next sample"
        if timeline.max_hold_ns is None
        else f"until next sample, at most {timeline.max_hold_ns / 1_000_000_000:g} s"
    )
    lines = [
        f"{signal_id}: {timeline.sample_count} samples, "
        f"{timeline.transition_count} transitions, {len(timeline.segments)} segments",
        f"State holds {hold}; hatched gray = no data",
    ]
    if timeline.segments_bounded or timeline.events_bounded:
        lines.append("Short segments or samples are combined to fit the view")
    return "\n".join(lines)


class StateTimelineWidget(QWidget):
    """Show one :class:`StateTimeline` as labeled bars over a seconds axis.

    The widget is assigned to any signal by its generic id; it knows nothing
    about specific sensors. Times are drawn in seconds relative to ``origin_ns``.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.plot_item = PlotItem()
        self.plot_widget = pg.PlotWidget(plotItem=self.plot_item)
        self.plot_widget.setBackground("#3a3a3a")
        self.plot_widget.showGrid(x=True, y=False, alpha=0.2)
        self.plot_widget.setMouseEnabled(x=True, y=False)
        self.plot_widget.getAxis("bottom").setPen(pg.mkPen("#cccccc"))
        self.plot_widget.getAxis("left").setPen(pg.mkPen("#cccccc"))
        self.plot_widget.getAxis("left").setWidth(LEFT_AXIS_WIDTH)
        self.plot_widget.setLabel("bottom", "time (s)")
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.time_cursor = pg.InfiniteLine(
            angle=90, movable=False, pen=pg.mkPen(CURSOR_COLOR, width=2)
        )
        self.bars: pg.BarGraphItem | None = None
        self.markers: pg.ScatterPlotItem | None = None
        self.text_items: list[pg.TextItem] = []
        self.signal_id: str | None = None
        self.timeline: StateTimeline | None = None
        self.origin_ns = 0
        self.selected_time_ns: int | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.plot_widget)
        layout.addWidget(self.summary)
        self.setStyleSheet("background-color: #3a3a3a; color: #ffffff;")
        self.clear()

    def clear(self) -> None:
        """Remove the timeline, markers, labels and selected-time cursor."""

        self.selected_time_ns = None
        self._reset()
        self.plot_widget.setTitle(EMPTY_TITLE, color="#aaaaaa")

    def _reset(self) -> None:
        self.plot_widget.clear()
        self.bars = None
        self.markers = None
        self.text_items = []
        self.signal_id = None
        self.timeline = None
        self.summary.setText("")
        self.plot_widget.getAxis("left").setTicks([[]])

    def show_timeline(
        self, signal_id: str, timeline: StateTimeline, *, origin_ns: int | None = None
    ) -> None:
        """Draw ``timeline`` for the signal ``signal_id``.

        ``origin_ns`` is the time shown as 0 s; it defaults to the window start,
        else the first segment or event.
        """

        self._reset()
        self.signal_id = signal_id
        self.timeline = timeline
        self.origin_ns = _default_origin(timeline) if origin_ns is None else origin_ns
        self.summary.setText(summary_text(signal_id, timeline))
        self.plot_widget.getAxis("left").setTicks([[(0.5, signal_id), (EVENT_Y, "samples")]])
        self.plot_item.getViewBox().setYRange(EVENT_Y - 0.4, BAR_HEIGHT + 0.2, padding=0)
        if timeline.is_empty:
            self.plot_widget.setTitle(f"{signal_id}: no samples in view", color="#aaaaaa")
        else:
            self.plot_widget.setTitle(signal_id, color="#aaaaaa")
            self._draw_segments(timeline)
            self._draw_events(timeline)
            self.plot_widget.enableAutoRange(axis="x")
        self._place_cursor()

    def set_selected_time(self, time_ns: int | None) -> None:
        """Show a vertical cursor at ``time_ns``, or hide it with ``None``."""

        self.selected_time_ns = time_ns
        self._place_cursor()

    def _place_cursor(self) -> None:
        if self.selected_time_ns is None:
            if self.time_cursor.scene() is not None:
                self.plot_widget.removeItem(self.time_cursor)
            return
        self.time_cursor.setValue(ns_to_seconds(self.selected_time_ns, self.origin_ns))
        if self.time_cursor.scene() is None:
            self.plot_item.addItem(self.time_cursor, ignoreBounds=True)

    def _draw_segments(self, timeline: StateTimeline) -> None:
        segments = timeline.segments
        if not segments:
            return
        self.bars = pg.BarGraphItem(
            x0=[ns_to_seconds(item.start_ns, self.origin_ns) for item in segments],
            x1=[ns_to_seconds(item.end_ns, self.origin_ns) for item in segments],
            y0=BAR_Y0,
            height=BAR_HEIGHT,
            brushes=[segment_brush(item) for item in segments],
            pen=pg.mkPen("#202020"),
        )
        self.plot_widget.addItem(self.bars)
        widest = sorted(segments, key=lambda item: item.duration_ns, reverse=True)[:MAX_LABELS]
        for segment in sorted(widest, key=lambda item: item.start_ns):
            text = pg.TextItem(segment.label, color="#ffffff", anchor=(0, 0.5))
            text.setPos(ns_to_seconds(segment.start_ns, self.origin_ns), BAR_Y0 + BAR_HEIGHT / 2)
            self.plot_item.addItem(text, ignoreBounds=True)
            self.text_items.append(text)

    def _draw_events(self, timeline: StateTimeline) -> None:
        events = timeline.events
        if not events:
            return
        self.markers = pg.ScatterPlotItem(
            x=[ns_to_seconds(item.time_ns, self.origin_ns) for item in events],
            y=[EVENT_Y] * len(events),
            symbol=["|" if item.count == 1 else "d" for item in events],
            size=[14 if item.transition else 7 for item in events],
            pen=pg.mkPen("#ffffff"),
            brush=pg.mkBrush("#ffffff"),
            data=[item.index for item in events],
        )
        self.plot_widget.addItem(self.markers)


def _default_origin(timeline: StateTimeline) -> int:
    if timeline.start_ns is not None:
        return timeline.start_ns
    starts = []
    if timeline.segments:
        starts.append(timeline.segments[0].start_ns)
    if timeline.events:
        starts.append(timeline.events[0].time_ns)
    return min(starts, default=0)
