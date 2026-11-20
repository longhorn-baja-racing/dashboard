"""Session markers in the real app: sidecar persistence, plot markers and replay jumps."""

import json
import shutil
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFileDialog

import lbr_dashboard.core.annotations as annotations_module
from lbr_dashboard.app import create_main_window
from lbr_dashboard.core.annotations import sidecar_path
from lbr_dashboard.source_controller import SourceController
from lbr_dashboard.ui.signal_browser import ID_ROLE
from lbr_dashboard.ui.tools.annotations import AnnotationsTool

SAMPLES = Path(__file__).resolve().parents[2] / "sample_data"
DEMO = SAMPLES / "timestamped_demo.csv"
TEMPERATURE = "csv.column.1"
VALVE = "csv.column.3"
MS = 1_000_000


@pytest.fixture
def log(tmp_path):
    path = tmp_path / "run 1.csv"
    shutil.copyfile(DEMO, path)
    return path


@pytest.fixture
def app(qtbot, monkeypatch, log):
    window = create_main_window()
    window.show()
    owner = window.source_owner
    assert isinstance(owner, SourceController)
    owner.replay._now = lambda: 0
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(log), ""))
    yield window, owner
    window.close()
    qtbot.waitUntil(lambda: not window.isVisible())
    window.deleteLater()


def open_csv(qtbot, window, owner):
    previous = owner.session
    picker = window.source_picker
    picker.choices.setCurrentIndex(picker.choices.findData("csv"))
    picker.connect_button.click()
    qtbot.waitUntil(lambda: owner.session is not previous and window.replay_bar.slider.isEnabled())


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


def tool(window) -> AnnotationsTool:
    widget = window.analysis_docks.open("annotations").widget()
    assert isinstance(widget, AnnotationsTool)
    return widget


def listed(widget: AnnotationsTool) -> list[tuple[str, str, str]]:
    table = widget.table
    return [
        tuple(table.item(row, column).text() for column in range(3))  # type: ignore[union-attr]
        for row in range(table.rowCount())
    ]


def saved(log: Path) -> list[dict]:
    return json.loads(sidecar_path(log).read_text(encoding="utf-8"))["annotations"]


def plot_markers(window, widget: AnnotationsTool) -> list:
    items = window.right_panel.plot_widget.getPlotItem().items
    return [line.value() for line in widget.markers if line in items]


def add_at(widget, window, time_ns, note, label=""):
    window.analysis.request_seek(time_ns)
    assert window.analysis.cursor_ns == time_ns
    widget.table.clearSelection()
    widget.label_edit.setText(label)
    widget.note_edit.setText(note)
    widget.add_cursor.click()


def test_add_edit_remove_save_to_the_sidecar_and_never_touch_the_log(qtbot, app, log):
    window, owner = app
    original = log.read_bytes()
    open_csv(qtbot, window, owner)
    select(window, TEMPERATURE)
    widget = tool(window)
    assert "saved to run 1.csv.annotations.json" in widget.status.text()
    assert not sidecar_path(log).exists(), "opening must not create a file"

    add_at(widget, window, 500 * MS, "Brake fade", "driver")
    assert listed(widget) == [("0.500 s", "driver", "Brake fade")]
    assert [(item["time_ns"], item["category"], item["text"]) for item in saved(log)] == [
        (500 * MS, "driver", "Brake fade")
    ]
    assert plot_markers(window, widget) == pytest.approx([0.5])

    widget.table.selectRow(0)
    assert widget.note_edit.text() == "Brake fade"
    widget.note_edit.setText("Brake fade, rear")
    widget.label_edit.setText("")
    widget.apply_edit.click()
    assert listed(widget) == [("0.500 s", "", "Brake fade, rear")]
    assert saved(log)[0]["text"] == "Brake fade, rear"

    window.analysis.request_seek(250 * MS)
    widget.table.selectRow(0)
    widget.move_to_cursor.click()
    assert saved(log)[0]["time_ns"] == 250 * MS
    assert plot_markers(window, widget) == pytest.approx([0.25])

    widget.table.selectRow(0)
    widget.remove.click()
    assert listed(widget) == [] and saved(log) == []
    assert plot_markers(window, widget) == []
    assert log.read_bytes() == original
    assert sorted(path.name for path in log.parent.iterdir()) == [
        "run 1.csv",
        "run 1.csv.annotations.json",
    ]


def test_jump_moves_the_replay_cursor_to_the_exact_marker_time(qtbot, app):
    window, owner = app
    open_csv(qtbot, window, owner)
    select(window, TEMPERATURE)
    widget = tool(window)
    odd = 123_456_789
    add_at(widget, window, odd, "Odd time")
    add_at(widget, window, 900 * MS, "Late")
    window.analysis.request_seek(0)

    widget.table.selectRow(0)
    assert widget.jump_to_selected()
    assert owner.replay.clock.snapshot().selected_ns == odd
    assert window.analysis.cursor_ns == odd
    cursor = owner.replay.cursor
    assert cursor is not None and cursor.value() == pytest.approx(odd / 1e9)

    widget.table.cellDoubleClicked.emit(1, 0)
    assert window.analysis.cursor_ns == 900 * MS


def test_interval_adds_markers_at_both_edges(qtbot, app, log):
    window, owner = app
    open_csv(qtbot, window, owner)
    select(window, TEMPERATURE)
    widget = tool(window)
    assert not widget.add_interval.isEnabled()
    window.analysis.selection.select(100 * MS, 750 * MS)
    assert widget.add_interval.isEnabled()
    widget.note_edit.setText("Hill")
    widget.add_interval.click()
    assert listed(widget) == [
        ("0.100 s", "", "Hill (start)"),
        ("0.750 s", "", "Hill (end)"),
    ]
    assert [item["time_ns"] for item in saved(log)] == [100 * MS, 750 * MS]


def test_reopening_the_log_restores_markers_on_the_list_and_plot(qtbot, app, log):
    window, owner = app
    original = log.read_bytes()
    open_csv(qtbot, window, owner)
    select(window, TEMPERATURE)
    widget = tool(window)
    add_at(widget, window, 100 * MS, "First", "lap 1")
    add_at(widget, window, 1000 * MS, "Last")
    before = listed(widget)

    open_csv(qtbot, window, owner)
    select(window, TEMPERATURE)
    assert listed(widget) == before
    assert plot_markers(window, widget) == pytest.approx([0.1, 1.0])
    widget.table.selectRow(1)
    assert widget.jump_to_selected()
    assert window.analysis.cursor_ns == 1000 * MS
    assert log.read_bytes() == original


def test_markers_return_after_the_plot_is_cleared(qtbot, app):
    window, owner = app
    open_csv(qtbot, window, owner)
    select(window, TEMPERATURE)
    widget = tool(window)
    add_at(widget, window, 500 * MS, "Mark")
    select(window, VALVE)
    assert plot_markers(window, widget) == []
    select(window, TEMPERATURE)
    assert plot_markers(window, widget) == pytest.approx([0.5])


def test_failed_save_keeps_the_previous_file_and_markers(qtbot, app, log, monkeypatch):
    window, owner = app
    open_csv(qtbot, window, owner)
    select(window, TEMPERATURE)
    widget = tool(window)
    add_at(widget, window, 500 * MS, "Kept")
    before = sidecar_path(log).read_bytes()

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(annotations_module.os, "replace", fail)
    add_at(widget, window, 750 * MS, "Lost")
    assert widget.error.isVisible()
    assert "disk full" in widget.error.text() and "were kept" in widget.error.text()
    widget.table.selectRow(0)
    widget.remove.click()
    assert listed(widget) == [("0.500 s", "", "Kept")]
    assert sidecar_path(log).read_bytes() == before
    assert plot_markers(window, widget) == pytest.approx([0.5])
    assert sorted(path.name for path in log.parent.iterdir()) == [
        "run 1.csv",
        "run 1.csv.annotations.json",
    ]

    monkeypatch.undo()
    add_at(widget, window, 750 * MS, "Saved")
    assert not widget.error.isVisible()
    assert [item["text"] for item in saved(log)] == ["Kept", "Saved"]


def test_unreadable_sidecar_is_reported_and_left_untouched(qtbot, app, log):
    window, owner = app
    sidecar_path(log).write_bytes(b"{not json")
    open_csv(qtbot, window, owner)
    widget = tool(window)
    assert widget.error.isVisible() and "invalid JSON" in widget.error.text()
    assert not widget.add_cursor.isEnabled()
    assert widget.add_at_cursor() is None
    assert sidecar_path(log).read_bytes() == b"{not json"


def test_markers_from_changed_log_content_are_kept_and_reattached(qtbot, app, log):
    window, owner = app
    open_csv(qtbot, window, owner)
    select(window, TEMPERATURE)
    widget = tool(window)
    add_at(widget, window, 500 * MS, "Before edit")
    old_sha = json.loads(sidecar_path(log).read_text(encoding="utf-8"))["log"]["sha256"]
    log.write_bytes(log.read_bytes() + b"1100,24.5,1,open\n")

    open_csv(qtbot, window, owner)
    assert "different content" in widget.status.text()
    assert listed(widget) == [("0.500 s", "", "Before edit")]
    add_at(widget, window, 1100 * MS, "After edit")
    document = json.loads(sidecar_path(log).read_text(encoding="utf-8"))
    assert document["log"]["sha256"] != old_sha
    assert [item["text"] for item in document["annotations"]] == ["Before edit", "After edit"]


def test_simulator_markers_stay_in_memory(qtbot, app, tmp_path, monkeypatch):
    window, owner = app
    monkeypatch.chdir(tmp_path)
    picker = window.source_picker
    picker.choices.setCurrentIndex(picker.choices.findData("process"))
    picker.connect_button.click()
    qtbot.waitUntil(lambda: window.replay_bar.slider.isEnabled(), timeout=10_000)
    widget = tool(window)
    assert "kept in memory only" in widget.status.text()
    window.analysis.request_seek(window.analysis.bounds.start_ns + 10 * MS)
    widget.add_cursor.click()
    assert len(listed(widget)) == 1
    assert widget.sidecar is None
    assert sorted(path.name for path in tmp_path.iterdir()) == ["run 1.csv"]


def test_legacy_table_removes_markers_and_session(qtbot, app):
    window, owner = app
    open_csv(qtbot, window, owner)
    select(window, TEMPERATURE)
    widget = tool(window)
    add_at(widget, window, 500 * MS, "Mark")
    window.load_csv(SAMPLES / "sample_baja_log.csv")
    assert window.analysis.plot is None and window.analysis.log_path is None
    assert widget.markers == [] and listed(widget) == []
    assert "Open a CSV file" in widget.status.text()
    assert not widget.add_cursor.isEnabled()
