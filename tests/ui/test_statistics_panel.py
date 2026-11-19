"""Offscreen checks for the mock statistics readout."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pytestqt.qtbot import QtBot

from lbr_dashboard.core.statistics import (
    WEIGHTING_TEXT,
    current_value,
    format_statistics,
    interval_statistics,
)
from lbr_dashboard.ui.statistics_panel import EMPTY_MESSAGE, StatisticsPanel

S = 1_000_000_000
TIMES = [0, S, 2 * S]
VALUES = [-2.0, 4.0, None]


def _panel(qtbot: QtBot) -> StatisticsPanel:
    panel = StatisticsPanel()
    qtbot.addWidget(panel)
    panel.show()
    return panel


def test_panel_starts_empty(qtbot: QtBot) -> None:
    panel = _panel(qtbot)
    assert panel.title.text() == EMPTY_MESSAGE
    assert panel.rows() == []


def test_panel_shows_statistics_and_definitions(qtbot: QtBot) -> None:
    panel = _panel(qtbot)
    stats = interval_statistics(TIMES, VALUES, 0, 2 * S, unit="degC")
    panel.show_statistics(stats)
    rows = panel.rows()
    assert rows == format_statistics(stats)
    shown = dict(rows)
    assert shown["Mean"] == "1 degC"
    assert shown["Samples"] == "2 valid, 1 missing, 0 invalid (3 in interval)"
    assert shown["Weighting"] == WEIGHTING_TEXT
    assert panel.title.text() == "Interval statistics"
    assert panel.table.rowCount() == len(rows)
    for index, (label, text) in enumerate(rows):
        label_item = panel.table.item(index, 0)
        text_item = panel.table.item(index, 1)
        assert label_item is not None and text_item is not None
        assert (label_item.text(), text_item.text()) == (label, text)


def test_panel_states_missing_current_value_in_text(qtbot: QtBot) -> None:
    panel = _panel(qtbot)
    stats = interval_statistics(TIMES, VALUES, 0, 2 * S)
    panel.show_statistics(stats, current_value(TIMES, VALUES, 2 * S))
    rows = panel.rows()
    assert rows[0] == ("Current value", "--")
    assert rows[1] == ("Current state", "missing: latest sample at 2.000 s has no value")
    assert rows[2:] == format_statistics(stats)


def test_panel_clear_removes_rows(qtbot: QtBot) -> None:
    panel = _panel(qtbot)
    panel.show_statistics(interval_statistics(TIMES, VALUES, 0, S))
    panel.clear()
    assert panel.rows() == []
    assert panel.table.rowCount() == 0
    assert panel.title.text() == EMPTY_MESSAGE
