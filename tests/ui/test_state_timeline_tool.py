"""The state timeline tool shows session states and events in the real window."""

from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFileDialog

from lbr_dashboard.app import create_main_window
from lbr_dashboard.core import SignalDescriptor, SignalSample, SignalSchema, TelemetrySession
from lbr_dashboard.core.state_timeline import SegmentKind
from lbr_dashboard.source_controller import SourceController
from lbr_dashboard.ui.analysis import AnalysisContext
from lbr_dashboard.ui.signal_browser import ID_ROLE
from lbr_dashboard.ui.tools.state_timeline import (
    MAX_EVENTS,
    MAX_SEGMENTS,
    StateTimelineTool,
)

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
    return window, owner


@pytest.fixture
def demo(qtbot, monkeypatch):
    window, owner = open_csv(qtbot, monkeypatch, DEMO)
    yield window, owner
    close(qtbot, window)


def close(qtbot, window) -> None:
    window.close()
    qtbot.waitUntil(lambda: not window.isVisible())
    window.deleteLater()


def select(window, signal_id: str) -> None:
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


def open_tool(window) -> StateTimelineTool:
    actions = {action.text(): action for action in window.analysis_docks.menu.actions()}
    actions["State timeline"].trigger()
    tool = window.analysis_docks.docks["state_timeline"].widget()
    assert isinstance(tool, StateTimelineTool)
    tool.refresh()
    return tool


def segments(tool: StateTimelineTool, row: int = 0) -> list[tuple[int, int, str]]:
    return [(item.start_ns, item.end_ns, item.label) for item in tool.rows[row].timeline.segments]


def event_times(tool: StateTimelineTool, row: int = 0) -> list[int]:
    return [item.time_ns for item in tool.rows[row].timeline.events]


def list_item(tool: StateTimelineTool, signal_id: str):
    for row in range(tool.signals.count()):
        item = tool.signals.item(row)
        if item.data(Qt.ItemDataRole.UserRole) == signal_id:
            return item
    raise AssertionError(signal_id)


def drawn(tool: StateTimelineTool, kind: str) -> list:
    return [item for item in tool._items if type(item).__name__ == kind]


def test_menu_entry_shows_the_string_signal_at_exact_sample_times(demo):
    window, _ = demo
    tool = open_tool(window)
    assert window.analysis_docks.open("state_timeline").widget() is tool
    assert tool.shown_ids() == [VALVE]
    assert list_item(tool, VALVE).checkState() == Qt.CheckState.Checked
    assert list_item(tool, TEMPERATURE).checkState() == Qt.CheckState.Unchecked
    assert segments(tool) == [
        (0, 100 * MS, "closed"),
        (100 * MS, 500 * MS, "open"),
        (500 * MS, 1000 * MS, "closed"),
    ]
    assert event_times(tool) == [0, 100 * MS, 250 * MS, 500 * MS, 1000 * MS]
    (bars,) = drawn(tool, "BarGraphItem")
    assert list(bars.opts["x0"]) == pytest.approx([0.0, 0.1, 0.5])
    (markers,) = drawn(tool, "ScatterPlotItem")
    assert [spot.pos().x() for spot in markers.points()] == pytest.approx(
        [0.0, 0.1, 0.25, 0.5, 1.0]
    )
    labels = [item.textItem.toPlainText() for item in drawn(tool, "TextItem")]
    assert labels == ["closed", "open", "closed"]
    assert "csv.column.3 (string): 5 samples, 2 changes" in tool.hint.text()


def test_missing_samples_are_drawn_as_no_data_not_as_a_held_state(qtbot, monkeypatch, tmp_path):
    path = tmp_path / "door.csv"
    path.write_text("timestamp_ms,door,speed\n0,open,1\n100,,2\n200,closed,3\n400,open,4\n")
    window, _ = open_csv(qtbot, monkeypatch, path)
    try:
        tool = open_tool(window)
        assert segments(tool) == [
            (0, 100 * MS, "open"),
            (100 * MS, 200 * MS, "no data"),
            (200 * MS, 400 * MS, "closed"),
        ]
        gap = tool.rows[0].timeline.segments[1]
        assert gap.kind is SegmentKind.NO_DATA and gap.value is None
        (bars,) = drawn(tool, "BarGraphItem")
        assert bars.opts["brushes"][1].style() == Qt.BrushStyle.BDiagPattern
        assert "hatched gray = no data" in tool.hint.text()
    finally:
        close(qtbot, window)


def test_cursor_and_interval_follow_the_shared_replay_clock_and_selection(demo):
    window, owner = demo
    tool = open_tool(window)
    assert tool.time_cursor.isVisible() and tool.time_cursor.value() == 0
    window.replay_bar.slider.setValue(2500)
    owner.poll()
    assert window.analysis.cursor_ns == 250 * MS
    assert tool.time_cursor.value() == pytest.approx(0.25)

    region = tool.region
    assert region is not None and not region.region.isVisible()
    window.analysis.selection.select(100 * MS, 500 * MS)
    assert region.region.isVisible()
    assert region.region.getRegion() == pytest.approx((0.1, 0.5))
    region.region.setRegion((0.2, 0.4))
    region.region.sigRegionChangeFinished.emit(region.region)
    interval = window.analysis.interval
    assert interval is not None
    assert (interval.start_ns, interval.end_ns) == (200 * MS, 400 * MS)
    main = window.analysis_docks.region
    assert main is not None and main.region.getRegion() == pytest.approx((0.2, 0.4))


def test_time_axis_uses_the_main_plot_origin_else_the_session_start(demo):
    window, owner = demo
    select(window, TEMPERATURE)
    tool = open_tool(window)
    time_plot = window.right_panel.time_plot
    assert time_plot.identity == (owner.session, TEMPERATURE)
    assert tool.origin_ns == time_plot.origin_ns == 0
    assert "first reading of csv.column.1, as in the main plot" in tool.hint.text()

    time_plot.origin_ns = 100 * MS  # as if the main plot's signal started later
    tool.follow.setChecked(False)
    tool.refresh()
    assert tool.origin_ns == 100 * MS
    (bars,) = drawn(tool, "BarGraphItem")
    assert list(bars.opts["x0"]) == pytest.approx([-0.1, 0.0, 0.4])
    assert tool.time_cursor.value() == pytest.approx(-0.1)
    window.analysis.selection.select(100 * MS, 500 * MS)
    assert tool.region is not None
    assert tool.region.region.getRegion() == pytest.approx((0.0, 0.4))

    select(window, VALVE)  # a string signal: the main plot shows no time trace
    assert time_plot.identity is None
    tool.refresh()
    assert tool.origin_ns == 0
    assert "seconds since the session start" in tool.hint.text()


def test_pan_and_zoom_requery_the_visible_range_and_follow_the_main_plot(demo):
    window, _ = demo
    select(window, TEMPERATURE)
    tool = open_tool(window)
    main_view = window.right_panel.time_plot.view
    main_view.setXRange(0.2, 0.6, padding=0)
    tool.refresh()
    assert tool.view.viewRange()[0] == pytest.approx([0.2, 0.6])
    assert tool.rows[0].timeline.start_ns == 200 * MS
    assert event_times(tool) == [250 * MS, 500 * MS]
    assert segments(tool) == [(200 * MS, 500 * MS, "open"), (500 * MS, 600 * MS, "closed")]

    tool.view.setXRange(0.3, 0.4, padding=0)
    assert main_view.viewRange()[0] == pytest.approx([0.3, 0.4])
    tool.refresh()
    assert segments(tool) == [(300 * MS, 400 * MS, "open")]

    tool.follow.setChecked(False)
    tool.view.setXRange(0.0, 0.15, padding=0)
    assert main_view.viewRange()[0] == pytest.approx([0.3, 0.4])
    tool.refresh()
    assert event_times(tool) == [0, 100 * MS]
    tool.fit_button.click()
    assert event_times(tool) == [0, 100 * MS, 250 * MS, 500 * MS, 1000 * MS]


@pytest.mark.parametrize(
    ("family", "rows", "labels"),
    [
        ("motion", ["motion.enabled", "motion.notice"], [{"false", "true"}, {"code=tick"}]),
        ("process", ["process.status"], [{"state=idle", "state=ready"}]),
    ],
)
def test_simulator_boolean_status_and_event_signals_need_no_specific_widget(
    qtbot, family, rows, labels
):
    window = create_main_window()
    window.show()
    owner = window.source_owner
    assert isinstance(owner, SourceController)
    try:
        owner.choose(family)
        qtbot.waitUntil(lambda: owner._finished_rendered and window.replay_bar.slider.isEnabled())
        tool = open_tool(window)
        tool.fit()
        assert tool.shown_ids() == rows
        snapshot = window.analysis.snapshot
        assert snapshot is not None
        for index, (signal_id, expected) in enumerate(zip(rows, labels, strict=True)):
            row = tool.rows[index]
            assert row.descriptor.identifier == signal_id
            assert {event.label for event in row.timeline.events} == expected
            assert event_times(tool, index) == [
                record.sample.timestamp_ns for record in snapshot.series[signal_id]
            ]
        if family == "motion":
            # Events are instants: markers and labels, no held state bars.
            assert len(drawn(tool, "BarGraphItem")) == 1
            assert "motion.notice (event)" in tool.hint.text()
            assert "holds at most 3 s" in tool.hint.text()  # 3 periods at 1 Hz
            assert list_item(tool, "motion.speed").checkState() == Qt.CheckState.Unchecked
    finally:
        close(qtbot, window)


def test_signals_are_picked_by_id_and_the_configuration_round_trips(demo):
    window, _ = demo
    tool = open_tool(window)
    assert tool.configuration() == {"signals": None, "max_hold_s": None, "follow_main_plot": True}
    list_item(tool, TEMPERATURE).setCheckState(Qt.CheckState.Checked)
    tool.refresh()
    assert tool.shown_ids() == [VALVE, TEMPERATURE]
    assert segments(tool, 1)[0] == (0, 100 * MS, "unsupported: float 21.5")
    assert "only whole numbers and text are categories" in tool.hint.text()
    config = tool.configuration()
    assert config == {"signals": [VALVE, TEMPERATURE], "max_hold_s": None, "follow_main_plot": True}

    tool.restore_configuration(
        {"signals": [FLOW, "other.signal"], "max_hold_s": 0.05, "follow_main_plot": False}
    )
    assert tool.shown_ids() == [FLOW]
    assert not tool.follow.isChecked()
    assert list_item(tool, VALVE).checkState() == Qt.CheckState.Unchecked
    assert "Not in this session: other.signal" in tool.hint.text()
    assert segments(tool)[:2] == [(0, 50 * MS, "0"), (50 * MS, 100 * MS, "no data")]
    assert tool.configuration()["signals"] == [FLOW, "other.signal"]
    tool.restore_configuration(config)
    assert tool.configuration() == config


@pytest.mark.parametrize(
    "config",
    [
        None,
        {"signals": None},
        {"signals": "abc", "max_hold_s": None, "follow_main_plot": True},
        {"signals": [""], "max_hold_s": None, "follow_main_plot": True},
        {"signals": None, "max_hold_s": -1, "follow_main_plot": True},
        {"signals": None, "max_hold_s": True, "follow_main_plot": True},
        {"signals": None, "max_hold_s": None, "follow_main_plot": "yes"},
    ],
)
def test_invalid_configurations_are_rejected(qtbot, config):
    tool = StateTimelineTool(AnalysisContext())
    qtbot.addWidget(tool)
    with pytest.raises(ValueError):
        tool.restore_configuration(config)
    assert tool.configuration()["signals"] is None


def descriptor(identifier: str, kind: str) -> SignalDescriptor:
    return SignalDescriptor(identifier, identifier, ("test",), kind, "test")


def test_empty_and_large_sessions_stay_bounded(qtbot):
    context = AnalysisContext()
    tool = StateTimelineTool(context)
    qtbot.addWidget(tool)
    tool.resize(800, 300)
    tool.show()
    assert "Load a session" in tool.hint.text() and tool.rows == []

    numeric = TelemetrySession(SignalSchema(1, (descriptor("only.numbers", "numeric"),)))
    numeric.append("only.numbers", SignalSample(0, 1.5))
    context.set_snapshot(numeric, numeric.snapshot())
    tool.refresh()
    assert "no boolean, status, string or event signals" in tool.hint.text()

    count = 200_000
    session = TelemetrySession(
        SignalSchema(1, (descriptor("big.toggle", "boolean"), descriptor("idle.flag", "boolean"))),
        max_batch_events=count,
    )
    session.extend(
        "big.toggle", (SignalSample(index * MS, index % 3 == 0) for index in range(count))
    )
    context.set_snapshot(session, session.snapshot())
    tool.refresh()
    assert tool.shown_ids() == ["big.toggle", "idle.flag"]
    big, idle = tool.rows
    assert len(big.timeline.segments) <= MAX_SEGMENTS
    assert len(big.timeline.events) <= MAX_EVENTS
    assert big.timeline.segments_bounded and big.timeline.events_bounded
    assert big.timeline.sample_count == count
    assert "combined to fit the view" in tool.hint.text()
    # A signal without samples is no data over the whole session, never a state.
    assert {segment.kind for segment in idle.timeline.segments} == {SegmentKind.NO_DATA}

    tool.view.setXRange(1.0, 1.01, padding=0)
    tool.refresh()
    big = tool.rows[0]
    assert not big.timeline.segments_bounded
    assert big.timeline.sample_count == 11
    assert [event.time_ns for event in big.timeline.events][:2] == [1000 * MS, 1001 * MS]

    context.clear()
    tool.refresh()
    assert tool.rows == [] and tool._items == []
    assert not tool.time_cursor.isVisible()
    assert tool.region is None
