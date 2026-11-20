"""Source health in the running app: picker indicator and the Analysis tool dock."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QFileDialog

from lbr_dashboard.app import create_main_window
from lbr_dashboard.core import SimulatorSource, SourceState, example_signals
from lbr_dashboard.core.source_health import MS, ConnectionState, LinkStatus, S
from lbr_dashboard.source_controller import SourceController, SourceOption
from lbr_dashboard.ui.source_picker import NO_HEALTH
from lbr_dashboard.ui.tools.source_health import EMPTY_HINT, HINT, SourceHealthTool

DEMO = Path(__file__).resolve().parents[2] / "sample_data" / "timestamped_demo.csv"


class Clock:
    def __init__(self) -> None:
        self.now = 50 * S

    def __call__(self) -> int:
        return self.now


@pytest.fixture
def app(qtbot):
    window = create_main_window()
    window.show()
    owner = window.source_owner
    assert isinstance(owner, SourceController)
    yield window, owner
    window.close()
    qtbot.waitUntil(lambda: not window.isVisible(), timeout=10000)
    window.deleteLater()


def tool(window) -> SourceHealthTool:
    widget = window.analysis_docks.open("source_health").widget()
    assert isinstance(widget, SourceHealthTool)
    widget.refresh()
    return widget


def rows(window) -> dict[str, str]:
    return dict(tool(window).panel.rows())


def indicator(window) -> str:
    return window.source_picker.health.text()


def connect(window, key: str) -> None:
    picker = window.source_picker
    picker.choices.setCurrentIndex(picker.choices.findData(key))
    picker.connect_button.click()


def test_menu_entry_and_empty_state_before_any_source(app):
    window, _ = app
    labels = [action.text() for action in window.analysis_docks.menu.actions()]
    assert "Source health" in labels
    widget = tool(window)
    assert widget.hint.text() == EMPTY_HINT
    assert widget.panel.rows() == []
    assert indicator(window) == NO_HEALTH
    assert window.analysis.source_health is None


def test_live_simulator_shows_live_health_then_disconnected(app, qtbot):
    window, owner = app
    connect(window, "live")
    qtbot.waitUntil(lambda: "Connected, live" in indicator(window), timeout=10000)
    report = window.analysis.source_health
    assert report is not None and report.health.connection is ConnectionState.CONNECTED
    # The simulator sends a health report every second and declares that heartbeat.
    qtbot.waitUntil(lambda: rows(window)["Heartbeat"].startswith("alive"), timeout=10000)
    values = rows(window)
    assert tool(window).hint.text() == HINT
    assert values["Status"] == "Connected, live"
    assert values["Lost packets"] == "unknown (no sequence numbers)"
    assert values["Decode errors"] == "unknown (not reported)"
    assert values["Dashboard drops"] == "0"
    assert values["Reported: state"] == "running"
    assert values["Signal process.pressure"].endswith("(fresh)")
    assert "loss unknown (no sequence numbers)" in indicator(window)
    # The contract health stays in the session snapshot for other consumers.
    snapshot = window.analysis.snapshot
    assert snapshot is not None and snapshot.source_health["simulator"].state is SourceState.RUNNING

    window.source_picker.disconnect_button.click()
    qtbot.waitUntil(lambda: "Disconnected (stopped)" in indicator(window), timeout=10000)
    assert rows(window)["Status"] == "Disconnected (stopped)"
    assert window.source_picker.health.property("healthStatus") == LinkStatus.DISCONNECTED.value


def test_stalled_live_source_is_connected_but_stale_not_disconnected(app, qtbot):
    window, owner = app
    clock = Clock()
    owner.health_clock = clock

    def pause(token, step: int) -> None:
        token.wait(0.005)

    def prepare(token):
        # Simulated time never advances after the first readings: the link stays
        # up but nothing new arrives, like a sensor bus that went quiet.
        return lambda: SimulatorSource(
            example_signals("process"),
            duration_ns=None,
            realtime=True,
            now_ns=lambda: 0,
            wait=pause,
        )

    owner.register(SourceOption("stalled", "Stalled live simulator", prepare))
    connect(window, "stalled")
    qtbot.waitUntil(lambda: "Connected, live" in indicator(window), timeout=10000)
    live = indicator(window)
    assert "last data 0.0 s ago" in live

    clock.now += 501 * MS
    qtbot.waitUntil(lambda: "Connected, data late" in indicator(window), timeout=5000)
    clock.now += 1500 * MS
    qtbot.waitUntil(lambda: "Connected but stale" in indicator(window), timeout=5000)
    stale = indicator(window)
    assert "last data 2.0 s ago" in stale
    assert window.source_picker.health.property("healthStatus") == LinkStatus.STALE.value
    values = rows(window)
    assert values["Status"] == "Connected but stale"
    assert values["Connection"].startswith("connected for 2.001 s")
    assert values["Heartbeat"].startswith("timed out")
    assert values["Signal process.pressure"].endswith("(stale)")

    window.source_picker.disconnect_button.click()
    qtbot.waitUntil(lambda: "Disconnected (stopped)" in indicator(window), timeout=10000)
    gone = indicator(window)
    assert gone != stale and "Connected" not in gone
    assert rows(window)["Status"] == "Disconnected (stopped)"
    assert window.source_picker.health.property("healthStatus") == LinkStatus.DISCONNECTED.value


def test_csv_replay_shows_unknown_for_metadata_it_does_not_have(app, qtbot, monkeypatch):
    window, _ = app
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(DEMO), ""))
    connect(window, "csv")
    qtbot.waitUntil(lambda: window.replay_bar.slider.isEnabled(), timeout=10000)
    qtbot.waitUntil(lambda: "Disconnected (end of data)" in indicator(window), timeout=10000)
    values = rows(window)
    assert values["Status"] == "Disconnected (end of data)"
    assert values["Lost packets"] == "unknown (no sequence numbers)"
    assert values["Duplicates / out of order"] == "unknown (no sequence numbers)"
    assert values["Heartbeat"] == "unknown (source has no heartbeat)"
    assert values["Decode errors"] == "unknown (not reported)"
    assert values["Reported status"] == "unknown (nothing reported by source)"
    assert int(values["Packets received"]) > 0
