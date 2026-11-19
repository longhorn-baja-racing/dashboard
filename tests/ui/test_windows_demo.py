"""Drive the generic Windows demo through its production controls."""

from pathlib import Path
from threading import Event

import numpy as np
import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QFileDialog

from lbr_dashboard.app import create_main_window
from lbr_dashboard.core import PlaybackMode, SourceState
from lbr_dashboard.source_controller import SourceController, SourceOption
from lbr_dashboard.ui.signal_browser import ID_ROLE

SAMPLES = Path(__file__).resolve().parents[2] / "sample_data"
DEMO = SAMPLES / "fixtures" / "timestamped_demo.csv"


@pytest.fixture
def demo(qtbot):
    window = create_main_window()
    window.show()
    owner = window.source_owner
    assert isinstance(owner, SourceController)
    now = [0]
    owner.replay._now = lambda: now[0]
    yield window, owner, now
    window.close()
    qtbot.waitUntil(lambda: not window.isVisible())
    assert owner.source is None and owner._preparing is None and not owner.timer.isActive()
    window.deleteLater()


def choose(window, key):
    picker = window.source_picker
    picker.choices.setCurrentIndex(picker.choices.findData(key))
    assert picker.connect_button.isEnabled()
    picker.connect_button.click()


def open_csv(window, monkeypatch, path):
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(path), ""))
    choose(window, "csv")


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


def assert_trace(window, signal_id, times, values):
    plot = window.right_panel.time_plot
    assert plot.identity is not None and plot.identity[1] == signal_id
    assert plot.curve is not None
    xs, ys = plot.curve.getData()
    np.testing.assert_allclose(xs, times, rtol=0, atol=1e-12)
    np.testing.assert_array_equal(ys, values)


def test_csv_replay_and_switch_to_seeded_simulator(demo, qtbot, monkeypatch):
    window, owner, now = demo
    open_csv(window, monkeypatch, DEMO)
    qtbot.waitUntil(lambda: window.replay_bar.slider.isEnabled())
    csv = owner.source
    assert csv is not None
    times = [0, 0.1, 0.25, 0.5, 1]
    select(window, "csv.column.2")
    assert_trace(window, "csv.column.2", times, [0, 12, 14, np.nan, 0])
    select(window, "csv.column.3")
    assert window.right_panel.time_plot.curve is None
    assert "string" in window.right_panel.plot_widget.getPlotItem().titleLabel.text
    assert window.right_panel.table.item(1, 1).text() == "open"
    select(window, "csv.column.1")
    assert_trace(window, "csv.column.1", times, [21.5, 22, np.nan, 23.5, 24])
    bar, clock = window.replay_bar, owner.replay.clock
    bar.play.click()
    now[0] = 100_000_000
    owner.poll()
    assert clock.snapshot().selected_ns == 100_000_000
    bar.play.click()
    assert clock.snapshot().mode is PlaybackMode.PAUSED
    now[0] = 800_000_000
    owner.poll()
    assert clock.snapshot().selected_ns == 100_000_000
    bar.slider.setValue(2500)
    assert clock.snapshot().selected_ns == 250_000_000
    assert window.right_panel.selected_reading.text() == "Selected: no available reading"

    choose(window, "process")
    qtbot.waitUntil(lambda: owner.source is not csv and window.replay_bar.slider.isEnabled())
    assert csv.state is SourceState.CLOSED and csv.join(0)
    assert owner.replay.clock is not clock
    assert owner.replay.clock.snapshot().selected_ns == 0
    select(window, "process.temperature")
    plot = window.right_panel.time_plot
    assert plot.identity is not None and plot.identity[1] == "process.temperature"
    assert plot.curve is not None
    xs, ys = plot.curve.getData()
    np.testing.assert_array_equal(xs, [0, 1, 2, 3])
    assert ys is not None and 19 < ys[0] < 21 and ys[1] > ys[2]
    assert plot.plot.getAxis("left").labelUnits == "°C"
    select(window, "process.pressure")
    assert plot.identity is not None and plot.identity[1] == "process.pressure"
    assert plot.curve is not None
    px, py = plot.curve.getData()
    assert px is not None and py is not None
    np.testing.assert_array_equal(px[:4], [0, 0.01, 0.02, 0.03])
    np.testing.assert_array_equal(py[:4], [0, 5, 8, 3])
    assert plot.plot.getAxis("left").labelUnits == "Pa"
    simulator = owner.source
    assert simulator is not None
    window.source_picker.disconnect_button.click()
    qtbot.waitUntil(lambda: owner.source is None)
    assert simulator.join(0) and simulator.state is SourceState.CLOSED
    assert "Disconnected" in window.source_picker.status.text()


def test_switch_from_existing_file_to_generic_replay(demo, qtbot, monkeypatch):
    window, owner, _ = demo
    window.load_csv(SAMPLES / "lbr_demo_session.csv")
    assert window.pages.currentWidget() is window.lbr_view
    open_csv(window, monkeypatch, DEMO)
    qtbot.waitUntil(lambda: window.replay_bar.slider.isEnabled())
    assert window.pages.currentIndex() == 0
    assert window.left_panel.signal_browser.isVisible()
    assert window.right_panel.plot_widget.isVisible()
    assert window.windowTitle() == "LBR Dashboard"
    assert owner.has_session()


def test_failed_and_cancelled_preparation_leave_the_recording_intact(
    demo, qtbot, monkeypatch, tmp_path
):
    window, owner, _ = demo
    open_csv(window, monkeypatch, DEMO)
    qtbot.waitUntil(lambda: window.replay_bar.slider.isEnabled())
    session = owner.session
    path = tmp_path / "broken.csv"
    path.write_text("timestamp_ms,flow\nbad,12\n", encoding="utf-8")
    open_csv(window, monkeypatch, path)
    qtbot.waitUntil(lambda: "Row 2" in window.source_picker.status.text())
    assert owner.session is session

    started, stopped = Event(), Event()

    def prepare(token):
        started.set()
        try:
            token.wait(5)
            token.raise_if_cancelled()
            pytest.fail("test preparation was not cancelled")
        finally:
            stopped.set()

    owner.register(SourceOption("blocked-demo", "Blocked demo", prepare))
    choose(window, "blocked-demo")
    qtbot.waitUntil(started.is_set)
    heartbeat = Event()
    QTimer.singleShot(0, heartbeat.set)
    qtbot.waitUntil(heartbeat.is_set)
    worker = owner._preparing
    assert worker is not None
    window.source_picker.disconnect_button.click()
    qtbot.waitUntil(lambda: owner._preparing is None)
    assert stopped.is_set() and worker.join(0)
    assert owner.session is session and owner.source is None
    assert_trace(window, "csv.column.1", [0, 0.1, 0.25, 0.5, 1], [21.5, 22, np.nan, 23.5, 24])


def test_a_broken_signal_browser_binding_is_detected(demo, qtbot, monkeypatch):
    window, owner, _ = demo
    open_csv(window, monkeypatch, DEMO)
    qtbot.waitUntil(lambda: window.replay_bar.slider.isEnabled())
    window.left_panel.signal_browser.signal_selected.disconnect(owner.select_signal)
    select(window, "csv.column.2")
    with pytest.raises(AssertionError):
        assert_trace(window, "csv.column.2", [0, 0.1, 0.25, 0.5, 1], [0, 12, 14, np.nan, 0])
