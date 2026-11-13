"""The state timeline widget draws labeled bars, gaps, sample markers and a cursor."""

from typing import Any

import pytest
from PySide6.QtCore import Qt
from pytestqt.qtbot import QtBot

from lbr_dashboard.core.state_timeline import SegmentKind, StateSegment, build_state_timeline
from lbr_dashboard.ui.state_timeline_widget import (
    EMPTY_TITLE,
    MAX_LABELS,
    StateTimelineWidget,
    ns_to_seconds,
    segment_brush,
    state_color,
)

SECOND = 1_000_000_000


@pytest.fixture
def widget(qtbot: QtBot) -> StateTimelineWidget:
    view = StateTimelineWidget()
    qtbot.addWidget(view)
    view.resize(800, 300)
    view.show()
    return view


def _bar_option(view: StateTimelineWidget, key: str) -> list[Any]:
    assert view.bars is not None
    option: Any = view.bars.opts[key]
    return list(option)


def _gear_timeline():
    return build_state_timeline(
        [0, 1 * SECOND, 2 * SECOND, 3 * SECOND, 6 * SECOND],
        ["N", "1", None, "1", "9"],
        labels={"N": "neutral", "1": "first"},
        max_hold_ns=2 * SECOND,
        data_end_ns=10 * SECOND,
    )


def test_segments_are_bars_in_seconds_with_labels(widget: StateTimelineWidget) -> None:
    timeline = _gear_timeline()
    widget.show_timeline("gear_position", timeline)
    assert widget.signal_id == "gear_position"
    assert widget.origin_ns == 0
    assert _bar_option(widget, "x0") == [0.0, 1.0, 2.0, 3.0, 5.0, 6.0, 8.0]
    assert _bar_option(widget, "x1") == [1.0, 2.0, 3.0, 5.0, 6.0, 8.0, 10.0]
    labels = [item.textItem.toPlainText() for item in widget.text_items]
    assert labels == ["neutral", "first", "no data", "first", "no data", "unknown: '9'", "no data"]
    assert widget.markers is not None
    assert [spot.pos().x() for spot in widget.markers.points()] == [0.0, 1.0, 2.0, 3.0, 6.0]
    assert widget.plot_widget.getAxis("bottom").labelText == "time (s)"
    assert "5 samples" in widget.summary.text()
    assert "at most 2 s" in widget.summary.text()
    assert "no data" in widget.summary.text()


def test_gaps_and_unknown_values_are_hatched_not_only_colored() -> None:
    gap = StateSegment(0, 1, SegmentKind.NO_DATA, None, "no data")
    unknown = StateSegment(0, 1, SegmentKind.UNKNOWN, 7, "unknown: 7")
    mixed = StateSegment(0, 1, SegmentKind.MIXED, None, "3 changes", 3)
    known = StateSegment(0, 1, SegmentKind.STATE, True, "true")
    assert segment_brush(gap).style() == Qt.BrushStyle.BDiagPattern
    assert segment_brush(unknown).style() == Qt.BrushStyle.DiagCrossPattern
    assert segment_brush(mixed).style() == Qt.BrushStyle.Dense5Pattern
    assert segment_brush(known).style() == Qt.BrushStyle.SolidPattern
    assert state_color("true") == state_color("true")


def test_origin_and_window_set_the_axis_zero(widget: StateTimelineWidget) -> None:
    timeline = build_state_timeline(
        [10 * SECOND, 12 * SECOND],
        [True, False],
        data_end_ns=14 * SECOND,
        window_start_ns=11 * SECOND,
        window_end_ns=13 * SECOND,
    )
    widget.show_timeline("brake_switch", timeline)
    assert widget.origin_ns == 11 * SECOND
    assert _bar_option(widget, "x0") == [0.0, 1.0]
    widget.show_timeline("brake_switch", timeline, origin_ns=10 * SECOND)
    assert _bar_option(widget, "x0") == [1.0, 2.0]


def test_selected_time_cursor_follows_and_hides(widget: StateTimelineWidget) -> None:
    widget.show_timeline("gear_position", _gear_timeline())
    widget.set_selected_time(int(2.5 * SECOND))
    assert widget.time_cursor.scene() is not None
    assert widget.time_cursor.value() == 2.5
    # Replacing the timeline keeps the selected time.
    widget.show_timeline("other_signal", _gear_timeline(), origin_ns=SECOND)
    assert widget.time_cursor.scene() is not None
    assert widget.time_cursor.value() == 1.5
    widget.set_selected_time(None)
    assert widget.time_cursor.scene() is None
    widget.set_selected_time(None)
    assert widget.time_cursor.scene() is None


def test_empty_timeline_draws_nothing(widget: StateTimelineWidget) -> None:
    widget.show_timeline("door", build_state_timeline([], []))
    assert widget.bars is None
    assert widget.markers is None
    assert widget.text_items == []
    assert "no samples" in widget.plot_item.titleLabel.text
    assert widget.origin_ns == 0


def test_events_only_timeline_draws_markers(widget: StateTimelineWidget) -> None:
    widget.show_timeline("button", build_state_timeline([5 * SECOND], [True]))
    assert widget.bars is None
    assert widget.markers is not None
    assert widget.origin_ns == 5 * SECOND


def test_large_timeline_is_bounded_and_labels_are_capped(widget: StateTimelineWidget) -> None:
    count = 50_000
    times = [index * 1_000_000 for index in range(count)]
    values = [index % 2 == 0 for index in range(count)]
    timeline = build_state_timeline(times, values, max_segments=600, max_events=800)
    widget.show_timeline("toggle", timeline)
    assert len(_bar_option(widget, "x0")) <= 600
    assert len(widget.text_items) == MAX_LABELS
    assert widget.markers is not None
    assert len(widget.markers.points()) <= 800
    assert "combined" in widget.summary.text()


def test_replace_clear_and_dispose(qtbot: QtBot) -> None:
    view = StateTimelineWidget()
    qtbot.addWidget(view)
    view.show_timeline("gear_position", _gear_timeline())
    view.set_selected_time(SECOND)
    view.show_timeline("gear_position", _gear_timeline())
    assert len(_bar_option(view, "x0")) == 7
    view.clear()
    assert view.bars is None
    assert view.markers is None
    assert view.text_items == []
    assert view.signal_id is None
    assert view.selected_time_ns is None
    assert view.plot_item.items == []
    assert view.plot_item.titleLabel.text == EMPTY_TITLE
    assert view.summary.text() == ""
    view.close()


def test_seconds_conversion() -> None:
    assert ns_to_seconds(1_500_000_000, 500_000_000) == 1.0
    assert ns_to_seconds(0, SECOND) == -1.0
