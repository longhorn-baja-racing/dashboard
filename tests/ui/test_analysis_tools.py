"""The shared interval, the main plot region and the statistics tool in the real app."""

from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFileDialog

from lbr_dashboard.app import create_main_window
from lbr_dashboard.core.interval import TimeInterval
from lbr_dashboard.source_controller import SourceController
from lbr_dashboard.ui.signal_browser import ID_ROLE
from lbr_dashboard.ui.tools.statistics import StatisticsTool

SAMPLES = Path(__file__).resolve().parents[2] / "sample_data"
DEMO = SAMPLES / "timestamped_demo.csv"
TEMPERATURE = "csv.column.1"
VALVE = "csv.column.3"
MS = 1_000_000


@pytest.fixture
def demo(qtbot, monkeypatch):
    window = create_main_window()
    window.show()
    owner = window.source_owner
    assert isinstance(owner, SourceController)
    now = [0]
    owner.replay._now = lambda: now[0]
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(DEMO), ""))
    picker = window.source_picker
    picker.choices.setCurrentIndex(picker.choices.findData("csv"))
    picker.connect_button.click()
    qtbot.waitUntil(lambda: window.replay_bar.slider.isEnabled())
    yield window, owner
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


def statistics(window) -> StatisticsTool:
    dock = window.analysis_docks.open("statistics")
    tool = dock.widget()
    assert isinstance(tool, StatisticsTool)
    tool.refresh()
    return tool


def rows(tool: StatisticsTool) -> dict[str, str]:
    return dict(tool.panel.rows())


def test_session_bounds_and_selected_signal_reach_the_shared_context(demo):
    window, owner = demo
    select(window, TEMPERATURE)
    context = window.analysis
    assert context.session is owner.session
    assert context.bounds == TimeInterval(0, 1000 * MS)
    assert context.selected_id == TEMPERATURE
    assert context.interval is None
    assert not window.analysis_docks.clear_action.isEnabled()


def test_visible_range_selection_draws_the_region_at_the_shared_endpoints(demo):
    window, _ = demo
    select(window, TEMPERATURE)
    docks = window.analysis_docks
    assert docks.region is not None and not docks.region.region.isVisible()
    window.right_panel.plot_widget.getPlotItem().getViewBox().setXRange(0.1, 0.5, padding=0)
    interval = docks.select_visible_range()
    assert interval is not None and window.analysis.interval == interval
    assert interval.start_ns == pytest.approx(100 * MS, abs=1)
    assert interval.end_ns == pytest.approx(500 * MS, abs=1)
    low, high = docks.region.region.getRegion()
    assert (low, high) == pytest.approx((0.1, 0.5), abs=1e-9)
    assert docks.region.region.isVisible() and docks.clear_action.isEnabled()

    window.analysis.selection.select(250 * MS, 1000 * MS)
    assert docks.region.region.getRegion() == pytest.approx((0.25, 1.0), abs=1e-9)
    docks.clear_action.trigger()
    assert window.analysis.interval is None and not docks.region.region.isVisible()


def test_invalid_ranges_are_rejected_or_clamped_to_the_session(demo):
    window, _ = demo
    selection = window.analysis.selection
    assert selection.select(900 * MS, -50 * MS) == TimeInterval(0, 900 * MS)
    with pytest.raises(ValueError, match="outside the session range"):
        selection.select(2000 * MS, 3000 * MS)
    assert selection.interval == TimeInterval(0, 900 * MS)


def test_statistics_match_hand_calculated_values_and_keep_gaps_missing(demo):
    window, _ = demo
    select(window, TEMPERATURE)
    window.analysis.selection.select(0, 250 * MS)
    values = rows(statistics(window))
    # 21.5 at 0 ms and 22 at 100 ms are valid; the reading at 250 ms is missing.
    assert values["Samples"] == "2 valid, 1 missing, 0 invalid (3 in interval)"
    assert values["Minimum"] == "21.5"
    assert values["Maximum"] == "22"
    assert values["Mean"] == "21.75"
    assert values["Interval"] == "0.000 s to 0.250 s"
    assert "each valid sample counts once" in values["Weighting"]
    assert "missing = no value or NaN" in values["Counts"]


def test_statistics_follow_the_shared_interval_and_cursor(demo):
    window, owner = demo
    select(window, TEMPERATURE)
    tool = statistics(window)
    assert "select a time interval" in tool.hint.text()
    assert rows(tool)["Current state"].startswith("valid: sample at 0.000 s")

    window.replay_bar.slider.setValue(2500)
    owner.poll()
    tool.refresh()
    assert rows(tool)["Current value"] == "--"
    assert rows(tool)["Current state"].startswith("missing: latest sample at 0.250 s")

    window.analysis.selection.select(250 * MS, 1000 * MS)
    tool.refresh()
    values = rows(tool)
    assert values["Samples"] == "2 valid, 1 missing, 0 invalid (3 in interval)"
    assert values["Mean"] == "23.75"
    assert values["Current value"] == "--"


def test_statistics_explain_non_numeric_signals(demo):
    window, _ = demo
    select(window, VALVE)
    tool = statistics(window)
    assert "statistics need a numeric signal" in tool.hint.text()
    assert tool.panel.rows() == []


def test_opening_a_tool_twice_reuses_its_dock(demo):
    window, _ = demo
    first = window.analysis_docks.open("statistics")
    assert window.analysis_docks.open("statistics") is first


def test_legacy_table_clears_the_session_interval(demo):
    window, owner = demo
    select(window, TEMPERATURE)
    window.analysis.selection.select(0, 500 * MS)
    window.load_csv(SAMPLES / "sample_baja_log.csv")
    assert not owner.has_session()
    assert window.analysis.snapshot is None
    assert window.analysis.interval is None and window.analysis.bounds is None
    assert window.analysis_docks.region is None
    assert not window.analysis_docks.select_action.isEnabled()


def test_tool_plots_follow_theme_changes_and_are_released_with_their_widget(qtbot):
    import pyqtgraph as pg

    from lbr_dashboard.ui.analysis import AnalysisContext

    context = AnalysisContext()
    plot = pg.PlotWidget()
    qtbot.addWidget(plot)
    context.style_plot(plot)
    context.set_plot_colors("#ffffff", "#101010")
    assert plot.backgroundBrush().color().name() == "#ffffff"
    item = plot.getPlotItem()
    assert item is not None
    assert item.getAxis("left").textPen().color().name() == "#101010"
    plot.deleteLater()
    qtbot.waitUntil(lambda: context.receivers("2theme_changed()") == 0)
    context.set_plot_colors("#000000", "#eeeeee")
