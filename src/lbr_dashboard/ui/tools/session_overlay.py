"""Compare sessions tool: overlay a second recorded run on the main session's timeline.

The main session stays the reference. A comparison session is read in full on a
background worker from a file or a finite source, kept as a snapshot and never
replaces the main source. ``lbr_dashboard.core.session_alignment`` maps the
comparison run onto the main timeline with one manual offset and reports
overlap, missing signals and unit or frame mismatches.

This module never opens files itself: the application passes a
:class:`ComparisonSources` with the readers it supports.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from html import escape
from numbers import Real
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ...core import (
    BackgroundService,
    CancellationToken,
    SessionRecord,
    SessionSnapshot,
    SignalDescriptor,
    SourceHealth,
    SourceState,
    TelemetrySession,
    TelemetrySource,
)
from ...core.session_alignment import (
    IssueKind,
    MatchIssue,
    Overlap,
    SessionAlignment,
    SessionData,
    SessionIdentity,
    Side,
    SignalMatch,
    SignalTrace,
    alignment_from_dict,
    check_matches,
    overlap,
    samples_in_window,
)
from ..analysis import AnalysisContext, AnalysisTool, is_numeric
from ..multi_trace_plot import COLORS, LIGHT_COLORS

NANOSECONDS = 1_000_000_000
NS_PER_MS = 1_000_000
MAX_OFFSET_MS = 1e12
NOT_COMPARED = "(not compared)"
RUN_NAMES = {Side.REFERENCE: "Main run", Side.COMPARED: "Comparison run"}

Prepare = Callable[[CancellationToken], Callable[[], TelemetrySource]]


@dataclass(frozen=True)
class ComparisonPreset:
    """A finite source that can be read as a comparison run, e.g. a simulator."""

    key: str
    label: str
    prepare: Prepare


@dataclass(frozen=True)
class ComparisonSources:
    """The readers the application offers for comparison sessions.

    ``open_file`` turns a chosen path into a preparation step that runs on a
    worker; ``presets`` are finite sources selectable by key.
    """

    open_file: Callable[[Path], Prepare] | None = None
    file_filter: str = "CSV files (*.csv)"
    presets: tuple[ComparisonPreset, ...] = ()


# Reading and converting sessions --------------------------------------------


def read_finite_session(prepare: Prepare, token: CancellationToken) -> SessionSnapshot:
    """Prepare, open and drain a finite source completely; return its snapshot.

    Runs on a worker thread. Live sources are refused because they never end.
    A source that fails while reading raises ``ValueError`` with its message.
    """

    factory = prepare(token)
    token.raise_if_cancelled()
    source = factory()
    if source.capabilities.is_live:
        raise ValueError(
            f"{source.source_id} is a live source; choose a finite recording to compare"
        )
    session = TelemetrySession(source.schema)
    health: SourceHealth | None = None
    source.open(token)
    try:
        source.start(token)
        while True:
            token.raise_if_cancelled()
            finished = source.join(0)
            count = session.ingest_source(source)
            if finished and source.events.stats.buffered == 0:
                break
            if count == 0:
                token.wait(0.002)
        health = source.health()
    finally:
        source.close(1.0)
    if health is not None and health.state is SourceState.FAILED:
        error = health.last_error
        raise ValueError(error.message if error is not None else "the source failed")
    return session.snapshot()


def _as_float(sample_value: object, available: bool) -> float | None:
    if not available or isinstance(sample_value, bool) or not isinstance(sample_value, Real):
        return None
    return float(sample_value)


def signal_unit(descriptor: SignalDescriptor, records: tuple[SessionRecord, ...]) -> str | None:
    """The descriptor's unit, else the one unit every reading states, else None."""

    if descriptor.canonical_unit is not None:
        return descriptor.canonical_unit
    units = {record.sample.unit for record in records}
    return units.pop() if len(units) == 1 else None


def signal_frame(descriptor: SignalDescriptor) -> str | None:
    """A reference frame named in the descriptor metadata, if any."""

    for mapping in (descriptor.extra, descriptor.display):
        frame = mapping.get("frame")
        if isinstance(frame, str) and frame.strip():
            return frame
    return None


def session_identity(snapshot: SessionSnapshot, label: str) -> SessionIdentity:
    """Identify a session by its source ids, with a readable label."""

    sources = sorted({descriptor.source_id for descriptor in snapshot.schema.signals})
    return SessionIdentity(" + ".join(sources) if sources else "session", label)


def session_data(snapshot: SessionSnapshot, identity: SessionIdentity) -> SessionData:
    """Turn the numeric scalar signals of a snapshot into alignment traces."""

    traces = []
    for descriptor in snapshot.schema.signals:
        if not is_numeric(descriptor):
            continue
        records = snapshot.series.get(descriptor.identifier, ())
        traces.append(
            SignalTrace(
                descriptor.identifier,
                [record.sample.timestamp_ns for record in records],
                [_as_float(record.sample.value, record.sample.available) for record in records],
                unit=signal_unit(descriptor, records),
                frame=signal_frame(descriptor),
            )
        )
    return SessionData(identity, traces)


def reading_at(trace: SignalTrace, time_ns: int) -> tuple[int, float | None] | None:
    """The last reading at or before ``time_ns`` (time, value), or None before the first."""

    index = bisect_right(trace.times_ns, time_ns)
    if index == 0:
        return None
    return trace.times_ns[index - 1], trace.values[index - 1]


@dataclass(frozen=True)
class CursorRow:
    """Both runs' values of one matched pair at the cursor.

    ``difference`` is comparison minus main, only when both values exist and
    the units are the same (two unspecified units count as the same).
    """

    match: SignalMatch
    unit: str | None
    reference: float | None
    compared: float | None
    difference: float | None
    units_match: bool


def cursor_rows(
    alignment: SessionAlignment,
    reference: SessionData,
    compared: SessionData,
    cursor_ns: int,
) -> list[CursorRow]:
    """Values of every matched pair at a comparison-time cursor."""

    rows = []
    for match in alignment.matches:
        ref = reference.signal(match.reference_signal_id)
        cmp = compared.signal(match.compared_signal_id)
        values: dict[Side, float | None] = {}
        for side, trace in ((Side.REFERENCE, ref), (Side.COMPARED, cmp)):
            reading = (
                reading_at(trace, alignment.to_original_ns(side, cursor_ns))
                if trace is not None
                else None
            )
            values[side] = reading[1] if reading is not None else None
        same_unit = ref is not None and cmp is not None and ref.unit == cmp.unit
        first, second = values[Side.REFERENCE], values[Side.COMPARED]
        difference = (
            second - first if same_unit and first is not None and second is not None else None
        )
        unit = ref.unit if ref is not None else cmp.unit if cmp is not None else None
        rows.append(CursorRow(match, unit, first, second, difference, same_unit))
    return rows


def default_matches(reference: SessionData, compared: SessionData) -> list[SignalMatch]:
    """Pair signals with the identical id in both sessions."""

    return [
        SignalMatch(trace.signal_id, trace.signal_id)
        for trace in reference.signals
        if compared.signal(trace.signal_id) is not None
    ]


def format_value(value: float | None, unit: str | None) -> str:
    if value is None:
        return "--"
    return f"{value:.6g} {unit}" if unit else f"{value:.6g}"


def format_seconds(time_ns: int, origin_ns: int) -> str:
    return f"{(time_ns - origin_ns) / NANOSECONDS:.3f} s"


def format_duration(duration_ns: int) -> str:
    return f"{duration_ns / NANOSECONDS:.3f} s"


# The tool ---------------------------------------------------------------------


@dataclass(frozen=True)
class LoadedComparison:
    snapshot: SessionSnapshot
    data: SessionData
    origin: dict[str, str]


class SessionOverlayTool(QWidget):
    """Overlay matched signals of the main run and a comparison run on one time axis."""

    def __init__(
        self,
        context: AnalysisContext,
        parent: QWidget | None = None,
        *,
        sources: ComparisonSources | None = None,
    ) -> None:
        super().__init__(parent)
        self.context = context
        self.sources = sources or ComparisonSources()
        self.reference: SessionData | None = None
        self.comparison: LoadedComparison | None = None
        self.matches: list[SignalMatch] = []
        self.offset_ns = 0
        self.hidden: set[str] = set()
        self.issues: tuple[MatchIssue, ...] = ()
        self.overlap: Overlap | None = None
        self.curves: dict[tuple[Side, str], pg.PlotDataItem] = {}
        self.legend_labels: dict[tuple[Side, str], str] = {}
        self._reference_snapshot: SessionSnapshot | None = None
        self._loading: tuple[BackgroundService, list[SessionSnapshot], dict[str, str], str] | None
        self._loading = None
        self._pending: tuple[list[SignalMatch], int] | None = None
        self._table_key: object = None

        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.source_choice = QComboBox()
        self.source_choice.setAccessibleName("Comparison source")
        if self.sources.open_file is not None:
            self.source_choice.addItem("Comparison file...", "file")
        for preset in self.sources.presets:
            self.source_choice.addItem(preset.label, f"preset:{preset.key}")
        self.load_button = QPushButton("Load comparison")
        self.load_button.clicked.connect(self.load_chosen)
        self.load_button.setEnabled(self.source_choice.count() > 0)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.offset = QDoubleSpinBox()
        self.offset.setAccessibleName("Comparison offset in milliseconds")
        self.offset.setDecimals(3)
        self.offset.setRange(-MAX_OFFSET_MS, MAX_OFFSET_MS)
        self.offset.setSuffix(" ms")
        self.offset.setToolTip(
            "Added to comparison-run times; a positive offset moves the comparison run later."
        )
        self.offset.valueChanged.connect(self._offset_edited)
        offset_label = QLabel("Offset:")
        offset_label.setBuddy(self.offset)
        self.align_button = QPushButton("Align at cursor")
        self.align_button.setToolTip("Move the comparison run so it starts at the replay cursor.")
        self.align_button.clicked.connect(self.align_at_cursor)
        self.show_reference = QCheckBox("Main run")
        self.show_compared = QCheckBox("Comparison run")
        for box in (self.show_reference, self.show_compared):
            box.setChecked(True)
            box.toggled.connect(self._apply_visibility)
        self.messages = QLabel()
        self.messages.setWordWrap(True)
        self.messages.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setMinimumSize(360, 220)
        context.style_plot(self.plot_widget)
        plot = self.plot_widget.getPlotItem()
        assert plot is not None
        self.plot = plot
        plot.showGrid(x=True, y=True, alpha=0.2)
        plot.setLabel("bottom", "Main run time since first reading", units="s")
        self.legend = plot.addLegend()
        self.overlap_region = pg.LinearRegionItem(
            movable=False, brush=pg.mkBrush(128, 128, 128, 40)
        )
        self.overlap_region.setZValue(-10)
        self.overlap_region.setVisible(False)
        plot.addItem(self.overlap_region, ignoreBounds=True)
        self.cursor_line = pg.InfiniteLine(angle=90, movable=False)
        self.cursor_line.setVisible(False)
        plot.addItem(self.cursor_line, ignoreBounds=True)

        self.cursor_label = QLabel()
        self.cursor_label.setWordWrap(True)
        self.readout = _table(
            ("Signal", "Main run", "Comparison run", "Difference (comparison - main)")
        )
        self.match_table = _table(("Plot", "Main run signal", "Comparison run signal"))
        self.match_table.itemChanged.connect(self._item_changed)
        cursor_page = QWidget()
        cursor_layout = QVBoxLayout(cursor_page)
        cursor_layout.setContentsMargins(0, 0, 0, 0)
        cursor_layout.addWidget(self.cursor_label)
        cursor_layout.addWidget(self.readout)
        self.tabs = QTabWidget()
        self.tabs.addTab(cursor_page, "At cursor")
        self.tabs.addTab(self.match_table, "Matched signals")

        load_row = QHBoxLayout()
        load_row.addWidget(self.source_choice, 1)
        load_row.addWidget(self.load_button)
        align_row = QHBoxLayout()
        align_row.addWidget(offset_label)
        align_row.addWidget(self.offset)
        align_row.addWidget(self.align_button)
        align_row.addStretch(1)
        align_row.addWidget(QLabel("Show:"))
        align_row.addWidget(self.show_reference)
        align_row.addWidget(self.show_compared)
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.plot_widget)
        splitter.addWidget(self.tabs)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setChildrenCollapsible(False)
        layout = QVBoxLayout(self)
        layout.addWidget(self.hint)
        layout.addLayout(load_row)
        layout.addWidget(self.status)
        layout.addLayout(align_row)
        layout.addWidget(self.messages)
        layout.addWidget(splitter, 1)

        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.refresh)
        self.load_timer = QTimer(self)
        self.load_timer.setInterval(30)
        self.load_timer.timeout.connect(self.poll_loading)
        context.snapshot_changed.connect(self.timer.start)
        context.theme_changed.connect(self.timer.start)
        context.cursor_changed.connect(self.update_cursor)
        if self.source_choice.count() == 0:
            self.status.setText("This window has no comparison readers.")
        else:
            self.status.setText("No comparison run loaded. The main session is never replaced.")
        self.refresh()

    # Loading -------------------------------------------------------------------

    def load_chosen(self) -> None:
        """Load the source chosen in the combo box, asking for a file when needed."""

        choice = self.source_choice.currentData()
        if choice == "file":
            path, _ = QFileDialog.getOpenFileName(
                self, "Open comparison session", "", self.sources.file_filter
            )
            if path:
                self.load_file(path)
        elif isinstance(choice, str) and choice.startswith("preset:"):
            self.load_preset(choice.removeprefix("preset:"))

    def load_file(self, path: str | Path, base_dir: Path | None = None) -> None:
        """Read a comparison file; a relative ``path`` is resolved against ``base_dir``."""

        if self.sources.open_file is None:
            self.status.setText("Comparison files cannot be opened in this window.")
            return
        chosen = Path(path)
        resolved = chosen if chosen.is_absolute() or base_dir is None else base_dir / chosen
        self._start_loading(
            self.sources.open_file(resolved), {"path": str(path)}, resolved.name or str(path)
        )

    def load_preset(self, key: str) -> None:
        preset = next((item for item in self.sources.presets if item.key == key), None)
        if preset is None:
            self.status.setText(f"Unknown comparison source {key!r}.")
            return
        self._start_loading(preset.prepare, {"preset": key}, preset.label)

    @property
    def loading(self) -> bool:
        return self._loading is not None

    def cancel_loading(self) -> None:
        if self._loading is not None:
            self._loading[0].cancel()
            self._loading = None
            self.load_timer.stop()

    def _start_loading(self, prepare: Prepare, origin: dict[str, str], label: str) -> None:
        self.cancel_loading()
        result: list[SessionSnapshot] = []

        def work(token: CancellationToken, _emit: object) -> None:
            result.append(read_finite_session(prepare, token))

        service = BackgroundService(work)
        self._loading = (service, result, origin, label)
        service.start()
        self.status.setText(f"Loading {label} as the comparison run...")
        self.load_timer.start()

    def poll_loading(self) -> None:
        """Pick up a finished comparison read; called by a timer on the GUI thread."""

        loading = self._loading
        if loading is None:
            self.load_timer.stop()
            return
        service, result, origin, label = loading
        if not service.join(0):
            return
        self._loading = None
        self.load_timer.stop()
        failure = service.failure
        if failure is not None:
            self._pending = None
            self.status.setText(
                f"Could not load {label}: {failure.message}. "
                "The main session and any previous comparison are unchanged."
            )
            return
        if result:
            self._set_comparison(result[0], origin, label)

    def _set_comparison(
        self, snapshot: SessionSnapshot, origin: dict[str, str], label: str
    ) -> None:
        identity = session_identity(snapshot, label)
        main = self._main_identity()
        if main is not None and identity.session_id == main.session_id:
            identity = SessionIdentity(f"{identity.session_id} (comparison)", label)
        data = session_data(snapshot, identity)
        self.comparison = LoadedComparison(snapshot, data, origin)
        pending, self._pending = self._pending, None
        if pending is not None:
            self.matches, offset_ns = list(pending[0]), pending[1]
        else:
            self.matches = (
                default_matches(self.reference, data) if self.reference is not None else []
            )
            offset_ns = 0
        self._set_offset(offset_ns)
        count = sum(len(trace.times_ns) for trace in data.signals)
        self.status.setText(
            f"Comparison run: {label} ({len(data.signals)} numeric signals, {count} readings)."
        )
        self.refresh()

    def clear_comparison(self) -> None:
        self.cancel_loading()
        self.comparison = None
        self.matches = []
        self._set_offset(0)
        self.status.setText("No comparison run loaded. The main session is never replaced.")
        self.refresh()

    # Alignment ------------------------------------------------------------------

    def _main_identity(self) -> SessionIdentity | None:
        snapshot = self.context.snapshot
        return session_identity(snapshot, "main session") if snapshot is not None else None

    def alignment(self) -> SessionAlignment | None:
        """The current alignment, or None until both runs are available."""

        if self.reference is None or self.comparison is None:
            return None
        # One comparison signal per main-run signal, as the Matched signals table shows.
        unique: dict[str, SignalMatch] = {}
        for match in self.matches:
            unique.setdefault(match.reference_signal_id, match)
        return SessionAlignment(
            self.reference.identity,
            self.comparison.data.identity,
            self.offset_ns,
            tuple(unique.values()),
        )

    def set_offset_ns(self, offset_ns: int) -> None:
        self._set_offset(offset_ns)
        self.refresh()

    def _set_offset(self, offset_ns: int) -> None:
        self.offset_ns = int(offset_ns)
        self.offset.blockSignals(True)
        self.offset.setValue(self.offset_ns / NS_PER_MS)
        self.offset.blockSignals(False)

    def _offset_edited(self, value_ms: float) -> None:
        self.offset_ns = round(value_ms * NS_PER_MS)
        self.refresh()

    def align_at_cursor(self) -> None:
        """Shift the comparison run so its first matched reading is at the replay cursor."""

        cursor = self.context.cursor_ns
        comparison = self.comparison
        if cursor is None or comparison is None:
            self.messages.setText("Load a comparison run and place the replay cursor first.")
            return
        compared_ids = {match.compared_signal_id for match in self.matches}
        traces = [
            trace
            for trace in comparison.data.signals
            if trace.times_ns and (not compared_ids or trace.signal_id in compared_ids)
        ]
        if not traces:
            self.messages.setText("The comparison run has no readings to align.")
            return
        self.set_offset_ns(cursor - min(trace.times_ns[0] for trace in traces))

    def set_match(self, reference_id: str, compared_id: str | None) -> None:
        """Compare ``reference_id`` with ``compared_id``, or stop comparing it with None."""

        matches = [m for m in self.matches if m.reference_signal_id != reference_id]
        if compared_id is not None:
            matches.append(SignalMatch(reference_id, compared_id))
        order = {trace.signal_id: i for i, trace in enumerate(self._reference_signals())}
        matches.sort(key=lambda m: order.get(m.reference_signal_id, len(order)))
        self.matches = matches
        self.refresh()

    def _reference_signals(self) -> tuple[SignalTrace, ...]:
        return tuple(self.reference.signals) if self.reference is not None else ()

    # Refresh --------------------------------------------------------------------

    def refresh(self) -> None:
        self.timer.stop()
        snapshot = self.context.snapshot
        if snapshot is not self._reference_snapshot:
            self._reference_snapshot = snapshot
            identity = self._main_identity()
            self.reference = (
                session_data(snapshot, identity)
                if snapshot is not None and identity is not None
                else None
            )
            comparison = self.comparison
            if (
                comparison is not None
                and identity is not None
                and comparison.data.identity.session_id == identity.session_id
            ):
                renamed = SessionIdentity(
                    f"{identity.session_id} (comparison)", comparison.data.identity.label
                )
                self.comparison = LoadedComparison(
                    comparison.snapshot,
                    session_data(comparison.snapshot, renamed),
                    comparison.origin,
                )
        self.align_button.setEnabled(self.comparison is not None)
        self._rebuild_table()
        alignment = self.alignment()
        if self.reference is None:
            self.hint.setText("Open a main session with the source picker to compare runs.")
        elif self.comparison is None:
            self.hint.setText(
                "Load a comparison run (a file or a finite simulator). It is drawn on the "
                "main run's time axis after the offset below."
            )
        else:
            self.hint.setText(
                "Main run: solid lines. Comparison run: dashed lines at main time = "
                "comparison time + offset. The shaded band is where both runs have data."
            )
        if alignment is None or self.comparison is None or self.reference is None:
            self.issues = ()
            self.overlap = None
            self.messages.setText("")
            self._draw(None)
            self.update_cursor()
            return
        self.issues = check_matches(alignment, self.reference, self.comparison.data)
        self.overlap = overlap(alignment, self.reference, self.comparison.data)
        lines = []
        if not alignment.matches:
            lines.append(
                "No signals are matched. Choose comparison signals on the Matched signals tab."
            )
        lines.extend(_issue_text(issue) for issue in self.issues)
        lines.append(self._overlap_text(self.overlap))
        self.messages.setText("\n".join(lines))
        self._draw(alignment)
        self.update_cursor()

    def _origin_ns(self) -> int:
        bounds = self.context.bounds
        if bounds is not None:
            return bounds.start_ns
        starts = [trace.times_ns[0] for trace in self._reference_signals() if trace.times_ns]
        return min(starts) if starts else 0

    def _overlap_text(self, result: Overlap) -> str:
        origin = self._origin_ns()

        def span(value: tuple[int, int] | None) -> str:
            if value is None:
                return "no readings"
            return f"{format_seconds(value[0], origin)} to {format_seconds(value[1], origin)}"

        ref, cmp = result.reference_span, result.compared_span
        spans = f"Main run {span(ref)}, comparison run {span(cmp)} (main run time)."
        if result.start_ns is not None and result.end_ns is not None:
            return (
                f"Overlap: {format_seconds(result.start_ns, origin)} to "
                f"{format_seconds(result.end_ns, origin)} "
                f"({format_duration(result.duration_ns)}). {spans} "
                "Outside the overlap only one run has data."
            )
        if ref is None or cmp is None:
            return f"No overlap: {result.reason}. {spans}"
        if cmp[1] < ref[0]:
            gap = ref[0] - cmp[1]
            where, change = "ends", "Increase"
            relation = "before the main run starts"
        else:
            gap = cmp[0] - ref[1]
            where, change = "starts", "Decrease"
            relation = "after the main run ends"
        return (
            f"No overlap: the comparison run {where} {format_duration(gap)} {relation}. "
            f"{spans} {change} the offset by at least {gap / NS_PER_MS:g} ms."
        )

    def _palette(self) -> list[str]:
        background = self.context.plot_colors[0]
        light = pg.mkColor(background).lightnessF() > 0.5
        return [LIGHT_COLORS.get(color, color) if light else color for color in COLORS]

    def _draw(self, alignment: SessionAlignment | None) -> None:
        for curve in self.curves.values():
            self.legend.removeItem(curve)
            self.plot.removeItem(curve)
        self.curves.clear()
        self.legend_labels.clear()
        background, text = self.context.plot_colors
        self.legend.setLabelTextColor(text)
        self.legend.setBrush(pg.mkBrush(QColor(background)))
        self.cursor_line.setPen(pg.mkPen(text, width=1, style=Qt.PenStyle.DotLine))
        result = self.overlap
        if alignment is None or self.reference is None or self.comparison is None or result is None:
            self.overlap_region.setVisible(False)
            return
        origin = self._origin_ns()
        if result.start_ns is not None and result.end_ns is not None:
            self.overlap_region.setRegion(
                ((result.start_ns - origin) / NANOSECONDS, (result.end_ns - origin) / NANOSECONDS)
            )
            self.overlap_region.setVisible(True)
        else:
            self.overlap_region.setVisible(False)
        palette = self._palette()
        order = {trace.signal_id: i for i, trace in enumerate(self._reference_signals())}
        sessions = {Side.REFERENCE: self.reference, Side.COMPARED: self.comparison.data}
        for index, match in enumerate(alignment.matches):
            color = palette[order.get(match.reference_signal_id, index) % len(palette)]
            for side, data in sessions.items():
                signal_id = match.signal_id(side)
                trace = data.signal(signal_id)
                if trace is None or trace.span_ns is None:
                    continue
                first, last = trace.span_ns
                shift = alignment.offset_for(side)
                samples = samples_in_window(
                    alignment, side, data, signal_id, first + shift, last + shift
                )
                xs = (np.asarray(samples.comparison_times_ns, dtype=np.int64) - origin) / float(
                    NANOSECONDS
                )
                ys = np.array(
                    [np.nan if value is None else value for value in samples.values],
                    dtype=float,
                )
                dashed = side is Side.COMPARED
                pen = pg.mkPen(
                    color,
                    width=2,
                    style=Qt.PenStyle.DashLine if dashed else Qt.PenStyle.SolidLine,
                )
                curve = pg.PlotDataItem(
                    xs,
                    ys,
                    pen=pen,
                    connect="finite",
                    symbol="s" if dashed else "o",
                    symbolSize=5,
                    symbolPen=pg.mkPen(color),
                    symbolBrush=pg.mkBrush(color) if not dashed else None,
                )
                key = (side, match.reference_signal_id)
                label = self._legend_text(side, data, trace)
                self.plot.addItem(curve)
                self.legend.addItem(curve, escape(label))
                self.curves[key] = curve
                self.legend_labels[key] = label
        self._apply_visibility()

    def _legend_text(self, side: Side, data: SessionData, trace: SignalTrace) -> str:
        snapshot = (
            self.context.snapshot
            if side is Side.REFERENCE
            else self.comparison.snapshot
            if self.comparison is not None
            else None
        )
        descriptor = (
            next(
                (d for d in snapshot.schema.signals if d.identifier == trace.signal_id),
                None,
            )
            if snapshot is not None
            else None
        )
        name = descriptor.display_name if descriptor is not None else trace.signal_id
        unit = trace.unit or "unit unspecified"
        run = f"{RUN_NAMES[side]} ({data.identity.label})"
        return f"{run}: {name} [{unit}] - {trace.signal_id}"

    def _apply_visibility(self) -> None:
        shown = {
            Side.REFERENCE: self.show_reference.isChecked(),
            Side.COMPARED: self.show_compared.isChecked(),
        }
        for (side, reference_id), curve in self.curves.items():
            curve.setVisible(shown[side] and reference_id not in self.hidden)

    def set_pair_visible(self, reference_id: str, visible: bool) -> None:
        if visible:
            self.hidden.discard(reference_id)
        else:
            self.hidden.add(reference_id)
        self._table_key = None
        self._rebuild_table()
        self._apply_visibility()

    # Cursor ---------------------------------------------------------------------

    def cursor_values(self) -> list[CursorRow]:
        alignment = self.alignment()
        cursor = self.context.cursor_ns
        if alignment is None or cursor is None or self.reference is None or self.comparison is None:
            return []
        return cursor_rows(alignment, self.reference, self.comparison.data, cursor)

    def update_cursor(self) -> None:
        cursor = self.context.cursor_ns
        alignment = self.alignment()
        origin = self._origin_ns()
        if cursor is None or alignment is None:
            self.cursor_line.setVisible(False)
            self.cursor_label.setText(
                "Values at the replay cursor appear here once both runs are loaded."
            )
            self.readout.setRowCount(0)
            return
        self.cursor_line.setValue((cursor - origin) / NANOSECONDS)
        self.cursor_line.setVisible(True)
        original = alignment.to_original_ns(Side.COMPARED, cursor)
        self.cursor_label.setText(
            f"Cursor at {format_seconds(cursor, origin)} main run time; comparison run "
            f"recording time {original / NANOSECONDS:.3f} s. Values are the last reading "
            "at or before the cursor."
        )
        rows = self.cursor_values()
        self.readout.setRowCount(len(rows))
        for index, row in enumerate(rows):
            if row.difference is not None:
                difference = format_value(row.difference, row.unit)
                if not difference.startswith("-"):
                    difference = "+" + difference
            elif not row.units_match:
                difference = "-- (units differ)"
            else:
                difference = "--"
            reference = (
                self.reference.signal(row.match.reference_signal_id) if self.reference else None
            )
            compared = (
                self.comparison.data.signal(row.match.compared_signal_id)
                if self.comparison
                else None
            )
            cells = (
                _pair_name(row.match),
                format_value(row.reference, reference.unit if reference else None),
                format_value(row.compared, compared.unit if compared else None),
                difference,
            )
            for column, cell in enumerate(cells):
                self.readout.setItem(index, column, QTableWidgetItem(cell))

    def readout_rows(self) -> list[tuple[str, ...]]:
        """The cursor table as text, in display order."""

        return _table_text(self.readout)

    def legend_texts(self) -> list[str]:
        return [label.text for _sample, label in self.legend.items]

    # Matched signals table ------------------------------------------------------

    def _rebuild_table(self) -> None:
        reference = self._reference_signals()
        compared = tuple(self.comparison.data.signals) if self.comparison is not None else ()
        key = (
            tuple(trace.signal_id for trace in reference),
            tuple(trace.signal_id for trace in compared),
            tuple(self.matches),
            frozenset(self.hidden),
            id(self.comparison),
        )
        if key == self._table_key:
            return
        self._table_key = key
        rows = [trace.signal_id for trace in reference]
        rows.extend(
            m.reference_signal_id for m in self.matches if m.reference_signal_id not in rows
        )
        table = self.match_table
        table.blockSignals(True)
        table.setRowCount(len(rows))
        for index, reference_id in enumerate(rows):
            plot_item = QTableWidgetItem()
            plot_item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            plot_item.setData(Qt.ItemDataRole.UserRole, reference_id)
            plot_item.setCheckState(
                Qt.CheckState.Unchecked if reference_id in self.hidden else Qt.CheckState.Checked
            )
            table.setItem(index, 0, plot_item)
            trace = self.reference.signal(reference_id) if self.reference is not None else None
            name = (
                self._signal_label(self.context.snapshot, trace)
                if trace is not None
                else f"missing: {reference_id}"
            )
            table.setItem(index, 1, QTableWidgetItem(name))
            combo = QComboBox()
            combo.addItem(NOT_COMPARED, None)
            for candidate in compared:
                snapshot = self.comparison.snapshot if self.comparison is not None else None
                combo.addItem(self._signal_label(snapshot, candidate), candidate.signal_id)
            current = next(
                (
                    m.compared_signal_id
                    for m in self.matches
                    if m.reference_signal_id == reference_id
                ),
                None,
            )
            if current is not None and combo.findData(current) < 0:
                combo.addItem(f"missing: {current}", current)
            combo.setCurrentIndex(max(combo.findData(current), 0) if current is not None else 0)
            combo.setAccessibleName(f"Comparison signal for {reference_id}")
            combo.currentIndexChanged.connect(
                lambda _index, ref=reference_id, box=combo: self.set_match(ref, box.currentData())
            )
            table.setCellWidget(index, 2, combo)
        table.blockSignals(False)

    @staticmethod
    def _signal_label(snapshot: SessionSnapshot | None, trace: SignalTrace) -> str:
        descriptor = (
            next((d for d in snapshot.schema.signals if d.identifier == trace.signal_id), None)
            if snapshot is not None
            else None
        )
        name = descriptor.display_name if descriptor is not None else trace.signal_id
        return f"{name} [{trace.unit or 'unit unspecified'}] - {trace.signal_id}"

    def _item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != 0:
            return
        reference_id = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(reference_id, str):
            self.set_pair_visible(reference_id, item.checkState() == Qt.CheckState.Checked)

    def match_combo(self, reference_id: str) -> QComboBox:
        """The comparison-signal chooser of one main-run signal row."""

        for row in range(self.match_table.rowCount()):
            item = self.match_table.item(row, 0)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == reference_id:
                widget = self.match_table.cellWidget(row, 2)
                assert isinstance(widget, QComboBox)
                return widget
        raise KeyError(reference_id)

    # Save and restore -----------------------------------------------------------

    def configuration(self, relative_to: Path | None = None) -> dict[str, object]:
        """Return the comparison as plain data: source, alignment and visibility.

        A file path is stored as chosen, or relative to ``relative_to`` when the
        file lies inside it. ``alignment`` is a ``SessionAlignment.to_dict()``
        document holding the matched signal ids and the offset.
        """

        comparison = self.comparison
        origin: dict[str, str] | None = dict(comparison.origin) if comparison else None
        if origin is not None and "path" in origin and relative_to is not None:
            path = Path(origin["path"])
            if path.is_absolute():
                try:
                    origin["path"] = path.relative_to(relative_to).as_posix()
                except ValueError:
                    pass
        alignment = self.alignment()
        return {
            "comparison": origin,
            "alignment": alignment.to_dict() if alignment is not None else None,
            "show_main": self.show_reference.isChecked(),
            "show_comparison": self.show_compared.isChecked(),
            "hidden": sorted(self.hidden),
        }

    def restore_configuration(
        self, config: Mapping[str, object], base_dir: Path | None = None
    ) -> None:
        """Apply :meth:`configuration` output and reload the comparison run.

        Relative file paths are resolved against ``base_dir``. The saved matches
        and offset are applied once the comparison run has been read. Invalid
        documents raise ``ValueError`` and change nothing.
        """

        comparison = config.get("comparison")
        document = config.get("alignment")
        hidden = config.get("hidden", [])
        flags = [config.get(name, True) for name in ("show_main", "show_comparison")]
        if comparison is not None and not (
            isinstance(comparison, Mapping)
            and len(comparison) == 1
            and (
                isinstance(comparison.get("path"), str) or isinstance(comparison.get("preset"), str)
            )
        ):
            raise ValueError("comparison must be null, {'path': str} or {'preset': str}")
        alignment = alignment_from_dict(document) if document is not None else None
        if not isinstance(hidden, list) or not all(isinstance(item, str) for item in hidden):
            raise ValueError("hidden must be a list of signal ids")
        if not all(isinstance(flag, bool) for flag in flags):
            raise ValueError("show_main and show_comparison must be true or false")
        self.hidden = set(hidden)
        self.show_reference.setChecked(bool(flags[0]))
        self.show_compared.setChecked(bool(flags[1]))
        self._table_key = None
        if comparison is None:
            self._pending = None
            self.clear_comparison()
            return
        assert isinstance(comparison, Mapping)
        self._pending = (
            (list(alignment.matches), alignment.offset_ns) if alignment is not None else None
        )
        path = comparison.get("path")
        if isinstance(path, str):
            self.load_file(path, base_dir)
        else:
            self.load_preset(str(comparison.get("preset")))


def _issue_text(issue: MatchIssue) -> str:
    kind = {
        IssueKind.MISSING_SIGNAL: "Missing signal",
        IssueKind.UNIT_MISMATCH: "Unit mismatch",
        IssueKind.FRAME_MISMATCH: "Frame mismatch",
    }[issue.kind]
    detail = issue.message
    for old, new in (
        ("reference session", "main run"),
        ("compared session", "comparison run"),
        (": reference ", ": main run "),
        (", compared ", ", comparison run "),
    ):
        detail = detail.replace(old, new)
    return f"{kind}: {detail}."


def _pair_name(match: SignalMatch) -> str:
    if match.reference_signal_id == match.compared_signal_id:
        return match.reference_signal_id
    return f"{match.reference_signal_id} vs {match.compared_signal_id}"


def _table(headers: tuple[str, ...]) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(list(headers))
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.verticalHeader().setVisible(False)
    header = table.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    header.setStretchLastSection(True)
    return table


def _table_text(table: QTableWidget) -> list[tuple[str, ...]]:
    rows = []
    for row in range(table.rowCount()):
        cells = [table.item(row, column) for column in range(table.columnCount())]
        rows.append(tuple(cell.text() if cell is not None else "" for cell in cells))
    return rows


TOOL = AnalysisTool("compare_sessions", "Compare sessions", SessionOverlayTool)
