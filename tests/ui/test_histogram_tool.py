"""The Analysis > Histogram tool on real session data in the running window."""

from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFileDialog

from lbr_dashboard.app import create_main_window
from lbr_dashboard.core import SimulatorSource, TelemetrySession, example_signals
from lbr_dashboard.core.histogram import DEFAULT_BINS, histogram
from lbr_dashboard.source_controller import SourceController
from lbr_dashboard.ui.analysis import AnalysisContext
from lbr_dashboard.ui.signal_browser import ID_ROLE
from lbr_dashboard.ui.tools.histogram import HistogramTool

SAMPLES = Path(__file__).resolve().parents[2] / "sample_data"
DEMO = SAMPLES / "timestamped_demo.csv"
TEMPERATURE = "csv.column.1"
FLOW = "csv.column.2"
VALVE = "csv.column.3"
MS = 1_000_000


def open_csv(qtbot, monkeypatch, path: Path):
    window = create_main_window()
    window.show()
    owner = window.source_owner
    assert isinstance(owner, SourceController)
    owner.replay._now = lambda: 0
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(path), ""))
    picker = window.source_picker
    picker.choices.setCurrentIndex(picker.choices.findData("csv"))
    picker.connect_button.click()
    qtbot.waitUntil(lambda: window.replay_bar.slider.isEnabled())
    return window


@pytest.fixture
def demo(qtbot, monkeypatch):
    window = open_csv(qtbot, monkeypatch, DEMO)
    yield window
    window.close()
    qtbot.waitUntil(lambda: not window.isVisible())
    window.deleteLater()


def select(window, signal_id):
    browser = window.left_panel.signal_browser
    browser.search.setText(signal_id)
    model = browser.tree.model()
    assert model is not None
    matches = model.match(
        model.index(0, 0),
        ID_ROLE,
        signal_id,
        2,
        Qt.MatchFlag.MatchExactly | Qt.MatchFlag.MatchRecursive,
    )
    assert len(matches) == 1
    browser.tree.setCurrentIndex(matches[0])


def tool(window) -> HistogramTool:
    dock = window.analysis_docks.open("histogram")
    widget = dock.widget()
    assert isinstance(widget, HistogramTool)
    widget.refresh()
    return widget


def bars(widget: HistogramTool) -> tuple[list[float], list[float], list[int]]:
    item = widget.view.bars
    assert item is not None
    opts: Any = item.opts
    return (
        [float(x) for x in opts["x0"]],
        [float(x) for x in opts["x1"]],
        [int(h) for h in opts["height"]],
    )


def test_menu_lists_the_histogram_after_statistics(demo):
    labels = [action.text() for action in demo.analysis_docks.menu.actions()]
    assert labels[-2:] == ["Statistics", "Histogram"]


def test_whole_session_histogram_counts_valid_samples_and_keeps_gaps_missing(demo):
    select(demo, TEMPERATURE)
    widget = tool(demo)
    hist = widget.result
    assert hist is not None
    # 21.5, 22, 23.5 and 24 are valid; the reading at 250 ms is missing.
    assert hist.edges[0] == 21.5 and hist.edges[-1] == 24.0
    assert len(hist.counts) == DEFAULT_BINS
    assert hist.counts == (1, 0, 1, 0, 0, 0, 0, 0, 1, 1)
    assert (hist.missing, hist.non_finite, hist.total) == (1, 0, 5)
    assert (hist.start_ns, hist.end_ns) == (0, 1000 * MS)
    assert "whole session" in widget.hint.text()
    assert "unit: no unit" in widget.hint.text()
    assert "Missing: 1  Non-finite: 0" in widget.view.summary.text()
    assert "Interval: [0 s, 1 s]" in widget.view.summary.text()
    assert widget.edges.text().startswith("Bin edges: 21.5 to 24, 10 bins of width 0.25.")
    assert "half-open" in widget.edges.text()
    rows = widget.rows()
    assert rows[0] == ("[21.5, 21.75)", "1")
    assert rows[-1] == ("[23.75, 24]", "1")


def test_missing_readings_never_enter_a_bin_as_zero(demo):
    select(demo, FLOW)
    widget = tool(demo)
    widget.restore_configuration({"bins": 2})
    widget.refresh()
    # 0, 12, 14, missing, 0: two real zeros in the first bin, never three.
    assert bars(widget) == ([0.0, 7.0], [7.0, 14.0], [2, 2])
    assert widget.result is not None and widget.result.missing == 1
    assert widget.rows() == [("[0, 7)", "2"), ("[7, 14]", "2")]


def test_histogram_follows_the_shared_interval_with_closed_ends(demo):
    select(demo, TEMPERATURE)
    widget = tool(demo)
    demo.analysis.selection.select(0, 250 * MS)
    widget.refresh()
    hist = widget.result
    assert hist is not None
    assert (hist.start_ns, hist.end_ns) == (0, 250 * MS)
    assert (hist.valid, hist.missing) == (2, 1)
    assert hist.edges[0] == 21.5 and hist.edges[-1] == 22.0
    assert hist.counts[0] == 1 and hist.counts[-1] == 1
    assert "selected time interval" in widget.hint.text()
    assert "Interval: [0 s, 0.25 s]" in widget.view.summary.text()

    demo.analysis_docks.clear_action.trigger()
    widget.refresh()
    assert widget.result is not None and widget.result.total == 5


def test_all_missing_interval_draws_no_bars_and_reports_the_missing_count(demo):
    select(demo, TEMPERATURE)
    widget = tool(demo)
    demo.analysis.selection.select(200 * MS, 300 * MS)
    widget.refresh()
    hist = widget.result
    assert hist is not None and hist.is_empty
    assert widget.view.bars is None
    assert hist.missing == 1 and widget.rows() == []
    assert "Missing: 1" in widget.view.summary.text()
    assert widget.edges.text().startswith("Bin edges: none")


def test_bin_count_changes_redraw_and_round_trip_as_plain_data(demo, qtbot):
    select(demo, TEMPERATURE)
    widget = tool(demo)
    assert widget.configuration() == {"bins": DEFAULT_BINS}
    widget.bins.setValue(5)
    qtbot.waitUntil(lambda: widget.result is not None and len(widget.result.counts) == 5)
    saved = widget.configuration()
    assert saved == {"bins": 5}

    fresh = HistogramTool(demo.analysis)
    qtbot.addWidget(fresh)
    fresh.restore_configuration(saved)
    fresh.refresh()
    assert fresh.result is not None and widget.result is not None
    assert fresh.result.counts == widget.result.counts
    for invalid in ({"bins": 0}, {"bins": "7"}, {"bins": True}, {"other": 3}, {}):
        fresh.restore_configuration(invalid)
        assert fresh.configuration() == {"bins": 5}


def test_non_numeric_and_unselected_signals_explain_themselves(demo):
    widget = tool(demo)
    demo.analysis.set_selected(None)
    widget.refresh()
    assert "Select a numeric signal" in widget.hint.text()
    assert widget.result is None and widget.view.bars is None
    select(demo, VALVE)
    widget.refresh()
    assert "a histogram needs a numeric signal" in widget.hint.text()
    assert widget.result is None and widget.rows() == []


def test_too_many_bins_for_a_narrow_range_is_explained(qtbot, monkeypatch, tmp_path):
    path = tmp_path / "narrow.csv"
    path.write_text("timestamp_ms,value\n0,1\n1,1.0000000000000002\n", encoding="ascii")
    window = open_csv(qtbot, monkeypatch, path)
    try:
        select(window, "csv.column.1")
        widget = tool(window)
        widget.restore_configuration({"bins": 500})
        widget.refresh()
        assert "cannot split the values into 500 bins" in widget.hint.text()
        assert widget.result is None and widget.view.bars is None
    finally:
        window.close()
        window.deleteLater()


def test_plot_follows_the_theme_across_redraws(demo):
    select(demo, TEMPERATURE)
    widget = tool(demo)
    demo.analysis.set_plot_colors("#fafafa", "#101010")
    widget.restore_configuration({"bins": 3})
    widget.refresh()
    plot = widget.view.plot_widget
    item = plot.getPlotItem()
    assert item is not None
    assert plot.backgroundBrush().color().name() == "#fafafa"
    assert item.titleLabel.opts["color"] == "#101010"
    assert item.getAxis("bottom").textPen().color().name() == "#101010"
    assert widget.view.minimumWidth() >= 320 and widget.view.minimumHeight() >= 200


def test_mixed_rate_signals_with_units_match_the_core_histogram(qtbot):
    source = SimulatorSource(
        example_signals("process"), seed=17, duration_ns=1_000_000_000, capacity=256
    )
    store = TelemetrySession(source.schema, max_samples_per_signal=200)
    source.open()
    source.start()
    try:
        assert source.join(5)
        store.ingest_source(source)
    finally:
        assert source.close(1)
    context = AnalysisContext()
    context.set_snapshot(store, store.snapshot())
    widget = HistogramTool(context)
    qtbot.addWidget(widget)
    snapshot = store.snapshot()
    results = {}
    for signal_id, unit in (("process.pressure", "Pa"), ("process.temperature", "deg C")):
        context.set_selected(signal_id)
        widget.refresh()
        hist = widget.result
        assert hist is not None
        records = snapshot.series[signal_id]
        expected = histogram(
            [record.sample.timestamp_ns for record in records],
            [record.sample.value if record.sample.available else None for record in records],
            start_ns=hist.start_ns,
            end_ns=hist.end_ns,
            unit=hist.unit,
        )
        assert hist == expected
        assert hist.total == len(records)
        if unit == "Pa":
            assert hist.unit == "Pa" and "unit: Pa" in widget.hint.text()
            header = widget.table.horizontalHeaderItem(0)
            assert header is not None and header.text() == "Bin (Pa)"
        results[signal_id] = hist
    pressure, temperature = results["process.pressure"], results["process.temperature"]
    # Same interval, different rates: counts follow each signal's own sample rate.
    assert (pressure.start_ns, pressure.end_ns) == (temperature.start_ns, temperature.end_ns)
    assert pressure.total == 91 and temperature.total == 2
    assert pressure.missing > 0 and temperature.missing == 0
