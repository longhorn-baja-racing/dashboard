"""Replay presentation and requested actions; time is owned by SessionClock."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
)

from ..core import ClockSnapshot, PlaybackMode

SLIDER_STEPS = 10000


def seconds(nanoseconds: int) -> str:
    whole, fraction = divmod(nanoseconds, 1_000_000_000)
    return f"{whole}.{fraction:09d} s"


class ReplayBar(QGroupBox):
    toggle_requested = Signal()
    seek_requested = Signal(int)
    pause_requested = Signal()
    speed_requested = Signal(float)
    live_requested = Signal()

    def __init__(self) -> None:
        super().__init__("Session replay")
        self.play = QPushButton("Play")
        self.live = QPushButton("Follow live")
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, SLIDER_STEPS)
        self.slider.setAccessibleName("Replay selected time")
        self.speed = QComboBox()
        self.speed.setAccessibleName("Replay speed")
        for rate in (0.25, 0.5, 1, 2, 4):
            self.speed.addItem(f"{rate:g}x", rate)
        self.speed.setCurrentIndex(2)
        self.time = QLabel("No replay data")
        self.time.setTextFormat(Qt.TextFormat.PlainText)
        self.time.setWordWrap(True)
        row = QHBoxLayout()
        for widget in (self.play, self.slider, self.speed, self.live):
            row.addWidget(widget)
        layout = QVBoxLayout(self)
        layout.addLayout(row)
        layout.addWidget(self.time)
        self.play.clicked.connect(self.toggle_requested)
        self.live.clicked.connect(self.live_requested)
        self.slider.valueChanged.connect(self.seek_requested)
        self.slider.sliderPressed.connect(self.pause_requested)
        self.speed.currentIndexChanged.connect(
            lambda: self.speed_requested.emit(self.speed.currentData())
        )
        for widget in (self.play, self.live, self.slider, self.speed):
            widget.setEnabled(False)

    def display(self, frame: ClockSnapshot, *, ready: bool, live: bool, busy: bool) -> None:
        populated = frame.selected_ns is not None and frame.start_ns is not None
        selected, start = frame.selected_ns or 0, frame.start_ns or 0
        span = (frame.end_ns or 0) - (frame.start_ns or 0)
        enabled = ready and populated and not busy
        self.play.setEnabled(enabled and (live and frame.connected or not live and span > 0))
        self.slider.setEnabled(enabled and not live and span > 0)
        self.speed.setEnabled(enabled and not live and span > 0)
        self.live.setEnabled(enabled and live and frame.connected)
        for widget in (self.play, self.slider, self.speed, self.live):
            widget.setToolTip(
                "Space: play/pause; Left/Right: seek 1 s; Home/End: start/end. "
                "Shortcuts leave text fields and navigation widgets alone."
                if widget.isEnabled()
                else "Finite replay requires loaded data. Live supports pause/follow only."
            )
        self.play.setText(
            "Pause"
            if frame.mode in {PlaybackMode.PLAYING, PlaybackMode.LIVE}
            else "Follow live"
            if live
            else "Replay"
            if frame.mode is PlaybackMode.ENDED
            else "Play"
        )
        if not self.slider.isSliderDown():
            self.slider.blockSignals(True)
            self.slider.setValue(
                (((selected - start) * SLIDER_STEPS + span // 2) // span)
                if populated and span > 0
                else 0
            )
            self.slider.blockSignals(False)
        self.speed.blockSignals(True)
        self.speed.setCurrentIndex(self.speed.findData(float(frame.speed)))
        self.speed.blockSignals(False)
        if not populated:
            self.time.setText("No replay data - connect a CSV or simulator source.")
        else:
            elapsed = selected - start
            reference = frame.reference[0] if frame.reference else "unbound"
            state = "Disconnected live input" if live and not frame.connected else frame.mode.value
            if busy or not ready:
                state = "Loading / cleanup - replay controls unavailable"
            self.time.setText(
                f"{state} | {seconds(elapsed)} / {seconds(span)} | "
                f"Original time: {frame.selected_ns} ns | timeline: {reference}"
            )
