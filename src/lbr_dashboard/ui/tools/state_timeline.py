"""State timeline tool: boolean, status, category and event signals on one time axis.

Each ticked signal gets one row. Rows are rebuilt from the session for the
visible time range only (after every pan, zoom or new snapshot), so the cost
of drawing follows the view and the bounded output of the state timeline
module, not the session length.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil, floor

import pyqtgraph as pg
from pyqtgraph.graphicsItems.PlotItem import PlotItem
from PySide6.QtCore import QSignalBlocker, Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core import SignalDescriptor
from ...core.state_timeline import StateTimeline, build_state_timeline, category_value
from ..analysis import AnalysisContext, AnalysisTool
from ..interval_region import IntervalRegion, PlotTimeScale
from ..plot_style import CURSOR_COLOR
from ..state_timeline_widget import event_markers, segment_bars, segment_labels
from ..time_plot import TimePlot

DISCRETE_TYPES = ("boolean", "status", "string", "event")
NS_PER_SECOND = 1_000_000_000
AUTO_ROWS = 8  # signals ticked by default
MAX_ROWS = 24
MAX_SEGMENTS = 500  # per row
MAX_EVENTS = 1000  # per row
SEGMENT_LABELS = 60  # per row
EVENT_LABELS = 40  # per event row; more events in view are drawn without labels
HOLD_PERIODS = 3  # automatic hold limit in nominal sample periods
BAR_BOTTOM = 0.35
BAR_HEIGHT = 0.55
SAMPLE_Y = 0.15
EVENT_Y = 0.5
CONFIG_KEYS = frozenset({"signals", "max_hold_s", "follow_main_plot"})


@dataclass(frozen=True)
class TimelineRow:
    """One drawn row: the signal and its timeline for the visible range."""

    descriptor: SignalDescriptor
    timeline: StateTimeline
    max_hold_ns: int | None


def is_discrete(descriptor: SignalDescriptor) -> bool:
    return descriptor.data_type in DISCRETE_TYPES and not descriptor.shape


class StateTimelineTool(QWidget):
    """Discrete signals of the loaded session as labeled state bars and event markers."""

    def __init__(self, context: AnalysisContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self.rows: list[TimelineRow] = []
        self.origin_ns = 0
        self.origin_text = "the session start"
        self.region: IntervalRegion | None = None
        self._chosen: list[str] | None = None  # None: the first discrete signals
        self._items: list[pg.GraphicsObject] = []
        self._session: object | None = None
        self._fitted = True
        self._syncing = False

        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.signals = QListWidget()
        self.signals.setMaximumHeight(110)
        self.signals.setToolTip("Tick the signals to show, by signal id.")
        self.signals.itemChanged.connect(self._ticked)
        self.hold = QDoubleSpinBox()
        self.hold.setRange(0.0, 86_400.0)
        self.hold.setDecimals(3)
        self.hold.setSuffix(" s")
        self.hold.setSpecialValueText("auto")
        self.hold.setToolTip(
            "Longest time a state is shown after its sample. auto: "
            f"{HOLD_PERIODS} sample periods for signals with a nominal rate, "
            "else until the next sample."
        )
        self.hold.valueChanged.connect(self._schedule)
        self.follow = QCheckBox("Follow main plot zoom")
        self.follow.setChecked(True)
        self.follow.toggled.connect(self._schedule)
        self.fit_button = QPushButton("Fit session")
        self.fit_button.clicked.connect(self.fit)

        self.plot_item = PlotItem()
        self.plot_widget = pg.PlotWidget(plotItem=self.plot_item)
        self.plot_widget.setMinimumSize(320, 160)
        self.plot_widget.showGrid(x=True, y=False, alpha=0.2)
        self.plot_widget.setMouseEnabled(x=True, y=False)
        self.plot_widget.setLabel("bottom", "time", units="s")
        self.plot_item.getAxis("left").setWidth(130)
        self.view = self.plot_item.getViewBox()
        self.view.disableAutoRange()
        self.view.sigXRangeChanged.connect(self._view_changed)
        context.style_plot(self.plot_widget)
        self.time_cursor = pg.InfiniteLine(
            angle=90, movable=False, pen=pg.mkPen(CURSOR_COLOR, width=2)
        )
        self.time_cursor.setZValue(20)
        self.plot_item.addItem(self.time_cursor, ignoreBounds=True)
        self.time_cursor.hide()

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Hold state at most"))
        controls.addWidget(self.hold)
        controls.addWidget(self.follow)
        controls.addStretch(1)
        controls.addWidget(self.fit_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.signals)
        layout.addLayout(controls)
        layout.addWidget(self.plot_widget, 1)
        layout.addWidget(self.hint)

        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.refresh)
        for change in (
            context.snapshot_changed,
            context.signal_changed,
            context.theme_changed,
        ):
            change.connect(self._schedule)
        context.cursor_changed.connect(self._place_cursor)
        main = self._main_plot()
        if main is not None:
            main.view.sigXRangeChanged.connect(self._main_range_changed)
        self.refresh()

    # Configuration

    def configuration(self) -> dict[str, object]:
        """The ticked signal ids (None: automatic), hold limit and zoom following."""

        hold = self.hold.value()
        return {
            "signals": None if self._chosen is None else list(self._chosen),
            "max_hold_s": hold if hold > 0 else None,
            "follow_main_plot": self.follow.isChecked(),
        }

    def restore_configuration(self, config: object) -> None:
        """Apply a configuration from :meth:`configuration`; invalid input raises ValueError."""

        if not isinstance(config, dict) or set(config) != CONFIG_KEYS:
            raise ValueError(f"State timeline configuration needs exactly {sorted(CONFIG_KEYS)}.")
        signals, hold, follow = config["signals"], config["max_hold_s"], config["follow_main_plot"]
        if signals is not None and (
            not isinstance(signals, list)
            or not all(isinstance(item, str) and item.strip() for item in signals)
        ):
            raise ValueError("signals must be None or a list of signal ids.")
        if hold is not None and (
            isinstance(hold, bool) or not isinstance(hold, (int, float)) or not 0 < hold <= 86_400
        ):
            raise ValueError("max_hold_s must be None or a positive number of seconds.")
        if not isinstance(follow, bool):
            raise ValueError("follow_main_plot must be true or false.")
        self._chosen = None if signals is None else list(dict.fromkeys(signals))
        with QSignalBlocker(self.hold), QSignalBlocker(self.follow):
            self.hold.setValue(0.0 if hold is None else float(hold))
            self.follow.setChecked(follow)
        self._update_checks()
        self.refresh()

    # Drawing

    def shown_ids(self) -> list[str]:
        """Ids of the ticked signals present in the session, in list order."""

        present = [self.context.descriptor(item) for item in self._chosen_ids()]
        return [item.identifier for item in present if item is not None][:MAX_ROWS]

    def fit(self) -> None:
        """Show the whole session (and zoom the main plot along when following it)."""

        self._fitted = True
        bounds = self.context.bounds
        if bounds is not None:
            self._set_x_range(*self._fit_range(bounds.start_ns, bounds.end_ns))
            main = self._linked_main()
            if main is not None:
                self._syncing = True
                try:
                    main.view.setXRange(*self.view.viewRange()[0], padding=0)
                finally:
                    self._syncing = False
        self.refresh()

    def refresh(self) -> None:
        self.timer.stop()
        context = self.context
        self._sync_list()
        self._clear_rows()
        bounds = context.bounds
        if context.snapshot is None or bounds is None:
            self._set_region(None)
            self._session = None
            self.hint.setText(
                "Load a session (CSV file or simulator) to see its boolean, status, "
                "string and event signals here."
            )
            self._place_cursor()
            return
        self._set_origin(*self._origin(bounds.start_ns))
        main = self._linked_main()
        if main is not None:
            self._set_x_range(*main.view.viewRange()[0])
        elif self._session is not context.session or self._fitted:
            self._fitted = True
            self._set_x_range(*self._fit_range(bounds.start_ns, bounds.end_ns))
        self._session = context.session
        self._place_cursor()

        ids = self.shown_ids()
        if not ids:
            self.hint.setText(self._empty_text())
            return
        low, high = self.view.viewRange()[0]
        start = self.origin_ns + floor(low * NS_PER_SECOND)
        end = max(start, self.origin_ns + ceil(high * NS_PER_SECOND))
        for row, signal_id in enumerate(ids):
            descriptor = context.descriptor(signal_id)
            assert descriptor is not None
            timeline_row = self._build_row(descriptor, start, end, bounds.start_ns, bounds.end_ns)
            self.rows.append(timeline_row)
            self._draw_row(timeline_row, len(ids) - 1 - row)
        left = self.plot_item.getAxis("left")
        left.setTicks(
            [[(len(ids) - 1 - row + EVENT_Y, signal_id) for row, signal_id in enumerate(ids)]]
        )
        self.view.setYRange(-0.1, len(ids) + 0.05, padding=0)
        self.hint.setText(self._summary(start, end))

    def _build_row(
        self, descriptor: SignalDescriptor, start: int, end: int, data_start: int, data_end: int
    ) -> TimelineRow:
        snapshot = self.context.snapshot
        assert snapshot is not None
        # One record on each side gives the state entering the view and the end of the last one.
        records = snapshot.range(descriptor.identifier, start, end, neighbors=True)
        hold_ns = self._hold_ns(descriptor)
        timeline = build_state_timeline(
            [record.sample.timestamp_ns for record in records],
            [
                category_value(record.sample.value) if record.sample.available else None
                for record in records
            ],
            max_hold_ns=hold_ns,
            data_start_ns=data_start,
            data_end_ns=data_end,
            window_start_ns=start,
            window_end_ns=end,
            max_segments=MAX_SEGMENTS,
            max_events=MAX_EVENTS,
        )
        return TimelineRow(descriptor, timeline, hold_ns)

    def _draw_row(self, row: TimelineRow, base: float) -> None:
        text = self.context.plot_colors[1]
        timeline, origin = row.timeline, self.origin_ns
        items: list[pg.GraphicsObject] = []
        if row.descriptor.data_type == "event":
            # Events are instants: markers with labels, no state held between them.
            markers = event_markers(timeline.events, origin, base + EVENT_Y, text, uniform=True)
            if markers is not None:
                items.append(markers)
            if len(timeline.events) <= EVENT_LABELS:
                for event in timeline.events:
                    label = pg.TextItem(event.label, color=text, anchor=(0, 1))
                    label.setPos((event.time_ns - origin) / NS_PER_SECOND, base + EVENT_Y + 0.1)
                    items.append(label)
        else:
            bars = segment_bars(timeline.segments, origin, base + BAR_BOTTOM, BAR_HEIGHT)
            if bars is not None:
                items.append(bars)
            items.extend(
                segment_labels(
                    timeline.segments,
                    origin,
                    base + BAR_BOTTOM + BAR_HEIGHT / 2,
                    SEGMENT_LABELS,
                    text,
                )
            )
            markers = event_markers(timeline.events, origin, base + SAMPLE_Y, text)
            if markers is not None:
                items.append(markers)
        for item in items:
            self.plot_item.addItem(item, ignoreBounds=True)
        self._items.extend(items)

    def _clear_rows(self) -> None:
        for item in self._items:
            self.plot_item.removeItem(item)
        self._items = []
        self.rows = []
        self.plot_item.getAxis("left").setTicks([[]])

    def _place_cursor(self) -> None:
        cursor = self.context.cursor_ns
        if cursor is None or self.context.snapshot is None:
            self.time_cursor.hide()
            return
        self.time_cursor.setValue((cursor - self.origin_ns) / NS_PER_SECOND)
        self.time_cursor.show()

    # Text

    def _summary(self, start: int, end: int) -> str:
        scale = PlotTimeScale.seconds(self.origin_ns)
        lines = [
            f"Time axis: seconds since {self.origin_text}. "
            f"View {scale.to_x(start):.3f} s to {scale.to_x(end):.3f} s.",
            "A state is shown from its sample until the next sample or its hold limit; "
            "hatched gray = no data, small ticks = samples, tall ticks = changes.",
        ]
        for row in self.rows:
            timeline, descriptor = row.timeline, row.descriptor
            if descriptor.data_type == "event":
                text = f"{descriptor.identifier} (event): {timeline.sample_count} events in view"
                if len(timeline.events) > EVENT_LABELS:
                    text += ", zoom in for labels"
            else:
                hold = (
                    "holds until next sample"
                    if row.max_hold_ns is None
                    else f"holds at most {row.max_hold_ns / NS_PER_SECOND:g} s"
                )
                text = (
                    f"{descriptor.identifier} ({descriptor.data_type}): "
                    f"{timeline.sample_count} samples, {timeline.transition_count} changes, {hold}"
                )
                if not is_discrete(descriptor):
                    text += "; only whole numbers and text are categories"
            if timeline.segments_bounded or timeline.events_bounded:
                text += "; short spans combined to fit the view"
            lines.append(text)
        missing = [item for item in self._chosen_ids() if self.context.descriptor(item) is None]
        if missing:
            lines.append("Not in this session: " + ", ".join(missing[:5]))
        if len(self.shown_ids()) < len(self._chosen_ids()) - len(missing):
            lines.append(f"Showing the first {MAX_ROWS} ticked signals.")
        return "\n".join(lines)

    def _empty_text(self) -> str:
        if not any(is_discrete(item) for item in self.context.descriptors):
            return (
                "This session has no boolean, status, string or event signals. "
                "Tick any signal above to show its whole-number or text values as states."
            )
        return "Tick boolean, status, string or event signals above to show them."

    # Signal list

    def _chosen_ids(self) -> list[str]:
        if self._chosen is not None:
            return self._chosen
        discrete = [item.identifier for item in self.context.descriptors if is_discrete(item)]
        return discrete[:AUTO_ROWS]

    def _sync_list(self) -> None:
        descriptors = sorted(self.context.descriptors, key=lambda item: not is_discrete(item))
        ids = [item.identifier for item in descriptors]
        current = [
            self.signals.item(row).data(Qt.ItemDataRole.UserRole)
            for row in range(self.signals.count())
        ]
        if ids == current:
            self._update_checks()
            return
        with QSignalBlocker(self.signals):
            self.signals.clear()
            for descriptor in descriptors:
                kind = descriptor.data_type + (
                    f" {list(descriptor.shape)}" if descriptor.shape else ""
                )
                item = QListWidgetItem(f"{descriptor.identifier}  ({kind})")
                item.setData(Qt.ItemDataRole.UserRole, descriptor.identifier)
                item.setToolTip(descriptor.display_name)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                self.signals.addItem(item)
        self._update_checks()

    def _update_checks(self) -> None:
        chosen = set(self._chosen_ids())
        with QSignalBlocker(self.signals):
            for row in range(self.signals.count()):
                item = self.signals.item(row)
                ticked = item.data(Qt.ItemDataRole.UserRole) in chosen
                item.setCheckState(Qt.CheckState.Checked if ticked else Qt.CheckState.Unchecked)

    def _ticked(self, _item: QListWidgetItem) -> None:
        listed = []
        for row in range(self.signals.count()):
            item = self.signals.item(row)
            if item.checkState() == Qt.CheckState.Checked:
                listed.append(item.data(Qt.ItemDataRole.UserRole))
        present = {item.identifier for item in self.context.descriptors}
        # Keep ids from a restored configuration that this session does not have.
        self._chosen = listed + [item for item in self._chosen_ids() if item not in present]
        self._schedule()

    # Time axis

    def _main_plot(self) -> TimePlot | None:
        panel = getattr(self.context.parent(), "right_panel", None)
        plot = getattr(panel, "time_plot", None)
        return plot if isinstance(plot, TimePlot) else None

    def _origin(self, session_start: int) -> tuple[int, str]:
        main = self._main_plot()
        if (
            main is not None
            and main.identity is not None
            and main.identity[0] is self.context.session
        ):
            signal_id = main.identity[1]
            return main.origin_ns, f"the first reading of {signal_id}, as in the main plot"
        return session_start, "the session start"

    def _linked_main(self) -> TimePlot | None:
        main = self._main_plot()
        if (
            self.follow.isChecked()
            and main is not None
            and main.identity is not None
            and main.identity[0] is self.context.session
            and main.origin_ns == self.origin_ns
        ):
            return main
        return None

    def _set_origin(self, origin_ns: int, text: str) -> None:
        self.origin_text = text
        if origin_ns != self.origin_ns:
            shift = (self.origin_ns - origin_ns) / NS_PER_SECOND
            low, high = self.view.viewRange()[0]
            self.origin_ns = origin_ns
            self._set_x_range(low + shift, high + shift)
            self._set_region(None)
        if self.region is None:
            self._set_region(origin_ns)

    def _set_region(self, origin_ns: int | None) -> None:
        if self.region is not None:
            self.region.detach()
            self.region = None
        if origin_ns is not None:
            self.region = IntervalRegion(
                self.plot_widget, self.context.selection, PlotTimeScale.seconds(origin_ns)
            )

    def _fit_range(self, start: int, end: int) -> tuple[float, float]:
        low = (start - self.origin_ns) / NS_PER_SECOND
        high = max((end - self.origin_ns) / NS_PER_SECOND, low + 0.001)
        pad = (high - low) * 0.02
        return low - pad, high + pad

    def _set_x_range(self, low: float, high: float) -> None:
        self._syncing = True
        try:
            self.view.setXRange(low, high, padding=0)
        finally:
            self._syncing = False

    def _view_changed(self) -> None:
        if self._syncing:
            return  # Set by refresh or by following the main plot.
        self._fitted = False
        main = self._linked_main()
        if main is not None:
            self._syncing = True
            try:
                main.view.setXRange(*self.view.viewRange()[0], padding=0)
            finally:
                self._syncing = False
        self._schedule()

    def _main_range_changed(self) -> None:
        if self._syncing or self._linked_main() is None:
            return
        self._schedule()

    def _schedule(self) -> None:
        if not self.timer.isActive():
            self.timer.start()

    def _hold_ns(self, descriptor: SignalDescriptor) -> int | None:
        hold = self.hold.value()
        if hold > 0:
            return max(1, round(hold * NS_PER_SECOND))
        rate = descriptor.nominal_rate_hz
        if rate is not None and rate > 0:
            return max(1, round(HOLD_PERIODS * NS_PER_SECOND / rate))
        return None


TOOL = AnalysisTool("state_timeline", "State timeline", StateTimelineTool)
