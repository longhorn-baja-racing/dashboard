"""Raw and filtered curves are drawn together without bridging missing samples."""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg
import pytest
from pytestqt.qtbot import QtBot

from lbr_dashboard.core.filters import moving_average
from lbr_dashboard.ui.filter_overlay import filter_label, overlay_filtered


def test_overlay_draws_raw_and_filtered_curves(qtbot: QtBot) -> None:
    widget = pg.PlotWidget()
    qtbot.addWidget(widget)
    plot_item = widget.getPlotItem()
    assert plot_item is not None
    plot_item.addLegend()
    times = [index * 10_000_000 for index in range(6)]
    raw = [1.0, 2.0, None, 4.0, 5.0, 6.0]
    result = moving_average(times, raw, window_samples=3, signal="engine.rpm", unit="rpm")

    raw_curve, filtered_curve = overlay_filtered(plot_item, raw, result, name="engine.rpm")

    assert plot_item.listDataItems() == [raw_curve, filtered_curve]
    raw_x, raw_y = raw_curve.getOriginalDataset()
    assert raw_x is not None and raw_y is not None
    np.testing.assert_allclose(raw_x, [0.0, 0.01, 0.02, 0.03, 0.04, 0.05])
    np.testing.assert_array_equal(raw_y, [1.0, 2.0, np.nan, 4.0, 5.0, 6.0])
    _, filtered_y = filtered_curve.getOriginalDataset()
    np.testing.assert_array_equal(filtered_y, [np.nan, np.nan, np.nan, np.nan, 5.0, np.nan])
    assert raw_curve.opts["connect"] == filtered_curve.opts["connect"] == "finite"
    assert raw_curve.name() == "engine.rpm [rpm] raw"
    assert filtered_curve.name() == "engine.rpm [rpm] moving average, 3 samples centered (30 ms)"


def test_label_without_a_known_period() -> None:
    result = moving_average([0], [1.0], window_samples=1, alignment="trailing")
    assert filter_label(result) == "moving average, 1 samples trailing"


def test_overlay_rejects_raw_values_of_another_length(qtbot: QtBot) -> None:
    widget = pg.PlotWidget()
    qtbot.addWidget(widget)
    plot_item = widget.getPlotItem()
    assert plot_item is not None
    result = moving_average([0, 10, 20], [1.0, 2.0, 3.0], window_samples=1)
    with pytest.raises(ValueError, match="raw_values has 2 samples"):
        overlay_filtered(plot_item, [1.0, 2.0], result, name="x")
    assert plot_item.listDataItems() == []
