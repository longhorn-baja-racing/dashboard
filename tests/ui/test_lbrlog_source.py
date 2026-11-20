"""Open a firmware session log (.lbrlog) through the production source picker."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path
from threading import Event

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFileDialog

from lbr_dashboard.app import create_main_window
from lbr_dashboard.core import CancellationToken, SourceState
from lbr_dashboard.source_controller import FileSourceOption, SourceController

HEX = Path(__file__).resolve().parents[1] / "fixtures" / "lbr" / "session.lbr.hex"
GOLDEN = bytes.fromhex(HEX.read_text())


@pytest.fixture
def picker(qtbot):
    window = create_main_window()
    window.show()
    owner = window.source_owner
    assert isinstance(owner, SourceController)
    yield window, owner
    owner.disconnect()
    qtbot.waitUntil(lambda: owner.source is None and owner._preparing is None)
    window.close()
    window.deleteLater()


def wait_done(qtbot, owner):
    qtbot.waitUntil(
        lambda: (
            owner.source is not None
            and owner._preparing is None
            and not owner._releasing
            and owner.source.state is SourceState.ENDED
            and owner.source.events.stats.buffered == 0
            and owner._finished_rendered
        ),
        timeout=10_000,
    )
    assert owner.session is not None
    return owner.session.snapshot()


def test_firmware_log_opens_from_the_picker_and_reports_damage(
    picker, qtbot, tmp_path, monkeypatch
):
    window, owner = picker
    path = tmp_path / "00000012.lbrlog"
    path.write_bytes(GOLDEN)
    original = path.read_bytes()
    choices = window.source_picker.choices
    index = choices.findData("lbrlog")
    assert index >= 0 and choices.itemText(index) == "Firmware log (.lbrlog)"
    filters: list[str] = []

    def dialog(parent, caption, directory, file_filter):
        filters.append(file_filter)
        return str(path), ""

    monkeypatch.setattr(QFileDialog, "getOpenFileName", dialog)
    choices.setCurrentIndex(index)
    qtbot.mouseClick(window.source_picker.connect_button, Qt.MouseButton.LeftButton)
    snapshot = wait_done(qtbot, owner)
    assert "*.lbrlog" in filters[0]
    brake = snapshot.series["driver_input.brake_pedal"]
    assert [(r.sample.timestamp_ns, r.sample.value) for r in brake] == [(50_330_000_000, 12.5)]
    temp = snapshot.series["temperature.temp_cvt"][0].sample
    assert (temp.available, temp.value, temp.quality) == (False, None, "NOT_FITTED")
    assert snapshot.series["stress.stress_3.node-clock"][0].sample.timestamp_domain == (
        "lbr_node_clock.stress"
    )
    status = window.source_picker.status.text()
    assert "Firmware log (.lbrlog)" in status and "ended" in status
    assert "Log read with problems" in status and "(offset 688)" in status
    assert "dropped 5 record(s)" in status
    owner.select_signal("driver_input.brake_pedal")
    plot = window.right_panel.plot_widget.getPlotItem()
    assert plot is not None
    qtbot.waitUntil(lambda: len(plot.listDataItems()) == 1)
    assert path.read_bytes() == original


def test_unreadable_log_is_refused_and_previous_readings_remain(picker, qtbot, tmp_path):
    window, owner = picker
    good = tmp_path / "good.lbrlog"
    good.write_bytes(GOLDEN)
    owner.open_file("lbrlog", good)
    wait_done(qtbot, owner)
    session = owner.session
    newer = bytearray(GOLDEN[:84])
    newer[6:8] = struct.pack("<H", 2)
    newer[80:84] = struct.pack("<I", zlib.crc32(bytes(newer[2:80])))
    bad = tmp_path / "newer.lbrlog"
    bad.write_bytes(bytes(newer))
    owner.open_file("lbrlog", bad)
    qtbot.waitUntil(lambda: owner._preparing is None and "Could not open" in owner._notice)
    status = window.source_picker.status.text()
    assert "This log uses .lbrlog format version 2; this dashboard reads version 1" in status
    assert owner.session is session and owner.source is not None


def test_cancelled_dialog_starts_nothing(picker, monkeypatch):
    _, owner = picker
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: ("", ""))
    owner.choose("lbrlog")
    assert owner._preparing is None and owner.source is None


def test_progress_is_shown_and_disconnect_cancels_the_read(picker, qtbot, tmp_path):
    window, owner = picker
    released = Event()

    def prepare(path: Path, token: CancellationToken, progress):
        try:
            progress(50, 100)
            while not token.is_cancelled:
                Event().wait(0.01)
            token.raise_if_cancelled()
        finally:
            released.set()
        raise AssertionError("unreachable")

    owner.register_file(FileSourceOption("slow", "Slow log", "Logs (*.log)", prepare))
    with pytest.raises(ValueError, match="already registered"):
        owner.register_file(FileSourceOption("slow", "Slow log", "Logs (*.log)", prepare))
    owner.open_file("slow", tmp_path / "big.log")
    qtbot.waitUntil(lambda: "50% read" in window.source_picker.status.text())
    assert "Disconnect cancels" in window.source_picker.status.text()
    qtbot.mouseClick(window.source_picker.disconnect_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: owner._preparing is None)
    assert released.is_set() and owner.source is None
    assert "% read" not in window.source_picker.status.text()
