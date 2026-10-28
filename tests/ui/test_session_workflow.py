"""A native Qt owner consumes the real source and queries bounded session data."""

from PySide6.QtCore import QTimer

from lbr_dashboard.app import create_main_window
from lbr_dashboard.core import (
    IteratorSource,
    SignalDescriptor,
    SignalSample,
    SignalSchema,
    SourceEvent,
    SourceEventKind,
    TelemetrySession,
)


def test_real_source_session_queries_and_frozen_view_in_responsive_ui(qtbot) -> None:
    pressure = SignalDescriptor(
        "bench.pressure",
        "Pressure",
        ("bench",),
        "numeric",
        "demo",
        canonical_unit="Pa",
        timestamp_domain="relative",
    )
    slow = SignalDescriptor(
        "lab.temperature",
        "Temperature",
        ("lab",),
        "numeric",
        "demo",
        canonical_unit="°C",
        timestamp_domain="relative",
    )
    schema = SignalSchema(1, (pressure, slow))

    def generate(token):
        for i in range(101):
            token.raise_if_cancelled()
            value = None if i == 90 else 0 if i == 75 else i
            yield SourceEvent(
                SourceEventKind.RECORD,
                "demo",
                signal_id=pressure.identifier,
                sample=SignalSample(
                    i * 10_000_000,
                    value,
                    available=value is not None,
                    timestamp_domain="relative",
                    source_id="demo",
                    unit="Pa",
                ),
            )
            if i % 100 == 0:
                yield SourceEvent(
                    SourceEventKind.RECORD,
                    "demo",
                    signal_id=slow.identifier,
                    sample=SignalSample(
                        i * 10_000_000,
                        20 + i,
                        timestamp_domain="relative",
                        source_id="demo",
                        unit="°C",
                    ),
                )

    source = IteratorSource("demo", schema, generate, is_live=True, capacity=128)
    store = TelemetrySession(schema, max_samples_per_signal=32)
    window = create_main_window()
    qtbot.addWidget(window)
    window.show()
    timer = QTimer(window)
    ticks = []

    def poll():
        ticks.append(1)
        store.ingest_source(source)
        view = store.snapshot()
        records = view.range(pressure.identifier, 0, 1_000_000_000)
        window.plot_column(
            "Pressure [Pa]",
            tuple(item.sample.value for item in records),
            tuple(item.sample.timestamp_ns / 1_000_000 for item in records),
        )

    timer.timeout.connect(poll)
    source.open()
    source.start()
    timer.start(1)
    try:
        qtbot.waitUntil(
            lambda: (
                source.join(0)
                and len(ticks) >= 2
                and len(store.snapshot().series[pressure.identifier]) == 32
            )
        )
        frozen = store.snapshot()
        assert len(frozen.series[slow.identifier]) == 2
        assert frozen.evicted[pressure.identifier] == 69
        missing = frozen.nearest(pressure.identifier, 900_000_000)
        assert missing is not None and missing.sample.value is None
        assert frozen.nearest(slow.identifier, 500_000_000, max_distance_ns=100_000_000) is None
        plot = window.right_panel.plot_widget.getPlotItem()
        assert plot is not None
        xs, ys = plot.listDataItems()[0].getData()
        assert len(xs) == 31 and 0 in ys and 90 not in ys
        timer.stop()
        store.append(
            pressure.identifier,
            SignalSample(
                1_100_000_000, 999, timestamp_domain="relative", source_id="demo", unit="Pa"
            ),
        )
        assert frozen.bounds(pressure.identifier) == (690_000_000, 1_000_000_000)
        assert store.bounds(pressure.identifier) == (700_000_000, 1_100_000_000)
        assert frozen.source_health["demo"].terminal_reason == "complete"
    finally:
        timer.stop()
        assert source.close(1)
        window.close()
