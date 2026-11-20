"""Live USB serial source and recording through the production window (fake serial line)."""

from __future__ import annotations

from pathlib import Path
from runpy import run_path

import pytest
from PySide6.QtCore import Qt

from lbr_dashboard.app import create_main_window
from lbr_dashboard.core import SourceState
from lbr_dashboard.importers.lbrlog import read_lbrlog
from lbr_dashboard.importers.serial_source import SerialLiveSource, SerialPortInfo
from lbr_dashboard.source_controller import SourceController

TESTS = Path(__file__).parents[1]
FAKE = run_path(str(TESTS / "source_contract" / "fake_serial.py"))
FakeLine = FAKE["FakeLine"]
drive_stream = FAKE["drive_stream"]
COM7 = SerialPortInfo("COM7", "Silicon Labs CP210x USB to UART Bridge", "Silicon Labs", True)
BRAKE = ("driver_input.brake_pedal", "driver_input.brake_pedal.node-clock")


@pytest.fixture
def app(qtbot):
    window = create_main_window()
    window.show()
    owner = window.source_owner
    assert isinstance(owner, SourceController)
    yield window, owner
    owner.disconnect()
    qtbot.waitUntil(lambda: owner.source is None and owner._preparing is None)
    window.close()
    window.deleteLater()


def connect(qtbot, window, owner, line) -> None:
    owner.list_serial_ports = lambda: [COM7]
    owner.open_serial_port = line.opener
    owner.serial_discovery_s = 0
    picker = window.source_picker
    picker.choices.setCurrentIndex(picker.choices.findData("serial"))
    assert picker.selected_port() == "COM7"
    qtbot.mouseClick(picker.connect_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(
        lambda: (
            owner.source is not None
            and owner.source.state is SourceState.RUNNING
            and line.open_ports == 1
        ),
        timeout=5_000,
    )


def brake(owner) -> list[tuple[int, float]]:
    snapshot = owner.session.snapshot()
    return sorted(
        (r.sample.timestamp_ns, r.sample.value) for key in BRAKE for r in snapshot.series[key]
    )


def test_serial_choice_lists_ports_and_explains_when_there_are_none(app, qtbot) -> None:
    window, owner = app
    ports: list[SerialPortInfo] = []
    owner.list_serial_ports = lambda: list(ports)
    picker = window.source_picker
    assert "USB/serial is not available" not in picker.controls.text()
    index = picker.choices.findData("serial")
    assert picker.choices.itemText(index) == "USB serial (firmware live)"
    assert not picker.port_row.isVisible()
    picker.choices.setCurrentIndex(index)
    assert picker.port_row.isVisible()
    assert not picker.connect_button.isEnabled()
    assert "No serial ports found" in picker.port_note.text()
    assert "No serial ports found" in picker.connect_button.toolTip()
    ports.append(COM7)
    qtbot.mouseClick(picker.refresh_ports_button, Qt.MouseButton.LeftButton)
    assert picker.connect_button.isEnabled()
    assert picker.ports.itemText(0) == (
        "COM7 (Silicon Labs CP210x USB to UART Bridge - Silicon Labs)"
    )
    assert "never sends" in picker.port_note.text()
    assert not picker.record_button.isEnabled()
    picker.choices.setCurrentIndex(picker.choices.findData("csv"))
    assert not picker.port_row.isVisible() and picker.connect_button.isEnabled()


def test_busy_port_is_reported_before_the_session_starts(app, qtbot) -> None:
    window, owner = app
    line = FakeLine()
    line.fail_open = "access denied; close any other program using the port"
    owner.list_serial_ports = lambda: [COM7]
    owner.open_serial_port = line.opener
    owner.serial_discovery_s = 0.05
    picker = window.source_picker
    picker.choices.setCurrentIndex(picker.choices.findData("serial"))
    qtbot.mouseClick(picker.connect_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: "Could not open source" in picker.status.text(), timeout=5_000)
    assert "access denied" in picker.status.text()
    assert "Check the port and retry" in picker.status.text()
    assert owner.source is None and line.open_ports == 0


def test_live_stream_recording_unplug_and_replay(app, qtbot, tmp_path) -> None:
    window, owner = app
    picker = window.source_picker
    line = FakeLine()
    connect(qtbot, window, owner, line)
    assert "Live source" in picker.controls.text()
    qtbot.waitUntil(lambda: picker.record_button.isEnabled())
    owner.recording.choose_path = lambda suggested: tmp_path / "capture"
    qtbot.mouseClick(picker.record_button, Qt.MouseButton.LeftButton)
    assert picker.record_button.isChecked()
    assert picker.record_button.text() == "Stop recording"
    for chunk in drive_stream(1, 50_000, 30):
        line.send(chunk)
    qtbot.waitUntil(lambda: len(brake(owner)) == 30, timeout=5_000)
    qtbot.waitUntil(
        lambda: (
            "Recording to capture.lbrlog: " in picker.recording.text()
            and " records; no loss" in picker.recording.text()
        ),
        timeout=5_000,
    )
    assert "COM7: 30 packets, 0 lost, 0 rejected frames" in picker.status.text()
    plot = window.right_panel.plot_widget.getPlotItem()
    assert plot is not None
    live = brake(owner)

    line.unplug()
    qtbot.waitUntil(lambda: owner.source.state is SourceState.FAILED, timeout=5_000)
    qtbot.waitUntil(lambda: picker.recording.text().startswith("Saved capture.lbrlog"))
    text = picker.recording.text()
    assert "the source disconnected" in text and "not the SD card log" in text
    assert not picker.record_button.isChecked() and not picker.record_button.isEnabled()
    assert line.open_ports == 0
    status = picker.status.text()
    assert "disconnected" in status and "Retry" in status and "stale" in status
    assert brake(owner) == live  # readings stay for inspection
    path = tmp_path / "capture.lbrlog"
    log = read_lbrlog(path)
    assert log.integrity.records_written is not None and log.integrity.problems() == ()

    owner.open_file("lbrlog", path)
    qtbot.waitUntil(
        lambda: (
            owner.source is not None
            and owner.source.state is SourceState.ENDED
            and owner._finished_rendered
        ),
        timeout=10_000,
    )
    assert brake(owner) == live


def test_closing_the_window_finalizes_the_recording(qtbot, tmp_path) -> None:
    window = create_main_window()
    window.show()
    owner = window.source_owner
    assert isinstance(owner, SourceController)
    line = FakeLine()
    connect(qtbot, window, owner, line)
    owner.recording.choose_path = lambda suggested: tmp_path / "closing.lbrlog"
    qtbot.waitUntil(lambda: window.source_picker.record_button.isEnabled())
    window.source_picker.record_button.click()
    for chunk in drive_stream(1, 60_000, 10):
        line.send(chunk)
    source = owner.source
    assert isinstance(source, SerialLiveSource)
    qtbot.waitUntil(lambda: source.link_status().packets == 10, timeout=5_000)
    window.close()
    qtbot.waitUntil(lambda: owner.source is None and not window.isVisible(), timeout=5_000)
    assert line.open_ports == 0
    log = read_lbrlog(tmp_path / "closing.lbrlog")
    assert log.integrity.records_written is not None, "the recording was not finalized"
    assert log.channel("driver_input", "sample", "BRAKE_PEDAL").count >= 1
    window.deleteLater()
