"""Offscreen checks for the XY scatter widget."""

from __future__ import annotations

import pytest
from pytestqt.qtbot import QtBot

from lbr_dashboard.core.xy_pairing import AlignmentPolicy, PairedSeries, pair_signals
from lbr_dashboard.ui.xy_plot import XYPlotWidget, axis_label, format_duration_ns, pairing_note

MS = 1_000_000


@pytest.fixture
def widget(qtbot: QtBot) -> XYPlotWidget:
    plot = XYPlotWidget(max_points=3)
    qtbot.addWidget(plot)
    plot.show()
    return plot


def _speed_vs_temperature() -> PairedSeries:
    x_times = [0, 10 * MS, 20 * MS, 30 * MS, 40 * MS, 50 * MS]
    x_values: list[float | None] = [10.0, 20.0, None, 40.0, 50.0, 60.0]
    y_times = [0, 10 * MS, 30 * MS, 40 * MS, 50 * MS]
    y_values: list[float | None] = [1.0, 2.0, 4.0, None, 6.0]
    return pair_signals(
        x_times,
        x_values,
        y_times,
        y_values,
        policy=AlignmentPolicy.NEAREST,
        tolerance_ns=2 * MS,
        x_unit="km/h",
        y_unit="deg C",
    )


def _axis_text(widget: XYPlotWidget, name: str) -> str:
    return widget.plot_widget.getPlotItem().getAxis(name).labelText  # type: ignore[union-attr]


def test_show_pairs_labels_axes_and_describes_rules(widget: XYPlotWidget) -> None:
    series = _speed_vs_temperature()
    assert len(series) == 4
    widget.show_pairs(series, "Wheel speed", "CVT temperature")

    assert _axis_text(widget, "bottom") == "Wheel speed (km/h)"
    assert _axis_text(widget, "left") == "CVT temperature (deg C)"
    note = widget.note_label.text()
    assert "nearest Y within 2 ms of each X" in note
    assert "Showing 3 of 4 pairs." in note
    assert "1 X missing, 1 Y missing, 0 no Y within tolerance" in note


def test_scatter_is_bounded_and_keeps_first_and_last(widget: XYPlotWidget) -> None:
    widget.show_pairs(_speed_vs_temperature(), "Speed", "Temp")
    xs, ys = widget.scatter.getData()
    assert len(xs) == 3
    assert (xs[0], ys[0]) == (10.0, 1.0)
    assert (xs[-1], ys[-1]) == (60.0, 6.0)


def test_highlight_marks_nearest_pair_and_hides_when_none(widget: XYPlotWidget) -> None:
    assert widget.highlight_time(0) is None
    assert not widget.marker.isVisible()

    widget.show_pairs(_speed_vs_temperature(), "Speed", "Temp")
    # The 30 ms pair is not drawn because of the point limit but is still found.
    assert widget.highlight_time(31 * MS) == 30 * MS
    assert widget.marker.isVisible()
    xs, ys = widget.marker.getData()
    assert (list(xs), list(ys)) == ([40.0], [4.0])

    assert widget.highlight_time(9 * MS) == 10 * MS
    assert widget.highlight_time(20 * MS) == 10 * MS  # tie with 30 ms picks the earlier
    assert widget.highlight_time(-5) == 0
    assert widget.highlight_time(10**12) == 50 * MS

    assert widget.highlight_time(None) is None
    assert not widget.marker.isVisible()


def test_new_series_clears_highlight_and_empty_series_is_explained(
    widget: XYPlotWidget,
) -> None:
    widget.show_pairs(_speed_vs_temperature(), "Speed", "Temp")
    widget.highlight_time(0)
    empty = pair_signals(
        [0, 1], [1.0, 2.0], [], [], policy=AlignmentPolicy.PREVIOUS, tolerance_ns=500
    )
    widget.show_pairs(empty, "Speed", "Temp")
    assert not widget.marker.isVisible()
    assert widget.highlight_time(0) is None
    assert len(widget.scatter.getData()[0]) == 0
    assert _axis_text(widget, "bottom") == "Speed"
    note = widget.note_label.text()
    assert "latest Y at or before each X, at most 500 ns old" in note
    assert "Showing 0 of 0 pairs." in note
    assert "2 no Y within tolerance" in note


def test_note_names_the_driver() -> None:
    series = pair_signals(
        [0], [1.0], [0], [2.0], policy=AlignmentPolicy.NEAREST, tolerance_ns=0, driver="y"
    )
    note = pairing_note(series, 1)
    assert "nearest X within 0 ns of each Y" in note
    assert "0 no X within tolerance" in note


@pytest.mark.parametrize(
    ("duration", "text"),
    [(0, "0 ns"), (999, "999 ns"), (5_000, "5 us"), (2 * MS, "2 ms"), (1_500 * MS, "1500 ms")]
    + [(3_000 * MS, "3 s")],
)
def test_format_duration(duration: int, text: str) -> None:
    assert format_duration_ns(duration) == text


def test_axis_label_without_unit() -> None:
    assert axis_label("Throttle", None) == "Throttle"
    assert axis_label("Throttle", "") == "Throttle"
    assert axis_label("Throttle", "%") == "Throttle (%)"


def test_invalid_point_limit_is_rejected() -> None:
    with pytest.raises(ValueError, match="max_points must be >= 2"):
        XYPlotWidget(max_points=1)
