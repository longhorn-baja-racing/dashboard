"""Thin application binding between SessionClock, the transport bar and plot cursor."""

from __future__ import annotations

from collections.abc import Callable
from math import isfinite
from time import monotonic_ns

import pyqtgraph as pg
from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QLineEdit,
    QPlainTextEdit,
    QTextEdit,
    QWidget,
)

from .core import (
    ClockSnapshot,
    PlaybackMode,
    SessionClock,
    SessionSnapshot,
    SignalDescriptor,
    TelemetrySession,
)
from .ui.main_window import MainWindow
from .ui.plot_style import CURSOR_COLOR
from .ui.replay_bar import SLIDER_STEPS


def reading_tolerance_ns(view: SessionSnapshot, descriptor: SignalDescriptor) -> int:
    """Half a sample period: the declared cadence, or else the recorded one.

    Without a declared rate (CSV columns, for example) the mean spacing of the
    recorded samples stands in for it, so a reading shows between samples but
    not in the middle of a gap several periods long.
    """
    rate = descriptor.nominal_rate_hz
    if rate:
        return int(min(500_000_000 / rate, 2**63 - 1))
    records = view.series.get(descriptor.identifier, ())
    bounds = view.bounds(descriptor.identifier) if records else None
    if bounds is None or len(records) < 2:
        return 0
    return (bounds[1] - bounds[0]) // (2 * (len(records) - 1))


def selected_reading(view: SessionSnapshot, descriptor: SignalDescriptor, selected_ns: int) -> str:
    tolerance = reading_tolerance_ns(view, descriptor)
    sample = view.nearest(descriptor.identifier, selected_ns, max_distance_ns=tolerance)
    if sample is None or not sample.sample.available:
        return "Selected: no available reading"
    reading = sample.sample
    unit = reading.unit or descriptor.canonical_unit
    return f"Selected: {reading.value}{' ' + unit if unit else ''} at {reading.timestamp_ns} ns"


class ReplayController(QObject):
    def __init__(self, window: MainWindow, *, now_ns: Callable[[], int] = monotonic_ns) -> None:
        super().__init__(window)
        self.window = window
        self._now = now_ns
        self.clock = SessionClock(now_ns=now_ns)
        self._session: TelemetrySession | None = None
        self._snapshot: SessionSnapshot | None = None
        self._live = False
        self._ready = self._busy = False
        self._reference: str | None = None
        self.cursor: pg.InfiniteLine | None = None
        bar = window.replay_bar
        bar.toggle_requested.connect(self.toggle)
        bar.seek_requested.connect(self.seek_slider)
        bar.pause_requested.connect(self.pause)
        bar.speed_requested.connect(self.speed)
        bar.live_requested.connect(self.follow)
        window.analysis.seek_requested.connect(self.seek)
        application = QApplication.instance()
        assert application is not None
        application.installEventFilter(self)

    def update(
        self,
        session: TelemetrySession | None,
        *,
        snapshot: SessionSnapshot | None = None,
        live: bool | None = None,
        ready: bool,
        connected: bool,
        busy: bool,
    ) -> None:
        if session is not self._session:
            self._session = session
            self._snapshot = None
            self._reference = None
            self.clock = SessionClock(now_ns=self._now)
            self._live = bool(live)
        if snapshot is not None:
            self._snapshot = snapshot
        self._ready, self._busy = ready, busy
        self.clock.set_connected(connected)
        view = self._snapshot
        if view is not None:
            if self._reference is None:
                self._reference = next(
                    (d.identifier for d in view.schema.signals if view.series[d.identifier]), None
                )
                if self._reference is not None and self._live:
                    self.clock.follow_live()
            if self._reference is not None:
                self.clock.update_from_session(view, self._reference)
        if busy or not ready:
            self.clock.pause()
        self.clock.tick()
        self.present()

    def toggle(self) -> None:
        if not self.window.replay_bar.play.isEnabled():
            return
        frame = self.clock.snapshot()
        if frame.mode in {PlaybackMode.PLAYING, PlaybackMode.LIVE}:
            self.clock.pause()
        elif self._live:
            self.clock.follow_live()
        else:
            if frame.selected_ns == frame.end_ns:
                assert frame.start_ns is not None
                self.clock.seek(frame.start_ns)
            self.clock.play()
        self.present()

    def pause(self) -> None:
        self.clock.pause()
        self.present()

    def set_busy(self) -> None:
        self._busy = True
        self.pause()

    def seek_slider(self, position: int) -> None:
        if not self.window.replay_bar.slider.isEnabled():
            return
        frame = self.clock.snapshot()
        assert frame.start_ns is not None and frame.end_ns is not None
        self.clock.seek(frame.start_ns + (frame.end_ns - frame.start_ns) * position // SLIDER_STEPS)
        self.present()

    def seek(self, time_ns: int) -> bool:
        """Pause and move the cursor to ``time_ns``, clamped to the session.

        Works whenever the replay slider does; returns whether the cursor moved.
        """

        if not self.window.replay_bar.slider.isEnabled():
            return False
        self.clock.seek(time_ns)
        self.present()
        return True

    def speed(self, rate: float) -> None:
        if self.window.replay_bar.speed.isEnabled():
            self.clock.set_speed(rate)
            self.present()

    def follow(self) -> None:
        if self.window.replay_bar.live.isEnabled():
            self.clock.follow_live()
            self.present()

    def present(self) -> None:
        self.window.replay_bar.display(
            self.clock.snapshot(), ready=self._ready, live=self._live, busy=self._busy
        )
        # The plot keeps its origin stable while the live retention window moves.
        frame = self.clock.snapshot()
        view = self._snapshot
        self.window.analysis.set_cursor(frame.selected_ns if view is not None else None)
        selected = self.window.left_panel.signal_browser.selected_id
        records = view.series.get(selected or "", ()) if view else ()
        descriptor = (
            next((d for d in view.schema.signals if d.identifier == selected), None)
            if view
            else None
        )
        panel = self.window.right_panel
        readout = panel.selected_reading
        plot = panel.plot_widget.getPlotItem()
        if self.cursor is not None and self.cursor.scene() is None:
            self.cursor = None
        aligned = (
            frame.reference is not None
            and descriptor is not None
            and descriptor.data_type in {"numeric", "boolean"}
            and not descriptor.shape
            and (
                descriptor.source_id,
                view.domains.get(descriptor.identifier, descriptor.timestamp_domain)
                if view is not None
                else descriptor.timestamp_domain,
            )
            == frame.reference[1:]
        )
        if (
            records
            and aligned
            and frame.selected_ns is not None
            and plot is not None
            and panel.time_plot.identity == (self._session, selected)
        ):
            if self.cursor is None:
                self.cursor = pg.InfiniteLine(angle=90, pen=pg.mkPen(CURSOR_COLOR, width=2))
                self.cursor.sigPositionChangeFinished.connect(self._cursor_dragged)
                plot.addItem(self.cursor, ignoreBounds=True)
            self.cursor.setMovable(not self._live and self._ready and not self._busy)
            if not self.cursor.moving:
                self.cursor.setValue((frame.selected_ns - panel.time_plot.origin_ns) / 1e9)
            self.cursor.show()
            assert view is not None and descriptor is not None
            readout.setText(selected_reading(view, descriptor, frame.selected_ns))
        else:
            if self.cursor is not None:
                self.cursor.hide()
            readout.setText("Selected: no aligned reading")
        self._present_docks(frame, view)

    def _present_docks(self, frame: ClockSnapshot, view: SessionSnapshot | None) -> None:
        workspace = self.window.workspace_docks
        for dock in workspace.docks.values():
            if dock.cursor is not None and dock.cursor.scene() is None:
                dock.cursor = None
            descriptor = workspace.descriptor(dock.binding[0]) if dock.binding else None
            if (
                view is None
                or frame.reference is None
                or frame.selected_ns is None
                or descriptor is None
                or descriptor.source_id != frame.reference[1]
                or dock.binding != (descriptor.identifier, frame.reference[1])
                or descriptor.identifier not in view.series
                or view.domains.get(descriptor.identifier, descriptor.timestamp_domain)
                != frame.reference[2]
            ):
                if dock.value_widget is not None:
                    if dock.presenting_replay_value:
                        dock.render()
                    continue
                dock.selected_reading.setText("Selected: no aligned reading")
                if dock.cursor is not None:
                    dock.cursor.hide()
                continue

            if dock.value_widget is not None:
                record = view.at_or_before(descriptor.identifier, frame.selected_ns)
                half_period = reading_tolerance_ns(view, descriptor)
                dock.present_value(
                    descriptor,
                    record.sample if record is not None else None,
                    selected_ns=frame.selected_ns,
                    max_age_ns=2 * half_period if half_period else None,
                )
                continue

            bindings = dock.multi_plot.bindings if dock.multi_plot is not None else {}
            if len(bindings) > 1:
                readings = []
                for binding in bindings.values():
                    item = workspace.descriptor(binding.signal_id)
                    if item is None or item.source_id != binding.source_id:
                        readings.append(f"{binding.signal_id} — missing signal")
                    elif (
                        item.source_id != frame.reference[1]
                        or view.domains.get(item.identifier, item.timestamp_domain)
                        != frame.reference[2]
                    ):
                        readings.append(f"{binding.signal_id} — no aligned reading")
                    else:
                        readings.append(
                            f"{item.display_name} [{binding.unit or 'unit unspecified'}] — "
                            + selected_reading(view, item, frame.selected_ns)
                        )
                dock.selected_reading.setText("\n".join(readings))
            else:
                dock.selected_reading.setText(selected_reading(view, descriptor, frame.selected_ns))
            if dock.plot is None or dock.origin_ns is None:
                continue
            plot = dock.plot.getPlotItem()
            if plot is None:
                continue
            if dock.cursor is None:
                dock.cursor = pg.InfiniteLine(angle=90, pen=pg.mkPen(CURSOR_COLOR, width=2))
                dock.cursor.sigPositionChangeFinished.connect(self._cursor_dragged)
                plot.addItem(dock.cursor, ignoreBounds=True)
            dock.cursor.setMovable(not self._live and self._ready and not self._busy)
            if not dock.cursor.moving:
                dock.cursor.setValue((frame.selected_ns - dock.origin_ns) / 1e9)
            dock.cursor.show()

    def _cursor_dragged(self, cursor: pg.InfiniteLine) -> None:
        if self._live or self._busy or not self._ready or not cursor.isVisible():
            return
        panel = self.window.right_panel
        if cursor is self.cursor:
            origin = panel.time_plot.origin_ns
        else:
            origin = next(
                (
                    dock.origin_ns
                    for dock in self.window.workspace_docks.docks.values()
                    if dock.cursor is cursor
                ),
                None,
            )
            if origin is None:
                return
        frame = self.clock.snapshot()
        position = cursor.value()
        if (
            frame.start_ns is None
            or frame.end_ns is None
            or not isinstance(position, (int, float))
            or not isfinite(position)
        ):
            self.present()
            return
        low = (frame.start_ns - origin) / 1e9
        high = (frame.end_ns - origin) / 1e9
        target = (
            frame.start_ns
            if position <= low
            else frame.end_ns
            if position >= high
            else (origin + round(position * 1e9))
        )
        self.clock.seek(target)
        self.present()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() != QEvent.Type.KeyPress or not isinstance(event, QKeyEvent):
            return False
        if not isinstance(watched, QWidget) or watched.window() is not self.window:
            return False
        focus = QApplication.focusWidget()
        if (
            isinstance(
                focus,
                (
                    QLineEdit,
                    QTextEdit,
                    QPlainTextEdit,
                    QAbstractSpinBox,
                    QComboBox,
                    QAbstractItemView,
                    QAbstractButton,
                ),
            )
            or focus is self.window.replay_bar.slider
        ):
            return False
        if event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier:
            return False
        if event.key() == Qt.Key.Key_Space and self.window.replay_bar.play.isEnabled():
            if not event.isAutoRepeat():
                self.toggle()
            return True
        if not self.window.replay_bar.slider.isEnabled():
            return False
        frame = self.clock.snapshot()
        assert (
            frame.start_ns is not None
            and frame.end_ns is not None
            and frame.selected_ns is not None
        )
        targets: dict[int, int] = {
            Qt.Key.Key_Home: frame.start_ns,
            Qt.Key.Key_End: frame.end_ns,
            Qt.Key.Key_Left: frame.selected_ns - 1_000_000_000,
            Qt.Key.Key_Right: frame.selected_ns + 1_000_000_000,
        }
        target = targets.get(event.key())
        if target is None:
            return False
        self.clock.seek(target)
        self.present()
        return True
