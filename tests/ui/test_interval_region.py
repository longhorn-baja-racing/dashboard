"""The plot region must mirror the shared interval selection without loops."""

import os
from collections.abc import Callable

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pyqtgraph as pg
from pyqtgraph.graphicsItems.PlotItem.PlotItem import PlotItem
from PySide6.QtCore import QCoreApplication, QEvent, Qt

from lbr_dashboard.core.interval import IntervalSelection, TimeInterval
from lbr_dashboard.ui.interval_region import (
    NS_PER_MILLISECOND,
    NS_PER_SECOND,
    IntervalRegion,
    PlotTimeScale,
)

EPOCH_NS = 1_700_000_000_123_456_789
SECOND = NS_PER_SECOND

MakeRegion = Callable[..., tuple[pg.PlotWidget, IntervalSelection, IntervalRegion]]


@pytest.fixture
def make_region(qtbot) -> MakeRegion:
    def make(
        bounds: TimeInterval | None = TimeInterval(EPOCH_NS, EPOCH_NS + 100 * SECOND),
        scale: PlotTimeScale | None = None,
    ) -> tuple[pg.PlotWidget, IntervalSelection, IntervalRegion]:
        widget = pg.PlotWidget()
        qtbot.addWidget(widget)
        selection = IntervalSelection(bounds)
        region = IntervalRegion(widget, selection, scale or PlotTimeScale.seconds(EPOCH_NS))
        return widget, selection, region

    return make


def _plot_item(widget: pg.PlotWidget) -> PlotItem:
    plot_item = widget.getPlotItem()
    assert plot_item is not None
    return plot_item


def _edges(region: IntervalRegion) -> tuple[float, float]:
    low, high = region.region.getRegion()
    assert isinstance(low, float) and isinstance(high, float)
    return low, high


def _record(selection: IntervalSelection) -> list[TimeInterval | None]:
    values: list[TimeInterval | None] = []
    selection.subscribe(values.append)
    return values


# PlotTimeScale


def test_scale_converts_seconds_and_milliseconds_around_an_origin() -> None:
    seconds = PlotTimeScale.seconds(EPOCH_NS)
    assert seconds.to_x(EPOCH_NS) == 0.0
    assert seconds.to_x(EPOCH_NS + 2_500_000_000) == 2.5
    assert seconds.to_ns(2.5) == EPOCH_NS + 2_500_000_000
    assert seconds.to_x(EPOCH_NS - SECOND) == -1.0
    millis = PlotTimeScale.milliseconds()
    assert millis.ns_per_unit == NS_PER_MILLISECOND
    assert millis.to_x(1_500_000) == 1.5
    assert millis.to_ns(1.5) == 1_500_000


def test_scale_round_trip_is_exact_near_the_origin() -> None:
    scale = PlotTimeScale.seconds(EPOCH_NS)
    offsets = [0, 1, 7, 999_999_999, 123_456_789_012, 86_400 * SECOND + 1, 2**50 - 1]
    offsets += [step * 1_234_567_891 + 3 for step in range(0, 900_000, 9_973)]
    for offset in offsets:
        for time_ns in (EPOCH_NS + offset, EPOCH_NS - offset):
            assert scale.to_ns(scale.to_x(time_ns)) == time_ns


def test_scale_without_origin_loses_sub_microsecond_precision_at_epoch_times() -> None:
    scale = PlotTimeScale.seconds()
    for time_ns in (EPOCH_NS, EPOCH_NS + 1, EPOCH_NS + 999_999_999):
        assert abs(scale.to_ns(scale.to_x(time_ns)) - time_ns) <= 512


@pytest.mark.parametrize(
    ("ns_per_unit", "origin_ns", "error"),
    [(0, 0, ValueError), (-1, 0, ValueError), (1.0, 0, TypeError), (True, 0, TypeError)]
    + [(1, 0.5, TypeError), (1, False, TypeError)],
)
def test_scale_rejects_invalid_parameters(
    ns_per_unit: object, origin_ns: object, error: type[Exception]
) -> None:
    with pytest.raises(error):
        PlotTimeScale(ns_per_unit, origin_ns)  # type: ignore[arg-type]


# IntervalRegion


def test_region_is_hidden_until_something_is_selected(make_region: MakeRegion) -> None:
    widget, selection, region = make_region()
    plot_item = _plot_item(widget)
    assert region.region in plot_item.items
    assert not region.region.isVisible()
    selection.select(EPOCH_NS + 2 * SECOND, EPOCH_NS + 5 * SECOND)
    assert region.region.isVisible()
    assert region.region.getRegion() == (2.0, 5.0)


def test_existing_selection_is_shown_on_creation(qtbot) -> None:
    widget = pg.PlotWidget()
    qtbot.addWidget(widget)
    selection = IntervalSelection()
    selection.select(EPOCH_NS, EPOCH_NS + SECOND // 2)
    region = IntervalRegion(widget, selection, PlotTimeScale.seconds(EPOCH_NS))
    assert region.region.isVisible()
    assert region.region.getRegion() == (0.0, 0.5)


def test_dragging_the_region_updates_the_selection_once(make_region: MakeRegion) -> None:
    _widget, selection, region = make_region()
    selection.select(EPOCH_NS, EPOCH_NS + SECOND)
    values = _record(selection)
    region.region.setRegion((12.25, 3.5))  # Emits sigRegionChangeFinished like a drag.
    expected = TimeInterval(EPOCH_NS + 3_500_000_000, EPOCH_NS + 12_250_000_000)
    assert selection.interval == expected
    assert values == [expected]
    assert region.region.getRegion() == (3.5, 12.25)


def test_selection_changes_move_the_region_without_feedback(make_region: MakeRegion) -> None:
    _widget, selection, region = make_region()
    values = _record(selection)
    finished: list[object] = []
    region.region.sigRegionChangeFinished.connect(finished.append)
    selection.select(EPOCH_NS + 1, EPOCH_NS + 3 * SECOND + 7)
    selection.set_end(EPOCH_NS + 4 * SECOND)
    assert values == [
        TimeInterval(EPOCH_NS + 1, EPOCH_NS + 3 * SECOND + 7),
        TimeInterval(EPOCH_NS + 1, EPOCH_NS + 4 * SECOND),
    ]
    assert len(finished) == 2  # The region did move, and that did not re-enter selection.
    low, high = _edges(region)
    assert PlotTimeScale.seconds(EPOCH_NS).to_ns(low) == EPOCH_NS + 1
    assert high == 4.0


def test_lossy_scale_never_rewrites_a_programmatic_selection(make_region: MakeRegion) -> None:
    # Without an origin, epoch times do not survive the float round trip. Moving the
    # region for a selection change must not feed the rounded value back.
    _widget, selection, region = make_region(scale=PlotTimeScale.seconds())
    values = _record(selection)
    requested = TimeInterval(EPOCH_NS + 1, EPOCH_NS + 2 * SECOND + 3)
    scale = PlotTimeScale.seconds()
    assert scale.to_ns(scale.to_x(requested.start_ns)) != requested.start_ns
    selection.select(requested.start_ns, requested.end_ns)
    assert selection.interval == requested
    assert values == [requested]


def test_drag_past_bounds_is_clamped_and_region_snaps_back(make_region: MakeRegion) -> None:
    _widget, selection, region = make_region()
    selection.select(EPOCH_NS + 90 * SECOND, EPOCH_NS + 100 * SECOND)
    values = _record(selection)
    region.region.setRegion((90.0, 130.0))  # The stored value does not change.
    assert values == []
    assert region.region.getRegion() == (90.0, 100.0)
    region.region.setRegion((-10.0, 1.0))
    assert selection.interval == TimeInterval(EPOCH_NS, EPOCH_NS + SECOND)
    assert region.region.getRegion() == (0.0, 1.0)


def test_drag_entirely_outside_bounds_is_rejected(make_region: MakeRegion) -> None:
    _widget, selection, region = make_region()
    selection.select(EPOCH_NS + 10 * SECOND, EPOCH_NS + 20 * SECOND)
    region.region.setRegion((150.0, 160.0))
    assert selection.interval == TimeInterval(EPOCH_NS + 10 * SECOND, EPOCH_NS + 20 * SECOND)
    assert region.region.getRegion() == (10.0, 20.0)


def test_unbounded_selection_follows_any_drag(make_region: MakeRegion) -> None:
    _widget, selection, region = make_region(bounds=None)
    selection.select(EPOCH_NS, EPOCH_NS + SECOND)
    region.region.setRegion((-5.0, 500.0))
    assert selection.interval == TimeInterval(EPOCH_NS - 5 * SECOND, EPOCH_NS + 500 * SECOND)


def test_clear_hides_the_region(make_region: MakeRegion) -> None:
    _widget, selection, region = make_region()
    selection.select(EPOCH_NS, EPOCH_NS + SECOND)
    selection.clear()
    assert not region.region.isVisible()


def test_escape_on_the_plot_clears_the_selection(make_region: MakeRegion, qtbot) -> None:
    widget, selection, region = make_region()
    selection.select(EPOCH_NS, EPOCH_NS + SECOND)
    values = _record(selection)
    qtbot.keyClick(widget, Qt.Key.Key_A)
    assert selection.interval is not None
    qtbot.keyClick(widget, Qt.Key.Key_Escape)
    assert selection.interval is None
    assert not region.region.isVisible()
    qtbot.keyClick(widget, Qt.Key.Key_Escape)  # Nothing selected: passed on, no notification.
    assert values == [None]


def test_select_visible_range_starts_a_clamped_selection(make_region: MakeRegion) -> None:
    widget, selection, region = make_region()
    _plot_item(widget).getViewBox().setXRange(-20.0, 40.0, padding=0)
    selected = region.select_visible_range()
    assert selected == TimeInterval(EPOCH_NS, EPOCH_NS + 40 * SECOND)
    assert selection.interval == selected
    assert region.region.isVisible()
    assert region.region.getRegion() == (0.0, 40.0)


def test_select_visible_range_outside_the_session_is_rejected(make_region: MakeRegion) -> None:
    widget, selection, region = make_region()
    _plot_item(widget).getViewBox().setXRange(200.0, 300.0, padding=0)
    with pytest.raises(ValueError, match="outside the session range"):
        region.select_visible_range()
    assert selection.interval is None


def test_two_plots_share_one_selection(make_region: MakeRegion, qtbot) -> None:
    _widget, selection, first = make_region()
    other_widget = pg.PlotWidget()
    qtbot.addWidget(other_widget)
    millis = PlotTimeScale.milliseconds(EPOCH_NS)
    second = IntervalRegion(other_widget, selection, millis)
    first.region.setRegion((1.0, 2.0))
    assert second.region.getRegion() == (1_000.0, 2_000.0)
    second.region.setRegion((500.0, 1_500.0))
    assert selection.interval == TimeInterval(EPOCH_NS + SECOND // 2, EPOCH_NS + 3 * SECOND // 2)
    assert first.region.getRegion() == (0.5, 1.5)


def test_detach_stops_following_the_selection(make_region: MakeRegion, qtbot) -> None:
    widget, selection, region = make_region()
    region.detach()
    region.detach()  # Harmless twice.
    assert region.region not in _plot_item(widget).items
    selection.select(EPOCH_NS, EPOCH_NS + SECOND)
    assert not region.region.isVisible()
    qtbot.keyClick(widget, Qt.Key.Key_Escape)
    assert selection.interval is not None


def test_deleted_plot_unsubscribes_from_the_selection() -> None:
    widget = pg.PlotWidget()
    selection = IntervalSelection()
    IntervalRegion(widget, selection, PlotTimeScale.seconds())
    destroyed: list[object] = []
    widget.destroyed.connect(lambda *_: destroyed.append(True))
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert destroyed
    selection.select(0, SECOND)  # Would raise if the deleted region were still notified.
    assert selection.interval == TimeInterval(0, SECOND)
