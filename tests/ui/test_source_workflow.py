"""Application-owned polling keeps source work away from the Qt thread."""

from threading import Event, get_ident

from PySide6.QtCore import QTimer

from lbr_dashboard.app import create_main_window
from lbr_dashboard.core import (
    IteratorSource,
    SignalDescriptor,
    SignalSample,
    SignalSchema,
    SourceEvent,
    SourceEventKind,
)


def test_real_source_handoff_plot_and_cancellation_leave_qt_responsive(qtbot) -> None:
    owner_thread = get_ident()
    producer_threads = []
    cleaned, waiting = Event(), Event()
    model = SignalDescriptor(
        "custom.pressure",
        "Pressure",
        ("custom",),
        "numeric",
        "live",
        canonical_unit="Pa",
        timestamp_domain="device_monotonic",
    )

    def generate(token):
        producer_threads.append(get_ident())
        try:
            for i, value in enumerate((5, 0, None, 8)):
                yield SourceEvent(
                    SourceEventKind.RECORD,
                    "live",
                    signal_id=model.identifier,
                    sample=SignalSample(
                        i * 10_000_000,
                        value,
                        available=value is not None,
                        source_id="live",
                        unit="Pa",
                        timestamp_domain="device_monotonic",
                    ),
                )
            waiting.set()
            token.wait(2)
            token.raise_if_cancelled()
        finally:
            cleaned.set()

    source = IteratorSource("live", SignalSchema(1, (model,)), generate, is_live=True, capacity=16)
    window = create_main_window()
    qtbot.addWidget(window)
    window.show()
    received, heartbeats = [], []
    timer = QTimer(window)

    def poll():
        assert get_ident() == owner_thread
        heartbeats.append(1)
        received.extend(
            event.sample for event in source.events.drain() if event.kind is SourceEventKind.RECORD
        )
        if received:
            window.plot_column(
                model.display_name,
                tuple(item.value for item in received),
                tuple(item.timestamp_ns / 1_000_000 for item in received),
            )

    timer.timeout.connect(poll)
    source.open()
    source.start()
    timer.start(1)
    try:
        qtbot.waitUntil(lambda: waiting.is_set() and len(received) == 4 and len(heartbeats) >= 2)
        assert producer_threads and producer_threads[0] != owner_thread
        plot = window.right_panel.plot_widget.getPlotItem()
        assert plot is not None
        curve = plot.listDataItems()[0]
        xs, ys = curve.getData()
        assert list(xs) == [0, 10, 30]
        assert list(ys) == [5, 0, 8]
        assert received[2].value is None and not received[2].available
        assert all(item.source_id == "live" and item.unit == "Pa" for item in received)
        source.stop()
        qtbot.waitUntil(lambda: source.join(0))
        assert cleaned.is_set() and source.close(0)
        assert source.health().terminal_reason == "cancelled"
    finally:
        timer.stop()
        assert source.close(1)
        window.close()
