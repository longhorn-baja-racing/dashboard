"""Two Qt presentations consume one app-owned clock without their own timebases."""

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from lbr_dashboard.core import (
    SessionClock,
    SignalDescriptor,
    SignalSample,
    SignalSchema,
    TelemetrySession,
)


def test_one_owner_pump_delivers_identical_frames_to_two_native_widgets(qtbot) -> None:
    model = SignalDescriptor(
        "custom.pressure",
        "Pressure",
        ("custom",),
        "numeric",
        "demo",
        canonical_unit="Pa",
        timestamp_domain="relative",
    )
    store = TelemetrySession(SignalSchema(1, (model,)))
    store.extend(
        model.identifier,
        [
            SignalSample(0, 0, timestamp_domain="relative", unit="Pa"),
            SignalSample(
                1_000_000_000, None, available=False, timestamp_domain="relative", unit="Pa"
            ),
            SignalSample(2_000_000_000, 8, timestamp_domain="relative", unit="Pa"),
        ],
    )
    fake_time = [0]
    clock = SessionClock(now_ns=lambda: fake_time[0])
    clock.update_from_session(store.snapshot(), model.identifier)
    window = QWidget()
    layout = QVBoxLayout(window)
    first, second = QLabel(), QLabel()
    layout.addWidget(first)
    layout.addWidget(second)
    qtbot.addWidget(window)
    window.show()
    frames_a, frames_b = [], []

    def show_a(frame):
        frames_a.append(frame)
        first.setText(str(frame.selected_ns))

    def show_b(frame):
        frames_b.append(frame)
        second.setText(str(frame.selected_ns))

    unsubscribe_a = clock.subscribe(show_a)
    unsubscribe_b = clock.subscribe(show_b)
    timer = QTimer(window)
    ticks = []

    def pump():
        ticks.append(1)
        fake_time[0] += 100_000_000
        clock.tick()

    timer.timeout.connect(pump)
    clock.play()
    timer.start(1)
    try:
        qtbot.waitUntil(lambda: len(ticks) >= 3)
        timer.stop()
        clock.pause()
        assert first.text() == second.text() == str(clock.snapshot().selected_ns)
        assert all(a is b for a, b in zip(frames_a, frames_b))
        selected = clock.seek(1_000_000_000).selected_ns
        assert selected is not None
        missing = store.nearest(model.identifier, selected, max_distance_ns=0)
        assert missing is not None and not missing.sample.available
        selected = clock.seek(1_500_000_000).selected_ns
        assert selected is not None
        assert store.nearest(model.identifier, selected, max_distance_ns=100_000_000) is None
        assert first.text() == second.text() == "1500000000"
        clock.follow_live()
        assert first.text() == second.text() == "2000000000"
        clock.set_connected(False)
        store.append(model.identifier, SignalSample(3_000_000_000, 9, timestamp_domain="relative"))
        clock.update_from_session(store.snapshot(), model.identifier)
        assert first.text() == second.text() == "2000000000"
        clock.set_connected(True)
        assert first.text() == second.text() == "3000000000"
    finally:
        timer.stop()
        unsubscribe_a()
        unsubscribe_b()
        window.close()
