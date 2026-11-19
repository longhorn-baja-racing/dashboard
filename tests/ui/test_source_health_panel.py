"""Offscreen checks for the source health readout."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from pytestqt.qtbot import QtBot

from lbr_dashboard.core.source_health import (
    S,
    SourceHealthConfig,
    SourceHealthTracker,
    format_source_health,
)
from lbr_dashboard.ui.source_health_panel import EMPTY_MESSAGE, EMPTY_STATUS, SourceHealthPanel


class FakeClock:
    def __init__(self) -> None:
        self.now = 10 * S

    def __call__(self) -> int:
        return self.now


def _panel(qtbot: QtBot) -> SourceHealthPanel:
    panel = SourceHealthPanel()
    qtbot.addWidget(panel)
    panel.show()
    return panel


def _table_rows(panel: SourceHealthPanel) -> list[tuple[str, str]]:
    rows = []
    for index in range(panel.table.rowCount()):
        label, text = panel.table.item(index, 0), panel.table.item(index, 1)
        assert label is not None and text is not None
        rows.append((label.text(), text.text()))
    return rows


def test_panel_starts_empty(qtbot: QtBot) -> None:
    panel = _panel(qtbot)
    assert panel.title.text() == EMPTY_MESSAGE
    assert panel.status.text() == EMPTY_STATUS
    assert panel.rows() == []
    assert panel.table.rowCount() == 0


def test_panel_shows_unknown_for_absent_metadata(qtbot: QtBot) -> None:
    clock = FakeClock()
    tracker = SourceHealthTracker("replay", SourceHealthConfig(), clock=clock)
    tracker.observe_packet()
    panel = _panel(qtbot)
    snapshot = tracker.snapshot()
    panel.show_snapshot(snapshot)
    assert panel.title.text() == "Source: replay"
    assert panel.status.text() == "Status: Live (connection unknown)"
    assert panel.rows() == format_source_health(snapshot)
    assert _table_rows(panel) == panel.rows()
    shown = dict(panel.rows())
    assert shown["Lost packets"] == "unknown (no sequence numbers)"
    assert shown["Heartbeat"] == "unknown (source has no heartbeat)"


@pytest.mark.parametrize(
    ("disconnect", "status", "property_value"),
    [
        pytest.param(False, "Status: Connected but stale", "stale", id="stale"),
        pytest.param(True, "Status: Disconnected (port closed)", "disconnected", id="gone"),
    ],
)
def test_stale_and_disconnected_read_differently(
    qtbot: QtBot, disconnect: bool, status: str, property_value: str
) -> None:
    clock = FakeClock()
    config = SourceHealthConfig(sequence_bits=8, heartbeat_timeout_ns=S)
    tracker = SourceHealthTracker("usb0", config, clock=clock)
    tracker.observe_connect()
    for number in (0, 1, 3):
        tracker.observe_packet(sequence=number)
    clock.now += 3 * S
    if disconnect:
        tracker.observe_disconnect(reason="port closed")
    panel = _panel(qtbot)
    panel.show_snapshot(tracker.snapshot())
    assert panel.status.text() == status
    assert panel.status.property("healthStatus") == property_value
    shown = dict(_table_rows(panel))
    assert shown["Lost packets"] == "1 (25.00% of expected)"
    assert shown["Last packet"].startswith("3.000 s ago (stale;")
    assert shown["Heartbeat"].startswith("timed out:")


def test_panel_updates_and_clears(qtbot: QtBot) -> None:
    clock = FakeClock()
    tracker = SourceHealthTracker("usb0", SourceHealthConfig(), clock=clock)
    panel = _panel(qtbot)
    panel.show_snapshot(tracker.snapshot())
    assert panel.status.text() == "Status: Waiting for data"
    tracker.observe_packet()
    clock.now += S
    panel.show_snapshot(tracker.snapshot())
    assert panel.status.text() == "Status: Data late (connection unknown)"
    assert panel.status.property("healthStatus") == "late"
    panel.clear()
    assert panel.rows() == []
    assert panel.table.rowCount() == 0
    assert panel.title.text() == EMPTY_MESSAGE
    assert panel.status.text() == EMPTY_STATUS
    assert panel.status.property("healthStatus") == "unknown"
