"""Browse and plot a firmware session exported as lbr-csv."""

from __future__ import annotations

from collections.abc import Sequence
from html import escape
from typing import Any

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.lbr_session import (
    NODE_CLOCK,
    SHARED_CLOCK,
    Channel,
    LbrSession,
    SessionEvent,
    display_unit,
)
from ..importers.lbr_csv import reset_reason_name

ID_ROLE = Qt.ItemDataRole.UserRole
PALETTE = (
    "#4dabf7",
    "#ff922b",
    "#69db7c",
    "#f06595",
    "#ffd43b",
    "#9775fa",
    "#3bc9db",
    "#ff6b6b",
    "#a9e34b",
    "#e599f7",
)
EVENT_COLORS = {"fault": "#ff6b6b", "event": "#ffd43b", "loss": "#ff922b"}
MAX_MARKERS = 200
SYMBOL_LIMIT = 60


def _seconds(t_us: int) -> str:
    seconds, micros = divmod(t_us, 1_000_000)
    return f"{seconds}.{micros:06d}"


def _format(value: float) -> str:
    if not np.isfinite(value):
        return str(value)
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return f"{value:.6g}"


class ChannelBrowser(QWidget):
    """Channels grouped by node; checking one plots it."""

    checked_changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter signals (name, node or unit)")
        self.filter.setClearButtonEnabled(True)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Signal", "Unit", "Samples"])
        self.tree.setUniformRowHeights(True)
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.clear_button = QPushButton("Clear plot")
        self.summary = QLabel()
        self.summary.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.filter)
        layout.addWidget(self.tree)
        row = QHBoxLayout()
        row.addWidget(self.summary, 1)
        row.addWidget(self.clear_button)
        layout.addLayout(row)

        self._items: dict[str, QTreeWidgetItem] = {}
        self._updating = False
        self.tree.itemChanged.connect(self._item_changed)
        self.filter.textChanged.connect(self.apply_filter)
        self.clear_button.clicked.connect(self.clear_checks)

    def set_session(self, session: LbrSession) -> None:
        self._updating = True
        self.tree.clear()
        self._items.clear()
        bold = QFont()
        bold.setBold(True)
        groups: dict[tuple[str, str], QTreeWidgetItem] = {}
        for node in session.nodes():
            item = QTreeWidgetItem(self.tree, [node or "(no node)", "", ""])
            item.setFont(0, bold)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            groups[(node, "sample")] = item
            item.setExpanded(True)
        for channel in session.channels:
            key = channel.key
            parent = groups.get((key.node, key.kind))
            if parent is None:
                parent = QTreeWidgetItem(groups[(key.node, "sample")], [key.kind, "", ""])
                parent.setFlags(Qt.ItemFlag.ItemIsEnabled)
                groups[(key.node, key.kind)] = parent
            unit, _ = display_unit(channel.unit)
            item = QTreeWidgetItem(parent, [key.label, unit, str(channel.count)])
            item.setData(0, ID_ROLE, channel.id)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, Qt.CheckState.Unchecked)
            item.setToolTip(0, self._tooltip(channel))
            if channel.available_count == 0:
                grey = QBrush(QColor("#808080"))
                for column in range(3):
                    item.setForeground(column, grey)
            self._items[channel.id] = item
        shared = sum(1 for c in session.channels if c.key.clock == SHARED_CLOCK)
        self.summary.setText(
            f"{len(session.channels)} channels from {len(session.nodes())} nodes"
            + (
                ""
                if shared == len(session.channels)
                else f" ({len(session.channels) - shared} on node clocks)"
            )
        )
        self._updating = False
        self.apply_filter(self.filter.text())

    @staticmethod
    def _tooltip(channel: Channel) -> str:
        lines = [channel.id, f"{channel.count} samples, {channel.available_count} available"]
        if channel.unit:
            lines.append(f"unit: {channel.unit}")
        reasons = channel.unavailable_reasons()
        if reasons:
            lines.append("unavailable: " + ", ".join(f"{k} x{v}" for k, v in reasons.items()))
        if channel.key.clock == NODE_CLOCK:
            lines.append("on the node's own clock (taken before time sync)")
        return "\n".join(lines)

    def apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        for channel_id, item in self._items.items():
            haystack = f"{channel_id} {item.text(0)} {item.text(1)}".lower()
            item.setHidden(bool(needle) and needle not in haystack)
        root = self.tree.invisibleRootItem()
        for index in range(root.childCount()):
            child = root.child(index)
            if child is not None:
                self._hide_empty(child, expand=bool(needle))

    def _hide_empty(self, item: QTreeWidgetItem, *, expand: bool) -> bool:
        if item.data(0, ID_ROLE) is not None:
            return not item.isHidden()
        visible = False
        for index in range(item.childCount()):
            child = item.child(index)
            if child is not None:
                visible = self._hide_empty(child, expand=expand) or visible
        item.setHidden(not visible)
        if expand and visible:
            item.setExpanded(True)
        return visible

    def checked_ids(self) -> list[str]:
        return [
            channel_id
            for channel_id, item in self._items.items()
            if item.checkState(0) == Qt.CheckState.Checked
        ]

    def set_checked(self, channel_id: str, checked: bool, *, notify: bool = True) -> None:
        item = self._items.get(channel_id)
        if item is None:
            return
        previous, self._updating = self._updating, self._updating or not notify
        try:
            item.setCheckState(0, Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        finally:
            self._updating = previous

    def clear_checks(self) -> None:
        self._updating = True
        for item in self._items.values():
            item.setCheckState(0, Qt.CheckState.Unchecked)
        self._updating = False
        self.checked_changed.emit()

    def _item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if not self._updating and column == 0 and item.data(0, ID_ROLE) is not None:
            self.checked_changed.emit()


class TimelinePlot(QWidget):
    """Time plot of selected channels with event, fault and loss markers."""

    def __init__(self) -> None:
        super().__init__()
        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setBackground("#2b2b2b")
        self.plot_widget.showGrid(x=True, y=True, alpha=0.2)
        item = self.plot_widget.getPlotItem()
        assert item is not None
        self.plot_item = item
        self.plot_item.setDownsampling(auto=True, mode="peak")
        self.plot_item.setClipToView(True)
        self.legend = self.plot_item.addLegend(
            offset=(-10, 10), brush=pg.mkBrush(20, 20, 20, 190), labelTextColor="#dddddd"
        )
        self.notice = QLabel()
        self.notice.setWordWrap(True)
        self.notice.setStyleSheet("background: #2b2b2b; color: #ffd43b; padding: 4px;")
        self.notice.hide()
        self.readout = QLabel()
        self.readout.setTextFormat(Qt.TextFormat.RichText)
        self.readout.setStyleSheet(
            "background: #2b2b2b; color: #dddddd; font-family: monospace; padding: 4px;"
        )
        self.cursor_line = pg.InfiniteLine(
            angle=90, movable=False, pen=pg.mkPen("#bbbbbb", width=1)
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.notice)
        layout.addWidget(self.plot_widget, 1)
        layout.addWidget(self.readout)

        self.session: LbrSession | None = None
        self.channels: list[Channel] = []
        self.curves: dict[str, pg.PlotDataItem] = {}
        self.markers: list[pg.InfiniteLine] = []
        self.clock: tuple[str, str] | None = None
        scene: Any = self.plot_widget.scene()
        scene.sigMouseMoved.connect(self._mouse_moved)
        self.show_channels([])

    def set_session(self, session: LbrSession | None) -> None:
        self.session = session
        self.set_notice("")
        self.show_channels([])

    def set_notice(self, text: str) -> None:
        self.notice.setText(text)
        self.notice.setVisible(bool(text))

    def show_channels(self, channels: Sequence[Channel]) -> None:
        """Plot ``channels``; all must share one clock (the caller checks)."""

        self.plot_item.clear()
        self.legend.clear()
        self.curves.clear()
        self.markers.clear()
        self.channels = list(channels)
        self.readout.clear()
        self.clock = clock_of(self.channels)
        if not self.channels:
            self.plot_item.setTitle("Check signals on the left to plot them", color="#aaaaaa")
            self.plot_item.setLabel("bottom", "Time since Central boot (s)")
            self.plot_item.setLabel("left", "")
            self._add_markers()
            self.plot_item.addItem(self.cursor_line, ignoreBounds=True)
            return
        self.plot_item.setTitle("")
        for index, channel in enumerate(self.channels):
            self._add_curve(channel, PALETTE[index % len(PALETTE)])
        units = {display_unit(c.unit)[0] for c in self.channels}
        if len(units) == 1:
            unit = units.pop()
            self.plot_item.setLabel("left", f"Value ({unit})" if unit else "Value")
        else:
            self.plot_item.setLabel("left", "Value (mixed units)")
        if self.clock is not None and self.clock[0] == NODE_CLOCK:
            self.plot_item.setLabel(
                "bottom", f"{self.clock[1]} node clock (s), not aligned with Central time"
            )
        else:
            self.plot_item.setLabel("bottom", "Time since Central boot (s)")
            self._add_markers()
        self.plot_item.addItem(self.cursor_line, ignoreBounds=True)
        self._view().enableAutoRange()

    def _add_curve(self, channel: Channel, color: str) -> None:
        unit, scale = display_unit(channel.unit)
        xs = channel.times_us / 1e6
        ys = np.where(channel.available, channel.values * scale, np.nan)
        name = f"{channel.key.node}.{channel.key.label}" + (f" [{unit}]" if unit else "")
        if channel.available_count == 0:
            reasons = ", ".join(channel.unavailable_reasons()) or "unavailable"
            name += f" (no data: {reasons})"
        options: dict[str, object] = {"pen": pg.mkPen(color, width=2), "name": name}
        if channel.is_discrete:
            options["stepMode"] = "right"
        if channel.count <= SYMBOL_LIMIT:
            options.update(symbol="o", symbolSize=6, symbolBrush=color, symbolPen=None)
        curve = self.plot_item.plot(xs, ys, connect="finite", **options)
        self.curves[channel.id] = curve

    def _add_markers(self) -> None:
        if self.session is None:
            return
        timed = [e for e in self.session.events if e.synced and e.kind in EVENT_COLORS]
        for event in timed[:MAX_MARKERS]:
            color = EVENT_COLORS[event.kind]
            style = Qt.PenStyle.DashLine if event.kind == "loss" else Qt.PenStyle.SolidLine
            line = pg.InfiniteLine(
                pos=event.t_us / 1e6,
                angle=90,
                movable=False,
                pen=pg.mkPen(color, width=1, style=style),
                label="LOSS" if event.kind == "loss" else event.name,
                labelOpts={
                    "position": 0.04,
                    "color": color,
                    "rotateAxis": (1, 0),
                    "anchors": [(0, 1), (0, 1)],
                },
            )
            line.setToolTip(f"{_seconds(event.t_us)} s  {event.node}: {event.label}")
            self.plot_item.addItem(line, ignoreBounds=True)
            self.markers.append(line)

    def _view(self) -> Any:
        view = self.plot_item.getViewBox()
        assert view is not None
        return view

    def _mouse_moved(self, position: QPointF) -> None:
        if not self.channels or not self.plot_item.sceneBoundingRect().contains(position):
            return
        x = self._view().mapSceneToView(position).x()
        self.show_readout(int(round(x * 1e6)))

    def show_readout(self, t_us: int) -> None:
        """Show each plotted channel's latest value at ``t_us``."""

        self.cursor_line.setPos(t_us / 1e6)
        parts = [f"<b>t = {_seconds(max(t_us, 0))} s</b>"]
        for index, channel in enumerate(self.channels):
            color = PALETTE[index % len(PALETTE)]
            position = channel.index_at(t_us)
            unit, scale = display_unit(channel.unit)
            if position < 0:
                text = "-"
            elif not channel.available[position]:
                text = channel.statuses[int(channel.status[position])] or "unavailable"
            else:
                text = _format(float(channel.values[position]) * scale) + (
                    f" {unit}" if unit else ""
                )
            parts.append(
                f'<span style="color:{color}">{escape(channel.key.label)}</span> {escape(text)}'
            )
        self.readout.setText("&nbsp;&nbsp; ".join(parts))

    def focus(self, t_us: int, half_width_s: float = 2.0) -> None:
        center = t_us / 1e6
        self._view().setXRange(center - half_width_s, center + half_width_s, padding=0)
        self.show_readout(t_us)


def clock_of(channels: Sequence[Channel]) -> tuple[str, str] | None:
    """The single clock shared by ``channels``, or None when they disagree."""

    clocks = {
        (SHARED_CLOCK, "") if c.key.clock == SHARED_CLOCK else (NODE_CLOCK, c.key.node)
        for c in channels
    }
    return clocks.pop() if len(clocks) == 1 else None


def _table(headers: Sequence[str]) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(list(headers))
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.verticalHeader().setVisible(False)
    table.horizontalHeader().setStretchLastSection(True)
    return table


def _fill(table: QTableWidget, rows: Sequence[Sequence[str]]) -> None:
    table.setRowCount(len(rows))
    for row_index, row in enumerate(rows):
        for column, value in enumerate(row):
            table.setItem(row_index, column, QTableWidgetItem(value))
    table.resizeColumnsToContents()


class LbrSessionView(QWidget):
    """Channel browser, timeline plot and session, event and value panels."""

    def __init__(self) -> None:
        super().__init__()
        self.browser = ChannelBrowser()
        self.plot = TimelinePlot()
        self.warnings = QLabel()
        self.warnings.setWordWrap(True)
        self.warnings.setTextFormat(Qt.TextFormat.RichText)
        self.session_table = _table(["Field", "Value"])
        self.events_table = _table(["Time (s)", "Kind", "Node", "Name", "Value"])
        self.selected_table = _table(
            [
                "Signal",
                "Unit",
                "Clock",
                "Samples",
                "Unavailable",
                "Min",
                "Max",
                "Last",
                "First (s)",
                "Last (s)",
            ]
        )
        session_page = QWidget()
        session_layout = QVBoxLayout(session_page)
        session_layout.addWidget(self.warnings)
        session_layout.addWidget(self.session_table, 1)
        self.tabs = QTabWidget()
        self.tabs.addTab(self.selected_table, "Selected signals")
        self.tabs.addTab(self.events_table, "Events")
        self.tabs.addTab(session_page, "Session")

        right = QSplitter(Qt.Orientation.Vertical)
        right.addWidget(self.plot)
        right.addWidget(self.tabs)
        right.setSizes([480, 220])
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.browser)
        splitter.addWidget(right)
        splitter.setSizes([320, 880])
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)

        self.session: LbrSession | None = None
        self._events: list[SessionEvent] = []
        self.browser.checked_changed.connect(self._selection_changed)
        self.events_table.cellDoubleClicked.connect(self._focus_event)

    def set_session(self, session: LbrSession) -> None:
        self.session = session
        self.plot.set_session(session)
        self.browser.set_session(session)
        self._fill_session(session)
        self._events = list(session.events)
        _fill(
            self.events_table,
            [
                (
                    _seconds(e.t_us) + ("" if e.synced else " (node clock)"),
                    e.kind,
                    e.node,
                    e.name,
                    e.value,
                )
                for e in self._events
            ],
        )
        self.tabs.setTabText(1, f"Events ({len(self._events)})")
        self.tabs.setTabText(
            2, f"Session ({len(session.warnings)} warnings)" if session.warnings else "Session"
        )
        self._selection_changed()
        if session.warnings:
            self.tabs.setCurrentIndex(2)

    def _fill_session(self, session: LbrSession) -> None:
        if session.warnings:
            items = "".join(f"<li>{escape(w)}</li>" for w in session.warnings)
            self.warnings.setText(
                f'<span style="color:#9c5700"><b>Warnings</b><ul>{items}</ul></span>'
            )
        else:
            self.warnings.setText(
                '<span style="color:#2b8a3e">No integrity problems reported.</span>'
            )
        rows: list[tuple[str, str]] = [
            ("file", session.source_name),
            ("lbr_csv_version", str(session.csv_version)),
            ("rows", str(session.row_count)),
            ("logger", session.logger or "unknown"),
        ]
        for name, value in session.session.items():
            if name == "reset_reason":
                value = f"{value} ({reset_reason_name(value)})"
            rows.append((f"session.{name}", value))
        rows += [(f"config.{name}" if name else "config", value) for name, value in session.config]
        rows += [(f"integrity.{name}", str(value)) for name, value in session.integrity.items()]
        _fill(self.session_table, rows)

    def _selection_changed(self) -> None:
        if self.session is None:
            return
        ids = self.browser.checked_ids()
        channels = [c for c in (self.session.channel(i) for i in ids) if c is not None]
        if channels and clock_of(channels) is None:
            newest = next(
                (c for c in reversed(channels) if c.id not in self.plot.curves), channels[-1]
            )
            self.browser.set_checked(newest.id, False, notify=False)
            channels.remove(newest)
            where = (
                "the Central" if newest.key.clock == SHARED_CLOCK else f"the {newest.key.node} node"
            )
            self.plot.set_notice(
                f"{newest.key.label} is on {where}'s clock and cannot share a time axis with the "
                "signals already plotted. Clear the plot to view it."
            )
        else:
            self.plot.set_notice("")
        self.plot.show_channels(channels)
        self._fill_selected(channels)

    def _fill_selected(self, channels: Sequence[Channel]) -> None:
        rows = []
        for channel in channels:
            unit, scale = display_unit(channel.unit)
            values = channel.valid_values() * scale
            reasons = channel.unavailable_reasons()
            missing = sum(reasons.values())
            available = np.flatnonzero(channel.available)
            last = _format(float(channel.values[available[-1]]) * scale) if available.size else "-"
            rows.append(
                (
                    f"{channel.key.node}.{channel.key.label}",
                    unit,
                    "node" if channel.key.clock == NODE_CLOCK else "Central",
                    str(channel.count),
                    f"{missing} ({', '.join(f'{k} x{v}' for k, v in reasons.items())})"
                    if missing
                    else "0",
                    _format(float(values.min())) if values.size else "-",
                    _format(float(values.max())) if values.size else "-",
                    last,
                    _seconds(int(channel.times_us[0])) if channel.count else "-",
                    _seconds(int(channel.times_us[-1])) if channel.count else "-",
                )
            )
        _fill(self.selected_table, rows)

    def _focus_event(self, row: int, _column: int) -> None:
        event = self._events[row]
        if event.synced:
            self.plot.focus(event.t_us)
