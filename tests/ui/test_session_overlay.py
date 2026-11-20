"""Analysis > Compare sessions in the real window: a second run on the main timeline."""

from pathlib import Path

import pytest
from PySide6.QtWidgets import QFileDialog

from lbr_dashboard.app import create_main_window
from lbr_dashboard.core.session_alignment import Side
from lbr_dashboard.source_controller import SourceController
from lbr_dashboard.ui.tools.session_overlay import SessionOverlayTool

SAMPLES = Path(__file__).resolve().parents[2] / "sample_data"
DEMO = SAMPLES / "timestamped_demo.csv"
TEMPERATURE = "csv.column.1"
FLOW = "csv.column.2"
MS = 1_000_000

# The demo run recorded 200 ms later, 1 degree warmer, with one extra reading
# at 375 ms (a different sample rate). Flow is unchanged except that reading.
SHIFTED = """timestamp_ms,chamber_temperature,flow_rate,valve_state
200,22.5,0,closed
300,23,12,open
375,23.25,13,open
450,,14,open
700,24.5,,closed
1200,25,0,closed
"""


@pytest.fixture
def demo(qtbot, monkeypatch, tmp_path):
    window = create_main_window()
    window.show()
    owner = window.source_owner
    assert isinstance(owner, SourceController)
    owner.replay._now = lambda: 0
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(DEMO), ""))
    picker = window.source_picker
    picker.choices.setCurrentIndex(picker.choices.findData("csv"))
    picker.connect_button.click()
    qtbot.waitUntil(lambda: window.replay_bar.slider.isEnabled())
    shifted = tmp_path / "shifted_run.csv"
    shifted.write_text(SHIFTED, encoding="utf-8")
    yield window, owner, shifted
    window.close()
    qtbot.waitUntil(lambda: not window.isVisible())
    window.deleteLater()


def open_tool(window) -> SessionOverlayTool:
    tool = window.analysis_docks.open("compare_sessions").widget()
    assert isinstance(tool, SessionOverlayTool)
    return tool


def load_file(qtbot, monkeypatch, tool: SessionOverlayTool, path: Path) -> None:
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(path), ""))
    tool.source_choice.setCurrentIndex(tool.source_choice.findData("file"))
    tool.load_button.click()
    qtbot.waitUntil(lambda: tool.comparison is not None and not tool.loading)


def move_cursor(window, owner, time_ms: int) -> None:
    window.replay_bar.slider.setValue(time_ms * 10)  # 10000 steps over 1000 ms
    owner.poll()


def curve_x(tool: SessionOverlayTool, side: Side, reference_id: str) -> list[float]:
    xs, _ = tool.curves[(side, reference_id)].getData()
    assert xs is not None
    return [round(float(x), 6) for x in xs]


def readout(tool: SessionOverlayTool) -> dict[str, tuple[str, ...]]:
    return {row[0]: row[1:] for row in tool.readout_rows()}


def test_comparison_loads_beside_the_main_session_and_matches_by_id(qtbot, monkeypatch, demo):
    window, owner, shifted = demo
    session, snapshot = owner.session, window.analysis.snapshot
    labels = [action.text() for action in window.analysis_docks.menu.actions()]
    assert "Compare sessions" in labels
    tool = open_tool(window)
    assert "Load a comparison run" in tool.hint.text()
    load_file(qtbot, monkeypatch, tool, shifted)

    assert owner.session is session and window.analysis.snapshot is snapshot
    assert "shifted_run.csv (2 numeric signals, 12 readings)" in tool.status.text()
    assert [(m.reference_signal_id, m.compared_signal_id) for m in tool.matches] == [
        (TEMPERATURE, TEMPERATURE),
        (FLOW, FLOW),
    ]
    assert tool.match_combo(TEMPERATURE).currentData() == TEMPERATURE


def test_both_runs_are_drawn_at_their_mapped_times(qtbot, monkeypatch, demo):
    window, _, shifted = demo
    tool = open_tool(window)
    load_file(qtbot, monkeypatch, tool, shifted)
    assert curve_x(tool, Side.REFERENCE, TEMPERATURE) == [0, 0.1, 0.25, 0.5, 1.0]
    assert curve_x(tool, Side.COMPARED, TEMPERATURE) == [0.2, 0.3, 0.375, 0.45, 0.7, 1.2]
    assert "Overlap: 0.200 s to 1.000 s (0.800 s)" in tool.messages.text()

    tool.offset.setValue(-200)
    assert tool.offset_ns == -200 * MS
    assert curve_x(tool, Side.COMPARED, TEMPERATURE) == [0, 0.1, 0.175, 0.25, 0.5, 1.0]
    assert curve_x(tool, Side.REFERENCE, TEMPERATURE) == [0, 0.1, 0.25, 0.5, 1.0]
    assert "Overlap: 0.000 s to 1.000 s (1.000 s)" in tool.messages.text()
    assert tool.overlap_region.isVisible()
    assert tool.overlap_region.getRegion() == pytest.approx((0.0, 1.0))
    _, values = tool.curves[(Side.COMPARED, TEMPERATURE)].getData()
    assert values is not None
    assert [round(float(v), 6) for v in values[:3]] == [22.5, 23, 23.25]


def test_legend_names_run_signal_and_unit_and_runs_can_be_hidden(qtbot, monkeypatch, demo):
    window, _, shifted = demo
    tool = open_tool(window)
    load_file(qtbot, monkeypatch, tool, shifted)
    assert tool.legend_texts() == [
        "Main run (main session): chamber_temperature [unit unspecified] - csv.column.1",
        "Comparison run (shifted_run.csv): chamber_temperature [unit unspecified] - csv.column.1",
        "Main run (main session): flow_rate [unit unspecified] - csv.column.2",
        "Comparison run (shifted_run.csv): flow_rate [unit unspecified] - csv.column.2",
    ]
    reference = tool.curves[(Side.REFERENCE, TEMPERATURE)]
    compared = tool.curves[(Side.COMPARED, TEMPERATURE)]
    assert reference.opts["pen"].style() != compared.opts["pen"].style()
    assert reference.opts["pen"].color() == compared.opts["pen"].color()

    tool.show_compared.setChecked(False)
    assert reference.isVisible() and not compared.isVisible()
    tool.show_compared.setChecked(True)
    tool.set_pair_visible(FLOW, False)
    assert not tool.curves[(Side.REFERENCE, FLOW)].isVisible()
    assert not tool.curves[(Side.COMPARED, FLOW)].isVisible()
    assert compared.isVisible()


def test_cursor_values_and_differences_match_the_reference_data(qtbot, monkeypatch, demo):
    window, owner, shifted = demo
    tool = open_tool(window)
    load_file(qtbot, monkeypatch, tool, shifted)
    tool.offset.setValue(-200)

    move_cursor(window, owner, 500)
    assert window.analysis.cursor_ns == 500 * MS
    assert "comparison run recording time 0.700 s" in tool.cursor_label.text()
    rows = readout(tool)
    assert rows[TEMPERATURE] == ("23.5", "24.5", "+1")
    assert rows[FLOW] == ("--", "--", "--")  # Both readings at the cursor are missing.
    assert tool.cursor_line.isVisible() and tool.cursor_line.value() == pytest.approx(0.5)

    move_cursor(window, owner, 200)
    values = {row.match.reference_signal_id: row for row in tool.cursor_values()}
    # Main: 22 at 100 ms. Comparison: 23.25 at 375 ms recording time (175 ms mapped).
    assert (values[TEMPERATURE].reference, values[TEMPERATURE].compared) == (22, 23.25)
    assert values[TEMPERATURE].difference == pytest.approx(1.25)
    assert values[FLOW].difference == pytest.approx(1)

    move_cursor(window, owner, 1000)
    rows = readout(tool)
    assert rows[TEMPERATURE] == ("24", "25", "+1")
    assert rows[FLOW] == ("0", "0", "+0")


def test_align_at_cursor_starts_the_comparison_run_at_the_cursor(qtbot, monkeypatch, demo):
    window, owner, shifted = demo
    tool = open_tool(window)
    load_file(qtbot, monkeypatch, tool, shifted)
    move_cursor(window, owner, 100)
    tool.align_button.click()
    assert tool.offset_ns == -100 * MS
    assert tool.offset.value() == pytest.approx(-100)
    assert curve_x(tool, Side.COMPARED, TEMPERATURE)[0] == pytest.approx(0.1)


def test_no_overlap_is_explained(qtbot, monkeypatch, demo):
    window, _, shifted = demo
    tool = open_tool(window)
    load_file(qtbot, monkeypatch, tool, shifted)
    tool.offset.setValue(5000)
    text = tool.messages.text()
    assert "No overlap: the comparison run starts 4.200 s after the main run ends." in text
    assert "comparison run 5.200 s to 6.200 s" in text
    assert "Decrease the offset by at least 4200 ms." in text
    assert not tool.overlap_region.isVisible()
    tool.offset.setValue(-1300)
    assert "the comparison run ends 0.100 s before the main run starts" in tool.messages.text()


def test_unit_mismatch_and_missing_signals_are_reported(qtbot, monkeypatch, demo):
    window, owner, _ = demo
    tool = open_tool(window)
    tool.source_choice.setCurrentIndex(tool.source_choice.findData("preset:process"))
    tool.load_button.click()
    qtbot.waitUntil(lambda: tool.comparison is not None and not tool.loading)
    assert tool.matches == []
    assert "No signals are matched" in tool.messages.text()

    combo = tool.match_combo(TEMPERATURE)
    combo.setCurrentIndex(combo.findData("process.temperature"))
    assert any(
        line.startswith("Unit mismatch: units differ for 'csv.column.1' and 'process.temperature'")
        and "main run (none), comparison run '" in line
        for line in tool.messages.text().splitlines()
    )
    move_cursor(window, owner, 500)
    assert readout(tool)["csv.column.1 vs process.temperature"][2] == "-- (units differ)"

    tool.restore_configuration(
        {
            **tool.configuration(),
            "alignment": {
                **tool.configuration()["alignment"],  # type: ignore[dict-item]
                "matches": [{"reference": TEMPERATURE, "compared": "process.humidity"}],
            },
        }
    )
    qtbot.waitUntil(lambda: not tool.loading)
    assert (
        "Missing signal: signal 'process.humidity' is not in comparison run 'simulator'."
        in tool.messages.text()
    )
    assert tool.match_combo(TEMPERATURE).currentText() == "missing: process.humidity"


def test_configuration_round_trip_restores_file_matches_offset_and_visibility(
    qtbot, monkeypatch, demo
):
    window, _, shifted = demo
    tool = open_tool(window)
    load_file(qtbot, monkeypatch, tool, shifted)
    tool.set_match(FLOW, None)
    tool.offset.setValue(-200)
    tool.show_reference.setChecked(False)
    tool.set_pair_visible(TEMPERATURE, False)
    saved = tool.configuration(relative_to=shifted.parent)
    assert saved["comparison"] == {"path": "shifted_run.csv"}
    alignment = saved["alignment"]
    assert isinstance(alignment, dict)
    assert alignment["offset_ns"] == -200 * MS
    assert alignment["matches"] == [{"reference": TEMPERATURE, "compared": TEMPERATURE}]
    assert alignment["compared"]["label"] == "shifted_run.csv"

    tool.restore_configuration({"comparison": None})
    assert tool.comparison is None and tool.offset_ns == 0 and not tool.curves

    tool.restore_configuration(saved, base_dir=shifted.parent)
    qtbot.waitUntil(lambda: tool.comparison is not None and not tool.loading)
    assert tool.configuration(relative_to=shifted.parent) == saved
    assert tool.offset.value() == pytest.approx(-200)
    assert set(tool.curves) == {(Side.REFERENCE, TEMPERATURE), (Side.COMPARED, TEMPERATURE)}
    assert not tool.show_reference.isChecked()
    assert curve_x(tool, Side.COMPARED, TEMPERATURE)[0] == 0


def test_invalid_configuration_changes_nothing(qtbot, monkeypatch, demo):
    window, _, shifted = demo
    tool = open_tool(window)
    load_file(qtbot, monkeypatch, tool, shifted)
    saved = tool.configuration()
    with pytest.raises(ValueError, match="offset_ns must be an integer"):
        tool.restore_configuration(
            {**saved, "alignment": {**saved["alignment"], "offset_ns": 1.5}}  # type: ignore[dict-item]
        )
    with pytest.raises(ValueError, match="comparison must be"):
        tool.restore_configuration({**saved, "comparison": {"url": "x"}})
    assert tool.configuration() == saved


def test_unreadable_comparison_keeps_the_main_session(qtbot, monkeypatch, demo, tmp_path):
    window, owner, _ = demo
    session = owner.session
    broken = tmp_path / "broken.csv"
    broken.write_text("time,value\n1,2\n", encoding="utf-8")
    tool = open_tool(window)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(broken), ""))
    tool.load_button.click()
    qtbot.waitUntil(lambda: not tool.loading)
    assert "Could not load broken.csv" in tool.status.text()
    assert "timestamp_ms" in tool.status.text()
    assert tool.comparison is None and owner.session is session
