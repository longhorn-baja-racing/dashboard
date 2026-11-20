"""Data-quality tool: per-signal and per-source quality of the loaded session.

The tables summarize the shared interval, or the whole session without one.
The timeline always spans the whole session so every gap can be found and
clicked; clicking a gap or a run of unavailable readings selects it as the
shared interval.
"""

from __future__ import annotations

from typing import cast

import pyqtgraph as pg
from pyqtgraph.graphicsItems.PlotItem.PlotItem import PlotItem
from pyqtgraph.GraphicsScene import GraphicsScene
from PySide6.QtCore import QPointF, Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ...core.data_quality import format_quality
from ...core.interval import TimeInterval
from ...core.session_quality import (
    SIGNAL_COLUMNS,
    Finding,
    SessionQualityReport,
    findings,
    format_time,
    signal_row,
    source_rows,
    summarize_snapshot,
)
from ..analysis import AnalysisContext, AnalysisTool
from ..interval_region import IntervalRegion, PlotTimeScale

FINDING_COLUMNS = ("Start", "End", "Signal", "Finding", "Evidence", "Detail")
COLORS = {
    "coverage": "#8c8c8c",
    "gap": "#d62728",
    "unavailable": "#ff7f0e",
    "counter": "#1f77b4",
    "source": "#9467bd",
}
SOURCE_ROW = -1
"""Timeline row for source events, above the first signal."""


def _table(columns: tuple[str, ...]) -> QTableWidget:
    table = QTableWidget(0, len(columns))
    table.setHorizontalHeaderLabels(list(columns))
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.setWordWrap(False)
    vertical = table.verticalHeader()
    if vertical is not None:
        vertical.setVisible(False)
    header = table.horizontalHeader()
    if header is not None:
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
    return table


def _fill(table: QTableWidget, rows: list[tuple[str, ...]]) -> None:
    table.setRowCount(len(rows))
    for row, cells in enumerate(rows):
        for column, text in enumerate(cells):
            table.setItem(row, column, QTableWidgetItem(text))


def _cells(table: QTableWidget) -> list[tuple[str, ...]]:
    return [
        tuple(
            (item.text() if (item := table.item(row, column)) is not None else "")
            for column in range(table.columnCount())
        )
        for row in range(table.rowCount())
    ]


class QualityTimeline(pg.PlotWidget):
    """One row per signal with gaps, unavailable runs and counter events over time."""

    def __init__(self, context: AnalysisContext, tool: DataQualityTool) -> None:
        super().__init__()
        self.context = context
        self.tool = tool
        self.setMinimumSize(320, 140)
        self.setMenuEnabled(False)
        self.setMouseEnabled(x=True, y=False)
        self.hideButtons()
        self.showGrid(x=True, y=False, alpha=0.2)
        self.plot_item = cast(PlotItem, self.getPlotItem())
        self.view = self.plot_item.getViewBox()
        self.view.invertY(True)
        self.plot_item.setLabel("bottom", "Time", units="s")
        context.style_plot(self)
        self.time_scale = PlotTimeScale.seconds(0)
        self.region: IntervalRegion | None = None
        self.findings: list[tuple[int, Finding]] = []
        cursor_pen = pg.mkPen("#2ca02c", width=1)
        self.cursor_line = pg.InfiniteLine(angle=90, movable=False, pen=cursor_pen)
        self.cursor_line.setZValue(20)
        self.plot_item.addItem(self.cursor_line, ignoreBounds=True)
        self.cursor_line.hide()
        self._items: list[pg.GraphicsObject] = []
        cast(GraphicsScene, self.scene()).sigMouseClicked.connect(self._clicked)

    def set_origin(self, origin_ns: int) -> None:
        """Rebind the interval region when the time origin changes (new session)."""

        if self.region is not None and self.time_scale.origin_ns == origin_ns:
            return
        if self.region is not None:
            self.region.detach()
        self.time_scale = PlotTimeScale.seconds(origin_ns)
        self.region = IntervalRegion(self, self.context.selection, self.time_scale)
        self.show_cursor(self.context.cursor_ns)

    def show_cursor(self, time_ns: int | None) -> None:
        if time_ns is None or self.region is None:
            self.cursor_line.hide()
            return
        self.cursor_line.setValue(self.time_scale.to_x(time_ns))
        self.cursor_line.show()

    def clear_rows(self) -> None:
        for graphic in self._items:
            self.plot_item.removeItem(graphic)
        self._items.clear()
        self.findings = []

    def show_report(self, report: SessionQualityReport, placed: list[Finding]) -> None:
        self.clear_rows()
        rows = {item.signal_id: index for index, item in enumerate(report.signals)}
        segments: dict[str, tuple[list[float], list[float]]] = {}
        points: dict[str, tuple[list[float], list[float]]] = {}

        def segment(kind: str, row: int, start_ns: int, end_ns: int) -> None:
            xs, ys = segments.setdefault(kind, ([], []))
            xs += [self.time_scale.to_x(start_ns), self.time_scale.to_x(end_ns)]
            ys += [row, row]

        for item in report.signals:
            timing = item.quality.timing
            if timing.first_ns is not None and timing.last_ns is not None:
                segment("coverage", rows[item.signal_id], timing.first_ns, timing.last_ns)
        for finding in placed:
            if finding.start_ns is None or finding.end_ns is None:
                continue
            row = SOURCE_ROW if finding.kind == "source" else rows.get(finding.signal_id or "")
            if row is None:
                continue
            self.findings.append((row, finding))
            if finding.end_ns > finding.start_ns and finding.kind != "source":
                segment(finding.kind, row, finding.start_ns, finding.end_ns)
            else:
                xs, ys = points.setdefault(finding.kind, ([], []))
                xs.append(self.time_scale.to_x(finding.start_ns))
                ys.append(row)
        for kind, (xs, ys) in segments.items():
            width = 2 if kind == "coverage" else 8
            curve = pg.PlotDataItem(
                xs, ys, connect="pairs", pen=pg.mkPen(COLORS[kind], width=width)
            )
            self._add(curve)
        for kind, (xs, ys) in points.items():
            symbol = "t" if kind == "source" else "o"
            scatter = pg.ScatterPlotItem(
                xs, ys, symbol=symbol, size=10, brush=pg.mkBrush(COLORS[kind]), pen=None
            )
            self._add(scatter)
        labels = [(SOURCE_ROW, "source events")] + [
            (index, item.descriptor.display_name) for index, item in enumerate(report.signals)
        ]
        self.plot_item.getAxis("left").setTicks([labels])
        self.view.setYRange(SOURCE_ROW - 0.5, len(report.signals) - 0.5, padding=0)

    def _add(self, graphic: pg.GraphicsObject) -> None:
        self.plot_item.addItem(graphic)
        self._items.append(graphic)

    def finding_at(self, x: float, y: float) -> Finding | None:
        """The selectable finding drawn nearest to plot point ``(x, y)``, if close enough."""

        row = round(y)
        (x_min, x_max), _ = self.view.viewRange()
        width = self.view.width() or 1.0
        tolerance = 6 * (x_max - x_min) / width
        best: tuple[float, Finding] | None = None
        for finding_row, finding in self.findings:
            if finding_row != row or not finding.selectable:
                continue
            assert finding.start_ns is not None and finding.end_ns is not None
            low, high = self.time_scale.to_x(finding.start_ns), self.time_scale.to_x(finding.end_ns)
            distance = 0.0 if low <= x <= high else min(abs(x - low), abs(x - high))
            if distance <= tolerance and (best is None or distance < best[0]):
                best = (distance, finding)
        return None if best is None else best[1]

    def select_at(self, x: float, y: float) -> TimeInterval | None:
        finding = self.finding_at(x, y)
        return None if finding is None else self.tool.select_finding(finding)

    def _clicked(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        position: QPointF = self.view.mapSceneToView(event.scenePos())
        if self.select_at(position.x(), position.y()) is not None:
            event.accept()


class DataQualityTool(QWidget):
    """Summarize the shared interval; recompute at most every 200 ms while data streams."""

    def __init__(self, context: AnalysisContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self.report: SessionQualityReport | None = None
        self.session_report: SessionQualityReport | None = None
        self.finding_rows: list[Finding] = []
        self._origin_session: object | None = None
        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.whole_session = QPushButton("Whole session")
        self.whole_session.setToolTip("Clear the shared interval and summarize the whole session")
        self.whole_session.clicked.connect(context.selection.clear)
        header = QHBoxLayout()
        header.addWidget(self.hint, 1)
        header.addWidget(self.whole_session)
        self.timeline = QualityTimeline(context, self)
        self.signals = _table(SIGNAL_COLUMNS)
        self.found = _table(FINDING_COLUMNS)
        self.found.cellClicked.connect(lambda row, _column: self.select_row(row))
        self.sources = _table(("Quantity", "Value"))
        self.details = _table(("Quantity", "Value"))
        self.tabs = QTabWidget()
        self.tabs.addTab(self.signals, "Signals")
        self.tabs.addTab(self.found, "Gaps and events")
        self.tabs.addTab(self.sources, "Sources")
        self.tabs.addTab(self.details, "Selected signal")
        layout = QVBoxLayout(self)
        layout.addLayout(header)
        layout.addWidget(self.timeline, 1)
        layout.addWidget(self.tabs, 2)
        self.setMinimumSize(360, 320)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self.refresh)
        for change in (context.snapshot_changed, context.signal_changed, context.interval_changed):
            change.connect(self.schedule)
        context.cursor_changed.connect(lambda: self.timeline.show_cursor(context.cursor_ns))
        self.refresh()

    def schedule(self) -> None:
        # Do not restart a pending refresh, or a streaming source would starve it.
        if not self.timer.isActive():
            self.timer.start()

    def refresh(self) -> None:
        self.timer.stop()
        context = self.context
        snapshot = context.snapshot
        bounds = context.bounds
        if snapshot is None:
            self._clear("Load a timestamped CSV or a simulator to summarize its data quality.")
            return
        interval = context.interval
        session_report = summarize_snapshot(snapshot)
        report = (
            session_report
            if interval is None
            else summarize_snapshot(snapshot, start_ns=interval.start_ns, end_ns=interval.end_ns)
        )
        self.session_report, self.report = session_report, report
        scope = (
            "whole session"
            if interval is None
            else f"interval {format_time(interval.start_ns)} to {format_time(interval.end_ns)}"
        )
        if bounds is not None and (
            self.timeline.region is None or context.session is not self._origin_session
        ):
            # Keep one origin per session so the timeline does not shift while it streams.
            self._origin_session = context.session
            self.timeline.set_origin(bounds.start_ns)
        text = (
            f"Data quality of the {scope}. Click a gap or unavailable run to select it. "
            "[recorded] = stored by the source or file, [inferred] = derived from "
            "timestamps, [retention] = removed by the session memory limit, not data loss."
        )
        if report.evicted:
            text += (
                f" Retention evicted {report.evicted} older records; "
                "the summary covers the retained records only."
            )
        self.hint.setText(text)
        self.whole_session.setEnabled(interval is not None)
        _fill(self.signals, [signal_row(item) for item in report.signals])
        self.finding_rows = findings(report)
        _fill(
            self.found,
            [
                (
                    format_time(item.start_ns),
                    format_time(item.end_ns),
                    item.signal_id or "-",
                    item.kind,
                    item.evidence.value,
                    item.detail,
                )
                for item in self.finding_rows
            ],
        )
        source_cells: list[tuple[str, ...]] = []
        for source in report.sources:
            source_cells += source_rows(source)
        _fill(self.sources, source_cells)
        self._show_details(report)
        self.timeline.show_report(session_report, findings(session_report))
        self.timeline.show_cursor(context.cursor_ns)

    def _show_details(self, report: SessionQualityReport) -> None:
        item = report.signal(self.context.selected_id or "")
        if item is None:
            _fill(self.details, [("Signal", "select a signal in the browser")])
            return
        rows = list(format_quality(item.quality))
        rows[0] = ("Signal", f"{item.descriptor.display_name} ({item.signal_id})")
        cells = signal_row(item)
        rows.insert(1, ("Expected from declared rate [inferred]", cells[3]))
        rows.insert(2, ("Evicted by retention [retention]", cells[7]))
        rows += [
            (f"Quality label {label!r} [recorded]", str(count))
            for label, count in item.quality_labels
        ]
        if item.counter_note:
            rows.append(("Sequence counter", f"not used: {item.counter_note}"))
        _fill(self.details, rows)

    def _clear(self, message: str) -> None:
        self.report = self.session_report = None
        self.finding_rows = []
        self.hint.setText(message)
        self.whole_session.setEnabled(False)
        for table in (self.signals, self.found, self.sources, self.details):
            table.setRowCount(0)
        self.timeline.clear_rows()

    def select_row(self, row: int) -> TimeInterval | None:
        if not 0 <= row < len(self.finding_rows):
            return None
        return self.select_finding(self.finding_rows[row])

    def select_finding(self, finding: Finding) -> TimeInterval | None:
        """Make a gap, unavailable run or counter event the shared interval."""

        if not finding.selectable or self.context.bounds is None:
            return None
        assert finding.start_ns is not None and finding.end_ns is not None
        try:
            return self.context.selection.select(finding.start_ns, finding.end_ns)
        except ValueError:
            return None

    def rows(self, table: QTableWidget) -> list[tuple[str, ...]]:
        return _cells(table)


TOOL = AnalysisTool("data_quality", "Data quality", DataQualityTool)
