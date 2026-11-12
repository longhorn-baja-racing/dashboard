"""Verify the production plot with irregular, missing and densely sampled data."""

import math
from itertools import islice
from pathlib import Path

import pytest

from lbr_dashboard.app import create_main_window
from lbr_dashboard.core import SignalDescriptor, SignalSample, SignalSchema, TelemetrySession
from lbr_dashboard.source_controller import SourceController
from lbr_dashboard.ui.right_panel.right_panel import RightPanel
from lbr_dashboard.ui.time_plot import POINT_LIMIT


def session_for(samples):
    descriptor = SignalDescriptor("lab.value", "Bench value", ("lab",), "numeric", "bench")
    session = TelemetrySession(SignalSchema(1, (descriptor,)))
    stream = iter(samples)
    while batch := tuple(islice(stream, 5000)):
        session.extend(descriptor.identifier, batch)
    return session, descriptor


def plot_session(panel, session, descriptor, query_log):
    view = session.snapshot()
    bounds = view.bounds(descriptor.identifier)
    assert bounds is not None

    def query(low, high):
        query_log.append((low, high))
        return view.range(descriptor.identifier, low, high, neighbors=True)

    panel.time_plot.set_trace((session, descriptor.identifier), "Bench value", "kPa", bounds, query)
    curve = panel.time_plot.curve
    assert curve is not None
    return curve


def points(curve):
    x, y = curve.getData()
    assert x is not None and y is not None
    return x, y


def test_irregular_times_gaps_and_zero_keep_original_coordinates(qtbot):
    panel = RightPanel()
    qtbot.addWidget(panel)
    origin = 2**60
    values = [(0, 0), (10, 5), (20, None), (57, 8), (82, -1)]
    session, descriptor = session_for(
        SignalSample(origin + t, value, available=value is not None)
        for t, value in reversed(values)
    )
    before = session.snapshot()
    curve = plot_session(panel, session, descriptor, [])
    x, y = points(curve)
    assert x.tolist() == [t / 1e9 for t, _ in values]
    assert [y[0], y[1], y[3], y[4]] == [0, 5, 8, -1]
    assert math.isnan(y[2])
    assert curve.opts["connect"].tolist() == [True, False, False, True, False]
    assert session.snapshot() == before


def test_large_trace_keeps_narrow_extrema_and_omitted_gap_breaks(qtbot):
    panel = RightPanel()
    qtbot.addWidget(panel)
    # Both gaps share a bucket. Only one is retained as a point, but neither may be bridged.
    special = {1204: 111, 1205: None, 1208: -99, 1209: None}
    session, descriptor = session_for(
        SignalSample(i * 1_000_000, special.get(i, 3), available=special.get(i, 3) is not None)
        for i in range(20_000)
    )
    before = session.snapshot()
    requests = []
    curve = plot_session(panel, session, descriptor, requests)
    x, y = points(curve)
    assert len(x) <= POINT_LIMIT
    assert x[0] == 0 and x[-1] == 19.999
    assert 111 in y and -99 in y
    assert 1.209 not in x
    low_peak = y.tolist().index(-99)
    assert not curve.opts["connect"][low_peak]
    assert requests == [(0, 19_999_000_000)]

    for i in range(10):
        panel.time_plot.view.setXRange(1.2 + i / 10000, 1.22, padding=0)
    assert len(requests) == 1  # Pan events are coalesced before the next draw.
    qtbot.waitUntil(lambda: len(requests) == 2)
    assert requests[-1][1] - requests[-1][0] < 25_000_000
    x, y = points(curve)
    assert len(x) < 30 and 1.209 in x
    assert math.isnan(y[x.tolist().index(1.209)])
    panel.fit_plot.click()
    assert points(curve)[0][-1] == 19.999
    assert session.snapshot() == before


def test_updates_keep_manual_range_and_empty_view_is_safe(qtbot):
    panel = RightPanel()
    qtbot.addWidget(panel)
    session, descriptor = session_for(SignalSample(i * 1_000_000_000, i) for i in range(5))
    curve = plot_session(panel, session, descriptor, [])
    panel.time_plot.view.setXRange(0.3, 0.4, padding=0)
    panel.time_plot.refresh()
    # Neighbors extend the curve across the narrow viewport without creating a sample.
    assert points(curve)[0].tolist() == [0, 1]
    session.append(descriptor.identifier, SignalSample(8_000_000_000, 8))
    assert plot_session(panel, session, descriptor, []) is curve
    panel.time_plot.refresh()
    assert panel.time_plot.view.viewRange()[0] == pytest.approx([0.3, 0.4])
    panel.time_plot.view.setXRange(10, 11, padding=0)
    panel.time_plot.refresh()
    assert curve.getData() == (None, None)


def test_real_dashboard_csv_plots_earlier_readings_and_replay_marker(qtbot, tmp_path: Path):
    path = tmp_path / "wide.csv"
    path.write_text(
        "timestamp_ms,pressure\n" + "".join(f"{i},{100 if i == 10 else 3}\n" for i in range(3100)),
        encoding="utf-8",
    )
    window = create_main_window()
    owner = window.source_owner
    assert isinstance(owner, SourceController)
    window.show()
    try:
        owner.open_csv(path)
        qtbot.waitUntil(lambda: owner._finished_rendered, timeout=8000)
        panel = window.right_panel
        curve = panel.time_plot.curve
        assert curve is not None
        x, y = points(curve)
        assert x[0] == 0 and x[-1] == 3.099 and 100 in y
        assert len(x) <= POINT_LIMIT
        assert panel.fit_plot.isVisible()
        window.replay_bar.slider.setValue(5000)
        cursor = owner.replay.cursor
        assert cursor is not None and cursor.value() == pytest.approx(1.5495)
    finally:
        owner.disconnect()
        qtbot.waitUntil(lambda: owner.source is None and owner._preparing is None)
        window.close()
        window.deleteLater()
