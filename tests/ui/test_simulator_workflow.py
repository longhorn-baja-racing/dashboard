"""Production simulator/source/session/clock in an owner-driven native Qt flow."""

import pytest
from PySide6.QtCore import QTimer

from lbr_dashboard.app import create_main_window
from lbr_dashboard.core import SessionClock, SimulatorSource, TelemetrySession, example_signals


@pytest.mark.parametrize("family", ["process", "motion"])
def test_simulator_populates_app_without_sensor_specific_widget_changes(qtbot, family) -> None:
    source = SimulatorSource(
        example_signals(family), seed=17, duration_ns=1_000_000_000, capacity=256
    )
    store = TelemetrySession(source.schema, max_samples_per_signal=200)
    reference = f"{family}.pressure" if family == "process" else "motion.speed"
    clock = SessionClock(now_ns=lambda: 0)
    clock.follow_live()
    first, second = [], []
    off_a, off_b = clock.subscribe(first.append), clock.subscribe(second.append)
    window = create_main_window()
    qtbot.addWidget(window)
    window.show()
    timer = QTimer(window)
    ticks = []

    def poll():
        ticks.append(1)
        store.ingest_source(source)
        view = store.snapshot()
        clock.update_from_session(view, reference)
        points = view.series[reference]
        window.plot_column(
            reference,
            tuple(item.sample.value for item in points),
            tuple(item.sample.timestamp_ns / 1_000_000 for item in points),
        )

    timer.timeout.connect(poll)
    source.open()
    source.start()
    timer.start(1)
    try:
        qtbot.waitUntil(
            lambda: source.join(0) and len(ticks) >= 2 and store.bounds(reference) is not None
        )
        timer.stop()
        view = store.snapshot()
        assert view.source_health[source.source_id].terminal_reason == "complete"
        assert first[-1] is second[-1] is clock.snapshot()
        assert clock.snapshot().selected_ns == 1_000_000_000
        plot = window.right_panel.plot_widget.getPlotItem()
        assert plot is not None
        _, ys = plot.listDataItems()[0].getData()
        if family == "process":
            assert len(view.series["process.pressure"]) == 91
            assert len(view.series["process.temperature"]) == 2
            assert len(ys) == 86 and 0 in ys
            assert view.nearest(reference, 220_000_000, max_distance_ns=0) is None
            missing = view.nearest(reference, 500_000_000, max_distance_ns=0)
            assert missing is not None and not missing.sample.available
            assert source.simulator_stats.stale == 5
        else:
            assert len(view.series["motion.vector"]) == 11
            assert view.series["motion.enabled"][0].sample.value is False
            assert view.series["motion.notice"][0].sample.value == {"code": "tick"}
            assert len(ys) == 4
    finally:
        timer.stop()
        off_a()
        off_b()
        assert source.close(1)
        window.close()
