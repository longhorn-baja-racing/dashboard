"""The XY plot tool in the real app and over sessions with mixed-rate signals."""

from __future__ import annotations

import time
from itertools import islice
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFileDialog

from lbr_dashboard.app import create_main_window
from lbr_dashboard.core import SignalDescriptor, SignalSample, SignalSchema, TelemetrySession
from lbr_dashboard.source_controller import SourceController
from lbr_dashboard.ui.analysis import AnalysisContext
from lbr_dashboard.ui.signal_browser import ID_ROLE
from lbr_dashboard.ui.tools.xy import XYTool

DEMO = Path(__file__).resolve().parents[2] / "sample_data" / "timestamped_demo.csv"
TEMPERATURE = "csv.column.1"
FLOW = "csv.column.2"
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


def open_xy(window) -> XYTool:
    dock = window.analysis_docks.open("xy")
    tool = dock.widget()
    assert isinstance(tool, XYTool)
    tool.refresh()
    return tool


def choices(combo) -> list[tuple[str, str]]:
    return [(combo.itemData(index), combo.itemText(index)) for index in range(combo.count())]


def axis_text(tool: XYTool, name: str) -> str:
    item = tool.plot.plot_widget.getPlotItem()
    assert item is not None
    return item.getAxis(name).labelText


def points(tool: XYTool) -> list[tuple[float, float]]:
    xs, ys = tool.plot.scatter.getData()
    return list(zip((float(x) for x in xs), (float(y) for y in ys)))


def numeric(identifier: str, unit: str | None, domain: str = "relative") -> SignalDescriptor:
    return SignalDescriptor(
        identifier,
        identifier.split(".")[-1].title(),
        ("bench",),
        "numeric",
        "bench",
        canonical_unit=unit,
        timestamp_domain=domain,
    )


def session_context(qtbot, readings, descriptors=None, *, max_points=None):
    descriptors = descriptors or [numeric(key, None) for key in readings]
    session = TelemetrySession(SignalSchema(1, tuple(descriptors)))
    for item in descriptors:
        samples = (
            SignalSample(
                t, value, available=value is not None, timestamp_domain=item.timestamp_domain
            )
            for t, value in readings[item.identifier]
        )
        while batch := tuple(islice(samples, 5000)):
            session.extend(item.identifier, batch)
    context = AnalysisContext()
    context.set_snapshot(session, session.snapshot())
    tool = XYTool(context) if max_points is None else XYTool(context, max_points=max_points)
    qtbot.addWidget(tool)
    tool.resize(640, 480)
    tool.show()
    return context, tool


def speed_vs_cvt(tool: XYTool, mode: str = "nearest", tolerance_ns: int = 10 * MS) -> None:
    tool.restore_configuration(
        {
            "x_signal_id": SPEED.identifier,
            "y_signal_id": CVT.identifier,
            "mode": mode,
            "tolerance_ns": tolerance_ns,
        }
    )


SPEED = numeric("bench.speed", "km/h")
CVT = numeric("bench.cvt_temperature", "degC")
# Speed every 10 ms, CVT temperature every 25 ms.
MIXED = {
    SPEED.identifier: [(t * MS, float(t)) for t in range(0, 60, 10)],
    CVT.identifier: [(0, 1.0), (25 * MS, 2.0), (50 * MS, 3.0)],
}


def test_menu_opens_the_tool_with_numeric_choices_and_documented_pairs(demo):
    window, _ = demo
    labels = [action.text() for action in window.analysis_docks.menu.actions()]
    assert "XY plot" in labels
    tool = open_xy(window)
    assert window.analysis_docks.open("xy").widget() is tool
    x_choices = choices(tool.x_choice)
    assert (TEMPERATURE, f"chamber_temperature - {TEMPERATURE}") in x_choices
    assert (FLOW, f"flow_rate - {FLOW}") in x_choices
    assert VALVE not in [identifier for identifier, _ in x_choices]
    assert choices(tool.y_choice) == x_choices
    assert (tool.x_id, tool.y_id) == (TEMPERATURE, FLOW)

    # Temperature is missing at 250 ms and flow at 500 ms; both share timestamps.
    assert points(tool) == [(21.5, 0.0), (22.0, 12.0), (24.0, 0.0)]
    note = tool.plot.note_label.text()
    assert "Showing 3 of 3 pairs." in note
    assert "Excluded: 1 X missing, 1 Y missing, 0 no Y within tolerance." in note
    assert axis_text(tool, "bottom") == "chamber_temperature"
    assert axis_text(tool, "left") == "flow_rate"
    assert "over the whole session" in tool.hint.text()


def test_shared_interval_restricts_pairs_and_cursor_marks_the_nearest_pair(demo):
    window, owner = demo
    tool = open_xy(window)
    window.analysis.selection.select(0, 250 * MS)
    tool.refresh()
    assert points(tool) == [(21.5, 0.0), (22.0, 12.0)]
    assert "Excluded: 1 X missing, 0 Y missing" in tool.plot.note_label.text()
    assert "selected interval 0.000 s to 0.250 s" in tool.hint.text()

    window.replay_bar.slider.setValue(1000)
    owner.poll()
    assert window.analysis.cursor_ns == 100 * MS
    assert tool.plot.marker.isVisible()
    xs, ys = tool.plot.marker.getData()
    assert (list(xs), list(ys)) == ([22.0], [12.0])
    window.replay_bar.slider.setValue(9000)
    owner.poll()
    assert not tool.plot.marker.isVisible()  # The cursor left the interval.

    window.analysis_docks.clear_action.trigger()
    tool.refresh()
    assert len(points(tool)) == 3


def test_user_choices_change_the_plot_and_the_saved_configuration(demo):
    window, _ = demo
    tool = open_xy(window)
    tool.x_choice.setCurrentIndex(tool.x_choice.findData(FLOW))
    tool.y_choice.setCurrentIndex(tool.y_choice.findData(TEMPERATURE))
    tool.mode_choice.setCurrentIndex(tool.mode_choice.findData("previous"))
    tool.tolerance.setValue(2.5)
    assert tool.configuration() == {
        "x_signal_id": FLOW,
        "y_signal_id": TEMPERATURE,
        "mode": "previous",
        "tolerance_ns": 2_500_000,
    }
    assert points(tool) == [(0.0, 21.5), (12.0, 22.0), (0.0, 24.0)]
    assert axis_text(tool, "bottom") == "flow_rate"
    assert "latest Y at or before each X, at most 2500 us old" in tool.plot.note_label.text()


def test_restoring_missing_signals_shows_them_unavailable_without_plotting(demo):
    window, _ = demo
    tool = open_xy(window)
    saved = {
        "x_signal_id": "motion.speed",
        "y_signal_id": FLOW,
        "mode": "previous",
        "tolerance_ns": 5 * MS,
    }
    tool.restore_configuration(saved)
    assert tool.configuration() == saved
    assert tool.x_choice.currentText() == "motion.speed (unavailable)"
    assert tool.x_choice.currentData() == "motion.speed"
    assert tool.y_choice.currentData() == FLOW
    assert tool.mode_choice.currentData() == "previous"
    assert tool.tolerance.value() == pytest.approx(5.0)
    assert "X signal motion.speed is not in this session" in tool.hint.text()
    assert points(tool) == []
    assert tool.plot.highlight_time(0) is None

    # A new revision of the same session keeps the unavailable choice.
    tool.refresh()
    assert tool.configuration() == saved
    tool.x_choice.setCurrentIndex(tool.x_choice.findData(TEMPERATURE))
    assert len(points(tool)) == 3
    assert "motion.speed (unavailable)" not in [text for _, text in choices(tool.x_choice)]


@pytest.mark.parametrize(
    "config",
    [
        [],
        {"x_signal_id": 3},
        {"x_signal_id": " "},
        {"mode": "linear"},
        {"tolerance_ns": -1},
        {"tolerance_ns": True},
        {"tolerance_ns": 1.5},
    ],
)
def test_malformed_configuration_is_rejected_and_changes_nothing(qtbot, config):
    _, tool = session_context(qtbot, MIXED, [SPEED, CVT])
    before = tool.configuration()
    with pytest.raises(ValueError):
        tool.restore_configuration(config)
    assert tool.configuration() == before


def test_unequal_rates_pair_by_policy_with_units_on_both_axes(qtbot):
    _, tool = session_context(qtbot, MIXED, [SPEED, CVT])
    speed_vs_cvt(tool)
    assert axis_text(tool, "bottom") == "Speed (km/h)"
    assert axis_text(tool, "left") == "Cvt_Temperature (degC)"
    assert (SPEED.identifier, "Speed (km/h) - bench.speed") in choices(tool.x_choice)
    # 20 ms is 5 ms from the 25 ms sample; 40 ms is 10 ms from 50 ms and 15 ms from 25 ms.
    assert points(tool) == [
        (0.0, 1.0),
        (10.0, 1.0),
        (20.0, 2.0),
        (30.0, 2.0),
        (40.0, 3.0),
        (50.0, 3.0),
    ]

    tool.mode_choice.setCurrentIndex(tool.mode_choice.findData("previous"))
    # Held values older than 10 ms are not used (20 ms and 40 ms).
    assert points(tool) == [(0.0, 1.0), (10.0, 1.0), (30.0, 2.0), (50.0, 3.0)]
    assert "Excluded: 0 X missing, 0 Y missing, 2 no Y within tolerance." in (
        tool.plot.note_label.text()
    )


def test_held_value_from_before_the_interval_still_pairs_at_its_start(qtbot):
    context, tool = session_context(qtbot, MIXED, [SPEED, CVT])
    speed_vs_cvt(tool, "previous")
    context.selection.select(30 * MS, 50 * MS)
    tool.refresh()
    # The 25 ms temperature is outside the interval but within tolerance of 30 ms.
    assert points(tool) == [(30.0, 2.0), (50.0, 3.0)]
    assert "1 no Y within tolerance" in tool.plot.note_label.text()


def test_gaps_are_excluded_by_reason_and_never_plotted(qtbot):
    readings = {
        "bench.a": [(0, 1.0), (10 * MS, None), (20 * MS, 3.0), (200 * MS, 4.0)],
        "bench.b": [(0, None), (10 * MS, 2.0), (20 * MS, 3.0)],
    }
    _, tool = session_context(qtbot, readings)
    tool.restore_configuration(
        {"x_signal_id": "bench.a", "y_signal_id": "bench.b", "tolerance_ns": 0}
    )
    assert points(tool) == [(3.0, 3.0)]
    assert "Excluded: 1 X missing, 1 Y missing, 1 no Y within tolerance." in (
        tool.plot.note_label.text()
    )


def test_different_time_bases_are_not_paired(qtbot):
    other = numeric("bench.other", "V", domain="device")
    readings = {SPEED.identifier: MIXED[SPEED.identifier], other.identifier: [(0, 1.0)]}
    _, tool = session_context(qtbot, readings, [SPEED, other])
    assert {tool.x_id, tool.y_id} == {SPEED.identifier, other.identifier}
    tool.restore_configuration({"x_signal_id": SPEED.identifier, "y_signal_id": "bench.other"})
    assert "use different time bases (relative and device)" in tool.hint.text()
    assert points(tool) == []


def test_large_interval_is_bounded_and_stays_responsive(qtbot):
    count = 60_000
    readings = {
        SPEED.identifier: [(t * MS, float(t % 97)) for t in range(count)],
        CVT.identifier: [(t * MS + 300_000, float(t % 89)) for t in range(0, count, 3)],
    }
    started = time.perf_counter()
    context, tool = session_context(qtbot, readings, [SPEED, CVT])
    speed_vs_cvt(tool, tolerance_ns=2 * MS)
    elapsed = time.perf_counter() - started
    assert len(tool.plot.scatter.getData()[0]) == 20_000
    assert "Showing 20000 of 60000 pairs." in tool.plot.note_label.text()
    assert elapsed < 10

    small = XYTool(context, max_points=50)
    qtbot.addWidget(small)
    speed_vs_cvt(small, tolerance_ns=2 * MS)
    assert len(small.plot.scatter.getData()[0]) == 50


def test_streaming_refresh_keeps_the_view_but_new_choices_refit(qtbot):
    _, tool = session_context(qtbot, MIXED, [SPEED, CVT])
    speed_vs_cvt(tool)
    view = tool.plot.plot_widget.getPlotItem().getViewBox()  # type: ignore[union-attr]
    view.setRange(xRange=(100, 200), yRange=(100, 200), padding=0)
    tool.refresh()
    assert view.viewRange()[0] == pytest.approx([100, 200])
    tool.y_choice.setCurrentIndex(tool.y_choice.findData(SPEED.identifier))
    assert view.autoRangeEnabled()[0]


def test_empty_states_are_explained(qtbot):
    context = AnalysisContext()
    tool = XYTool(context)
    qtbot.addWidget(tool)
    assert "Load a session" in tool.hint.text()
    assert tool.x_choice.count() == 0

    text = SignalDescriptor("bench.mode", "Mode", ("bench",), "string", "bench")
    session = TelemetrySession(SignalSchema(1, (text,)))
    session.append("bench.mode", SignalSample(0, "idle"))
    context.set_snapshot(session, session.snapshot())
    tool.refresh()
    assert "no numeric signals" in tool.hint.text()
    assert tool.configuration()["x_signal_id"] is None


def test_plot_follows_the_theme(qtbot):
    context, tool = session_context(qtbot, MIXED, [SPEED, CVT])
    speed_vs_cvt(tool)
    context.set_plot_colors("#ffffff", "#101010")
    plot = tool.plot.plot_widget
    assert plot.backgroundBrush().color().name() == "#ffffff"
    item = plot.getPlotItem()
    assert item is not None
    assert item.getAxis("left").textPen().color().name() == "#101010"
    assert axis_text(tool, "left") == "Cvt_Temperature (degC)"


def test_signal_browser_selection_does_not_change_the_xy_choices(demo):
    window, _ = demo
    tool = open_xy(window)
    browser = window.left_panel.signal_browser
    browser.search.setText(VALVE)
    model = browser.tree.model()
    assert model is not None
    match = model.match(
        model.index(0, 0),
        ID_ROLE,
        VALVE,
        1,
        Qt.MatchFlag.MatchExactly | Qt.MatchFlag.MatchRecursive,
    )
    browser.tree.setCurrentIndex(match[0])
    tool.refresh()
    assert (tool.x_id, tool.y_id) == (TEMPERATURE, FLOW)
