"""The histogram widget draws bars on bin edges and spells out every count."""

from math import nan
from typing import Any

import pyqtgraph as pg
import pytest
from pytestqt.qtbot import QtBot

from lbr_dashboard.core.histogram import histogram
from lbr_dashboard.ui.histogram_widget import (
    EMPTY_TITLE,
    HistogramWidget,
    format_ns,
    interval_text,
)


@pytest.fixture
def widget(qtbot: QtBot) -> HistogramWidget:
    view = HistogramWidget()
    qtbot.addWidget(view)
    view.show()
    return view


def _axis_label(view: HistogramWidget, side: str) -> str:
    return view.plot_widget.getAxis(side).labelText


def _bar_option(view: HistogramWidget, key: str) -> list[float]:
    assert view.bars is not None
    option: Any = view.bars.opts[key]
    return [float(item) for item in option]


def test_bars_span_bin_edges_with_counts(widget: HistogramWidget) -> None:
    values = [None, 1.0, 2.0, 2.0, 3.0, nan, 9.0]
    hist = histogram(
        [0, 1, 2, 3, 4, 5, 6], values, edges=[1, 2, 3], unit="kPa", start_ns=0, end_ns=5
    )
    widget.show_histogram(hist, "brake pressure")
    assert _bar_option(widget, "x0") == [1.0, 2.0]
    assert _bar_option(widget, "x1") == [2.0, 3.0]
    assert _bar_option(widget, "height") == [1, 3]
    assert _axis_label(widget, "bottom") == "brake pressure (kPa)"
    assert _axis_label(widget, "left") == "samples"
    text = widget.summary.text()
    assert "Valid samples: 4 in 2 bins" in text
    assert "Missing: 2" in text
    assert "Above range: 0" in text
    assert "Interval: [0 s, 0.000000005 s]" in text
    plot = widget.plot_widget.getPlotItem()
    assert plot is not None
    assert widget.bars in plot.items


def test_empty_result_draws_no_bars_but_reports_missing(widget: HistogramWidget) -> None:
    hist = histogram([0, 1], [None, nan])
    widget.show_histogram(hist, "rpm")
    assert widget.bars is None
    assert _axis_label(widget, "bottom") == "rpm"
    plot = widget.plot_widget.getPlotItem()
    assert plot is not None
    assert "no valid samples" in plot.titleLabel.text
    assert "Missing: 2" in widget.summary.text()
    assert "Interval: whole log" in widget.summary.text()


def test_new_histogram_replaces_old_and_clear_resets(widget: HistogramWidget) -> None:
    widget.show_histogram(histogram([0, 1], [1.0, 2.0], bins=2), "a")
    widget.show_histogram(histogram([0, 1, 2], [1.0, 2.0, 3.0], bins=3), "b")
    plot = widget.plot_widget.getPlotItem()
    assert plot is not None
    assert [item for item in plot.items if isinstance(item, pg.BarGraphItem)] == [widget.bars]
    assert _bar_option(widget, "height") == [1, 1, 1]
    widget.clear()
    assert widget.bars is None
    assert widget.summary.text() == ""
    assert EMPTY_TITLE in plot.titleLabel.text


@pytest.mark.parametrize(
    ("value", "text"),
    [(0, "0 s"), (1_500_000_000, "1.5 s"), (-250_000_000, "-0.25 s"), (7, "0.000000007 s")],
)
def test_format_ns_is_exact(value: int, text: str) -> None:
    assert format_ns(value) == text


def test_interval_text_names_open_ends() -> None:
    assert interval_text(histogram([0], [1.0], start_ns=0)) == "[0 s, log end]"
    assert interval_text(histogram([0], [1.0], end_ns=2_000_000_000)) == "[log start, 2 s]"
