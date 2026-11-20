"""The Analysis > Export tool and the main plot PNG action in the real app."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QFileDialog, QMessageBox, QProgressDialog

from lbr_dashboard.app import create_main_window
from lbr_dashboard.core import export
from lbr_dashboard.core.export import CSV_COLUMNS
from lbr_dashboard.source_controller import SourceController
from lbr_dashboard.ui.signal_browser import ID_ROLE
from lbr_dashboard.ui.tools.export import ExportTool

SAMPLES = Path(__file__).resolve().parents[2] / "sample_data"
DEMO = SAMPLES / "timestamped_demo.csv"
TEMPERATURE = "csv.column.1"
FLOW = "csv.column.2"
VALVE = "csv.column.3"
MS = 1_000_000

# sample_data/timestamped_demo.csv as raw records: the temperature is missing
# at 250 ms and the flow rate at 500 ms. A flow rate of 0 is a real reading.
TEMPERATURE_ROWS = [
    [TEMPERATURE, "0", "0.000000000", "21.5", "", "true", "measured"],
    [TEMPERATURE, "100000000", "0.100000000", "22.0", "", "true", "measured"],
    [TEMPERATURE, "250000000", "0.250000000", "", "", "false", "missing"],
    [TEMPERATURE, "500000000", "0.500000000", "23.5", "", "true", "measured"],
    [TEMPERATURE, "1000000000", "1.000000000", "24.0", "", "true", "measured"],
]
FLOW_ROWS = [
    [FLOW, "0", "0.000000000", "0.0", "", "true", "measured"],
    [FLOW, "100000000", "0.100000000", "12.0", "", "true", "measured"],
    [FLOW, "250000000", "0.250000000", "14.0", "", "true", "measured"],
    [FLOW, "500000000", "0.500000000", "", "", "false", "missing"],
    [FLOW, "1000000000", "1.000000000", "0.0", "", "true", "measured"],
]
VALVE_ROWS = [
    [VALVE, "0", "0.000000000", "closed", "", "true", "measured"],
    [VALVE, "100000000", "0.100000000", "open", "", "true", "measured"],
    [VALVE, "250000000", "0.250000000", "open", "", "true", "measured"],
    [VALVE, "500000000", "0.500000000", "closed", "", "true", "measured"],
    [VALVE, "1000000000", "1.000000000", "closed", "", "true", "measured"],
]


@pytest.fixture
def demo(qtbot, monkeypatch):
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
    yield window
    window.close()
    qtbot.waitUntil(lambda: not window.isVisible())
    window.deleteLater()


class Dialogs:
    """Stand-ins for the modal dialogs; they record what the user would see."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, save_path: Path | None) -> None:
        self.save_path = save_path
        self.save_calls: list[tuple[object, ...]] = []
        self.answer = QMessageBox.StandardButton.Yes
        self.questions: list[str] = []
        self.warnings: list[str] = []
        monkeypatch.setattr(QFileDialog, "getSaveFileName", self._save)
        monkeypatch.setattr(QMessageBox, "question", self._question)
        monkeypatch.setattr(QMessageBox, "warning", self._warning)

    def _save(self, *args: object) -> tuple[str, str]:
        self.save_calls.append(args)
        return (str(self.save_path) if self.save_path else "", "")

    def _question(self, _parent: object, _title: str, text: str, *_: object) -> object:
        self.questions.append(text)
        return self.answer

    def _warning(self, _parent: object, _title: str, text: str, *_: object) -> object:
        self.warnings.append(text)
        return QMessageBox.StandardButton.Ok


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


def export_tool(window) -> ExportTool:
    dock = window.analysis_docks.open("export")
    tool = dock.widget()
    assert isinstance(tool, ExportTool)
    tool.refresh()
    return tool


def rows(path: Path) -> list[list[str]]:
    with path.open(newline="", encoding="utf-8") as file:
        return list(csv.reader(file))


def metadata(path: Path) -> dict:
    return json.loads(export.metadata_path_for(path).read_text(encoding="utf-8"))


def test_menu_lists_the_export_tool_and_the_plot_image_action(demo):
    labels = [action.text() for action in demo.analysis_docks.menu.actions()]
    assert "Export" in labels
    assert "Save main plot as PNG..." in labels


def test_selected_signal_is_checked_by_default_and_follows_the_browser(demo):
    select(demo, TEMPERATURE)
    tool = export_tool(demo)
    assert tool.signals.count() == len(demo.analysis.descriptors) == 3
    assert tool.checked_ids() == [TEMPERATURE]
    assert "whole session" in tool.scope.text()
    assert "5 records from 1 signals" in tool.scope.text()

    select(demo, FLOW)
    assert tool.checked_ids() == [FLOW]
    tool.set_checked([FLOW, VALVE])
    select(demo, TEMPERATURE)
    assert tool.checked_ids() == [FLOW, VALVE]  # a user choice is kept


def test_interval_export_matches_the_demo_csv_with_missing_kept_missing(
    demo, monkeypatch, tmp_path
):
    select(demo, TEMPERATURE)
    tool = export_tool(demo)
    tool.set_checked([TEMPERATURE, VALVE])
    demo.analysis.selection.select(100 * MS, 500 * MS)
    tool.refresh()
    assert "Interval 0.100 s to 0.500 s" in tool.scope.text()
    assert "either edge are included" in tool.scope.text()
    assert "6 records from 2 signals" in tool.scope.text()
    folder = tmp_path / ("r" + chr(0xE9) + "sultats")
    folder.mkdir()
    dialogs = Dialogs(monkeypatch, folder / "run")  # the suffix is added

    tool.export_button.click()

    path = folder / "run.csv"
    assert sorted(item.name for item in folder.iterdir()) == ["run.csv", "run.csv.meta.json"]
    assert rows(path) == [list(CSV_COLUMNS), *TEMPERATURE_ROWS[1:4], *VALVE_ROWS[1:4]]
    assert rows(path)[2][3:6] == ["", "", "false"]  # missing, never zero
    info = metadata(path)
    assert info["interval"] == {
        "start_ns": 100 * MS,
        "end_ns": 500 * MS,
        "boundaries": "closed",
        "scope": "samples inside the interval",
    }
    assert info["alignment"] == "none"
    assert info["time"]["unit"] == "ns"
    assert [item["signal_id"] for item in info["signals"]] == [TEMPERATURE, VALVE]
    temperature = info["signals"][0]
    assert temperature["display_name"] == "chamber_temperature"
    assert temperature["unit"] == ""
    assert temperature["frame"] == "unspecified"
    assert temperature["timestamp_domain"] == "relative"
    assert (temperature["sample_count"], temperature["missing_count"]) == (3, 1)
    assert info["signals"][1]["data_type"] == "string"
    assert "Exported 6 records" in tool.status.text()
    assert dialogs.questions == [] and dialogs.warnings == []
    assert dialogs.save_calls and dialogs.save_calls[0][-1] == (
        QFileDialog.Option.DontConfirmOverwrite
    )


def test_without_interval_the_whole_session_is_exported(demo, monkeypatch, tmp_path):
    select(demo, FLOW)
    tool = export_tool(demo)
    tool.set_checked([TEMPERATURE, FLOW])
    Dialogs(monkeypatch, tmp_path / "all.csv")

    result = tool.export_selected()

    assert result is not None and result.row_count == 10
    assert rows(tmp_path / "all.csv")[1:] == TEMPERATURE_ROWS + FLOW_ROWS
    info = metadata(tmp_path / "all.csv")
    assert info["interval"]["scope"] == "all samples"
    assert info["interval"]["start_ns"] is None and info["interval"]["end_ns"] is None
    assert info["source"]


def test_nothing_checked_or_dialog_cancelled_writes_nothing(demo, monkeypatch, tmp_path):
    select(demo, TEMPERATURE)
    tool = export_tool(demo)
    dialogs = Dialogs(monkeypatch, None)
    assert tool.export_selected() is None
    assert dialogs.save_calls

    tool.set_checked([])
    assert not tool.export_button.isEnabled()
    assert tool.export_selected() is None
    assert "Check at least one signal" in tool.status.text()
    assert list(tmp_path.iterdir()) == []


def test_cancelled_export_leaves_no_file(demo, monkeypatch, tmp_path):
    select(demo, TEMPERATURE)
    tool = export_tool(demo)
    tool.set_checked([TEMPERATURE, FLOW, VALVE])
    Dialogs(monkeypatch, tmp_path / "run.csv")
    monkeypatch.setattr(export, "CHUNK_ROWS", 2)
    monkeypatch.setattr(QProgressDialog, "wasCanceled", lambda self: True)

    assert tool.export_selected() is None

    assert list(tmp_path.iterdir()) == []
    assert "Export cancelled" in tool.status.text()
    assert tool.export_button.isEnabled()


def test_failed_export_reports_the_error_and_leaves_no_partial_file(demo, monkeypatch, tmp_path):
    select(demo, TEMPERATURE)
    tool = export_tool(demo)
    dialogs = Dialogs(monkeypatch, tmp_path / "missing folder" / "run.csv")

    assert tool.export_selected() is None
    assert len(dialogs.warnings) == 1
    assert "No export file was written" in dialogs.warnings[0]
    assert "Export failed" in tool.status.text()
    assert list(tmp_path.iterdir()) == []

    def disk_full(*_args):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(export.os, "fsync", disk_full)
    dialogs.save_path = tmp_path / "full.csv"
    assert tool.export_selected() is None
    assert "No space left" in dialogs.warnings[1]
    assert list(tmp_path.iterdir()) == []


def test_existing_files_need_confirmation(demo, monkeypatch, tmp_path):
    select(demo, TEMPERATURE)
    tool = export_tool(demo)
    path = tmp_path / "run.csv"
    export.metadata_path_for(path).write_text("old meta", encoding="utf-8")
    dialogs = Dialogs(monkeypatch, path)
    dialogs.answer = QMessageBox.StandardButton.No

    assert tool.export_selected() is None
    assert "run.csv.meta.json" in dialogs.questions[0]
    assert not path.exists()
    assert export.metadata_path_for(path).read_text(encoding="utf-8") == "old meta"
    assert "existing files were kept" in tool.status.text()

    dialogs.answer = QMessageBox.StandardButton.Yes
    assert tool.export_selected() is not None
    assert rows(path)[1:] == TEMPERATURE_ROWS
    assert metadata(path)["row_count"] == 5


def test_save_main_plot_as_png_with_axes_title_and_legend(demo, monkeypatch, tmp_path):
    select(demo, TEMPERATURE)
    plot = demo.right_panel.plot_widget
    plot.resize(640, 400)
    item = plot.getPlotItem()
    assert item is not None
    assert item.titleLabel.text == "chamber_temperature"
    assert item.getAxis("bottom").labelText == "Measurement time since first reading"
    assert [data.name() for data in item.listDataItems()] == ["chamber_temperature"]
    target = tmp_path / ("plot " + chr(0xE9))
    dialogs = Dialogs(monkeypatch, target)

    demo.plot_image.action.trigger()

    path = target.with_name(target.name + ".png")
    image = QImage(str(path))
    assert not image.isNull()
    assert image.width() > 100 and image.height() > 100
    assert path.read_bytes().startswith(b"\x89PNG")
    assert item.legend is None  # the legend was added for the image only
    assert "Saved the main plot" in demo.statusBar().currentMessage()
    assert dialogs.warnings == []

    dialogs.answer = QMessageBox.StandardButton.No
    before = path.read_bytes()
    dialogs.save_path = path
    assert demo.plot_image.save() is None
    assert path.read_bytes() == before and len(dialogs.questions) == 1


def test_plot_image_failure_is_reported(demo, monkeypatch, tmp_path):
    select(demo, TEMPERATURE)
    dialogs = Dialogs(monkeypatch, tmp_path / "missing" / "plot.png")
    assert demo.plot_image.save() is None
    assert len(dialogs.warnings) == 1
    assert "No image was written" in dialogs.warnings[0]
    assert list(tmp_path.iterdir()) == []
