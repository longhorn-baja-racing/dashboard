"""Thin application binding between SessionClock, the transport bar and plot cursor."""

from __future__ import annotations

from collections.abc import Callable
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

from .core import PlaybackMode, SessionClock, SessionSnapshot, TelemetrySession
from .ui.main_window import MainWindow
from .ui.replay_bar import SLIDER_STEPS


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
        # The plot uses time relative to the selected signal's first retained record.
        frame = self.clock.snapshot()
        view = self._snapshot
        selected = self.window.left_panel.signal_browser.selected_id
        records = view.series.get(selected or "", ()) if view else ()
        descriptor = (
            next((d for d in view.schema.signals if d.identifier == selected), None)
            if view
            else None
        )
        plot = self.window.right_panel.plot_widget.getPlotItem()
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
        if records and aligned and frame.selected_ns is not None and plot is not None:
            if self.cursor is None:
                self.cursor = pg.InfiniteLine(angle=90, pen=pg.mkPen("#ffcc00", width=2))
                plot.addItem(self.cursor, ignoreBounds=True)
            self.cursor.setValue((frame.selected_ns - records[0].sample.timestamp_ns) / 1e9)
            self.cursor.show()
        elif self.cursor is not None:
            self.cursor.hide()

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
