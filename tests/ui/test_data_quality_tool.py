"""Analysis > Data quality in the real window with the CSV demo and the simulator."""

from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtWidgets import QFileDialog

import lbr_dashboard.source_controller as source_controller
from lbr_dashboard.app import create_main_window
from lbr_dashboard.core.interval import TimeInterval
from lbr_dashboard.source_controller import SourceController
from lbr_dashboard.ui.signal_browser import ID_ROLE
from lbr_dashboard.ui.tools.data_quality import DataQualityTool

SAMPLES = Path(__file__).resolve().parents[2] / "sample_data"
DEMO = SAMPLES / "timestamped_demo.csv"
TEMPERATURE = "csv.column.1"
FLOW = "csv.column.2"
PRESSURE = "process.pressure"
MS = 1_000_000


def start(window, key: str) -> SourceController:
    owner = window.source_owner
    assert isinstance(owner, SourceController)
    picker = window.source_picker
    picker.choices.setCurrentIndex(picker.choices.findData(key))
    picker.connect_button.click()
    return owner


@pytest.fixture
def window(qtbot):
    window = create_main_window()
    window.show()
    yield window
    window.close()
    qtbot.waitUntil(lambda: not window.isVisible(), timeout=5000)
    window.deleteLater()


@pytest.fixture
def demo(qtbot, window, monkeypatch):
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(DEMO), ""))
    owner = start(window, "csv")
    owner.replay._now = lambda: 0
    qtbot.waitUntil(lambda: window.replay_bar.slider.isEnabled())
    return window


def select_signal(window, signal_id: str) -> None:
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


def open_tool(window) -> DataQualityTool:
    actions = {action.text(): action for action in window.analysis_docks.menu.actions()}
    actions["Data quality"].trigger()
    dock = window.analysis_docks.docks["data_quality"]
    assert dock.isVisible()
    tool = dock.widget()
    assert isinstance(tool, DataQualityTool)
    tool.refresh()
    return tool


def signal_rows(tool: DataQualityTool) -> dict[str, tuple[str, ...]]:
    return {row[0].rsplit("(", 1)[1].rstrip(")"): row for row in tool.rows(tool.signals)}


def finding_rows(tool: DataQualityTool) -> list[tuple[str, ...]]:
    return tool.rows(tool.found)


def pairs(tool: DataQualityTool, table) -> dict[str, str]:
    return {row[0]: row[1] for row in tool.rows(table)}


def source_values(tool: DataQualityTool) -> dict[str, str]:
    return pairs(tool, tool.sources)


def test_csv_demo_reports_missing_readings_at_their_times_and_unknown_completeness(demo):
    tool = open_tool(demo)
    assert "whole session" in tool.hint.text()
    rows = signal_rows(tool)
    # Records, unavailable, expected, gaps, dropped, completeness, evicted.
    assert rows[TEMPERATURE][1:] == (
        "5",
        "1",
        "- (no declared rate)",
        "2 (~3 periods)",
        "unknown (no counter)",
        "unknown",
        "0",
    )
    assert rows[FLOW][2] == "1"
    found = finding_rows(tool)
    unavailable = [row for row in found if row[3] == "unavailable"]
    assert [(row[0], row[1], row[2], row[4]) for row in unavailable] == [
        ("0.250 s", "0.250 s", TEMPERATURE, "recorded"),
        ("0.500 s", "0.500 s", FLOW, "recorded"),
    ]
    gaps = [row for row in found if row[3] == "gap" and row[2] == TEMPERATURE]
    assert [(row[0], row[1], row[4]) for row in gaps] == [
        ("0.250 s", "0.500 s", "inferred"),
        ("0.500 s", "1.000 s", "inferred"),
    ]
    sources = source_values(tool)
    assert sources["State [recorded]"].startswith("ended (complete)")
    assert "not missing data" in sources["State [recorded]"]
    assert sources["Lost before the session [recorded]"].startswith("0 ")
    assert sources["Evicted by retention [retention]"] == "0"


def test_same_interval_gives_reproducible_counts(demo):
    tool = open_tool(demo)
    demo.analysis.selection.select(0, 250 * MS)
    tool.refresh()
    first = (tool.rows(tool.signals), finding_rows(tool), tool.rows(tool.sources))
    tool.refresh()
    assert (tool.rows(tool.signals), finding_rows(tool), tool.rows(tool.sources)) == first
    assert "interval 0.000 s to 0.250 s" in tool.hint.text()
    temperature = signal_rows(tool)[TEMPERATURE]
    # 0, 100 and 250 ms are inside; deltas 100 and 150 ms are not gaps.
    assert temperature[1:5] == ("3", "1", "- (no declared rate)", "0")
    assert tool.whole_session.isEnabled()
    tool.whole_session.click()
    assert demo.analysis.interval is None


def test_clicking_a_gap_selects_it_as_the_shared_interval(qtbot, demo):
    tool = open_tool(demo)
    found = finding_rows(tool)
    row = next(
        index
        for index, cells in enumerate(found)
        if cells[3] == "gap" and cells[2] == TEMPERATURE and cells[0] == "0.500 s"
    )
    tool.found.cellClicked.emit(row, 0)
    assert demo.analysis.interval == TimeInterval(500 * MS, 1000 * MS)
    region = demo.analysis_docks.region
    assert region is not None and region.region.getRegion() == pytest.approx((0.5, 1.0))

    # A real mouse click on the first temperature gap in the timeline.
    tool.refresh()
    timeline = tool.timeline
    report = tool.session_report
    assert report is not None
    row_index = [item.signal_id for item in report.signals].index(TEMPERATURE)
    view = timeline.plot_item.getViewBox()
    scene_point = view.mapViewToScene(QPointF(timeline.time_scale.to_x(375 * MS), row_index))
    widget_point = timeline.mapFromScene(scene_point)
    qtbot.mouseClick(
        timeline.viewport(),
        Qt.MouseButton.LeftButton,
        pos=QPoint(widget_point.x(), widget_point.y()),
    )
    qtbot.waitUntil(lambda: demo.analysis.interval == TimeInterval(250 * MS, 500 * MS))
    assert timeline.region is not None
    assert timeline.region.region.getRegion() == pytest.approx((0.25, 0.5))
    # Clicking empty space selects nothing.
    assert timeline.select_at(timeline.time_scale.to_x(50 * MS), row_index) is None


def test_timeline_follows_the_replay_cursor_and_selected_signal(demo):
    tool = open_tool(demo)
    owner = demo.source_owner
    demo.replay_bar.slider.setValue(2500)
    owner.poll()
    assert tool.timeline.cursor_line.isVisible()
    assert tool.timeline.cursor_line.value() == pytest.approx(0.25)
    select_signal(demo, TEMPERATURE)
    tool.refresh()
    details = pairs(tool, tool.details)
    assert details["Signal"].endswith(f"({TEMPERATURE})")
    assert details["Missing values [recorded]"] == "1"
    assert details["Completeness"] == "unknown (no sequence counter)"
    assert details["Quality label 'missing' [recorded]"] == "1"


def finished(owner: SourceController) -> bool:
    return owner._finished_rendered


def test_finite_simulator_separates_source_loss_from_inferred_gaps(qtbot, window):
    owner = start(window, "process")
    qtbot.waitUntil(lambda: finished(owner), timeout=10000)
    tool = open_tool(window)
    pressure = signal_rows(tool)[PRESSURE]
    assert pressure[1:] == (
        "291",
        "5",
        "301 at 100 Hz, 10 without a record",
        "2 (~10 periods)",
        "unknown (no counter)",
        "unknown",
        "0",
    )
    found = finding_rows(tool)
    pressure_findings = [(row[0], row[1], row[3]) for row in found if row[2] == PRESSURE]
    assert ("0.190 s", "0.250 s", "gap") in pressure_findings
    assert ("0.500 s", "0.540 s", "unavailable") in pressure_findings
    assert ("0.690 s", "0.750 s", "gap") in pressure_findings
    assert ("0.200 s", "0.200 s", "source") in pressure_findings
    sources = source_values(tool)
    # The source reports 5 lost records; the other 5 absent records were stale, not lost.
    assert sources["Lost before the session [recorded]"].startswith("5 ")
    assert sources["Source events [recorded]"] == "3"


def test_live_simulator_reports_retention_eviction_and_disconnect_separately(
    qtbot, window, monkeypatch
):
    real = source_controller.TelemetrySession

    def small_retention(schema, **options):
        return real(schema, **{**options, "max_samples_per_signal": 30})

    monkeypatch.setattr(source_controller, "TelemetrySession", small_retention)
    owner = start(window, "live")
    qtbot.waitUntil(
        lambda: owner.session is not None and owner.session.snapshot().evicted[PRESSURE] > 30,
        timeout=10000,
    )
    tool = open_tool(window)
    pressure = signal_rows(tool)[PRESSURE]
    assert pressure[1] == "30"
    evicted = int(pressure[7].split()[0])
    assert evicted > 30 and pressure[7].endswith("(not data loss)")
    # Evicted records are not unavailable readings or confirmed drops.
    assert int(pressure[2]) <= 5 and pressure[5] == "unknown (no counter)"
    assert "Retention evicted" in tool.hint.text()
    sources = source_values(tool)
    assert "not data loss" in sources["Evicted by retention [retention]"]
    assert sources["State [recorded]"].startswith("running")
    assert int(sources["Lost before the session [recorded]"].split()[0]) >= 5

    window.source_picker.disconnect_button.click()
    qtbot.waitUntil(lambda: owner.source is None, timeout=10000)
    tool.refresh()
    state = source_values(tool)["State [recorded]"]
    assert "no longer delivering" in state and "not missing data" in state
    assert signal_rows(tool)[PRESSURE][1] == "30"
