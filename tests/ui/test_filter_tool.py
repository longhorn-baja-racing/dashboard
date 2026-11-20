"""Analysis > Filters overlays a moving average on the main time plot in the real app."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFileDialog

from lbr_dashboard.app import create_main_window
from lbr_dashboard.core.filters import moving_average
from lbr_dashboard.source_controller import SourceController
from lbr_dashboard.ui.signal_browser import ID_ROLE
from lbr_dashboard.ui.tools.filters import FilterTool

SAMPLES = Path(__file__).resolve().parents[2] / "sample_data"
DEMO = SAMPLES / "timestamped_demo.csv"
PRESSURE = "process.pressure"
TEMPERATURE = "process.temperature"
MS = 1_000_000


@pytest.fixture
def app(qtbot):
    window = create_main_window()
    window.resize(1100, 800)
    window.show()
    owner = window.source_owner
    assert isinstance(owner, SourceController)
    owner.replay._now = lambda: 0
    yield window, owner
    window.close()
    qtbot.waitUntil(lambda: not window.isVisible())
    window.deleteLater()


def choose(window, qtbot, key):
    picker = window.source_picker
    picker.choices.setCurrentIndex(picker.choices.findData(key))
    picker.connect_button.click()
    owner = window.source_owner
    qtbot.waitUntil(lambda: window.replay_bar.slider.isEnabled())
    if key != "csv":
        qtbot.waitUntil(lambda: owner.source is not None and owner.source.join(0))
        owner.poll()


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


def filters(window) -> FilterTool:
    dock = window.analysis_docks.open("filters")
    tool = dock.widget()
    assert isinstance(tool, FilterTool)
    tool.refresh()
    return tool


def set_params(tool, window=3, alignment="centered", gap_ms=None, jitter_pct=10.0):
    tool.window_samples.setValue(window)
    tool.alignment.setCurrentIndex(tool.alignment.findData(alignment))
    tool.jitter.setValue(jitter_pct)
    tool.split_gaps.setChecked(gap_ms is not None)
    if gap_ms is not None:
        tool.gap_ms.setValue(gap_ms)


def raw_data(window):
    curve = window.right_panel.time_plot.curve
    assert curve is not None
    xs, ys = curve.getData()
    return np.array(xs), np.array(ys)


def data_items(window):
    item = window.right_panel.plot_widget.getPlotItem()
    assert item is not None
    return item.listDataItems()


def test_filters_menu_entry_opens_the_tool(app):
    window, _ = app
    labels = [action.text() for action in window.analysis_docks.menu.actions()]
    assert "Filters" in labels
    tool = filters(window)
    assert tool.minimumWidth() >= 300 and tool.minimumHeight() >= 200
    assert "Select a numeric signal" in tool.hint.text()
    assert not tool.apply_button.isEnabled()


def test_irregular_csv_sampling_is_rejected_with_its_reason(app, qtbot, monkeypatch):
    window, _ = app
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(DEMO), ""))
    choose(window, qtbot, "csv")
    select(window, "csv.column.1")
    before = raw_data(window)
    tool = filters(window)
    assert tool.apply_button.isEnabled()
    assert tool.apply() is None
    text = tool.status.text()
    assert text.startswith("Not filtered: sample spacing")
    assert "differs from the nominal period" in text and "raw trace is unchanged" in text
    assert tool.overlay is None and data_items(window) == [window.right_panel.time_plot.curve]
    np.testing.assert_array_equal(raw_data(window)[1], before[1])

    select(window, "csv.column.3")
    assert "filters need a numeric signal" in tool.hint.text()
    assert not tool.apply_button.isEnabled()


def test_overlay_matches_the_reference_and_leaves_the_raw_trace(app, qtbot):
    window, _ = app
    choose(window, qtbot, "process")
    select(window, PRESSURE)
    raw_x, raw_y = raw_data(window)
    tool = filters(window)

    # The simulated pressure drops samples 20-24, a 60 ms spacing at 10 ms.
    set_params(tool, window=3)
    assert tool.apply() is None
    assert "spacing 60000000 ns" in tool.status.text()

    set_params(tool, window=3, gap_ms=30.0)
    result = tool.apply()
    assert result is not None and tool.overlay is not None
    times, values = window.analysis.numeric_series(PRESSURE)
    reference = moving_average(times, values, window_samples=3, gap_ns=30 * MS)
    np.testing.assert_array_equal(result.values, reference.values)
    assert result.unit == "Pa" and result.signal == PRESSURE
    assert result.missing_count > 0 and result.segment_count >= 3

    curve = tool.overlay.curve
    assert data_items(window) == [window.right_panel.time_plot.curve, curve]
    xs, ys = curve.getOriginalDataset()
    assert xs is not None and ys is not None
    origin = window.right_panel.time_plot.origin_ns
    np.testing.assert_allclose(xs, (np.array(times) - origin) / 1e9, rtol=0, atol=1e-12)
    np.testing.assert_array_equal(ys, reference.values)
    assert curve.opts["connect"] == "finite"
    label = "pressure [Pa] moving average, 3 samples centered (30 ms)"
    assert curve.name() == label
    legend = [entry[1].text for entry in tool.overlay.legend.items]
    assert legend == ["pressure [Pa] raw", label]
    status = tool.status.text()
    assert "Nominal period 10 ms, largest spacing deviation 0.0% (tolerance 10%)" in status
    assert "Spacings of at least 30 ms split segments." in status
    after_x, after_y = raw_data(window)
    np.testing.assert_array_equal(after_x, raw_x)
    np.testing.assert_array_equal(after_y, raw_y)

    # Same parameters on refresh (e.g. a new snapshot) give the same output.
    tool.refresh()
    assert tool.result is not None and tool.overlay.curve is curve
    np.testing.assert_array_equal(tool.result.values, reference.values)

    # New parameters for the same trace relabel the same curve.
    set_params(tool, window=5, gap_ms=30.0)
    wider = tool.apply()
    assert wider is not None and tool.overlay.curve is curve
    label = "pressure [Pa] moving average, 5 samples centered (50 ms)"
    assert curve.name() == label
    assert [entry[1].text for entry in tool.overlay.legend.items] == ["pressure [Pa] raw", label]
    np.testing.assert_array_equal(curve.getOriginalDataset()[1], wider.values)


def test_trailing_label_shows_its_delay_and_a_second_signal(app, qtbot):
    window, _ = app
    choose(window, qtbot, "process")
    select(window, TEMPERATURE)
    tool = filters(window)
    set_params(tool, window=2, alignment="trailing")
    result = tool.apply()
    assert result is not None and tool.overlay is not None
    unit = chr(0xB0) + "C"
    assert result.unit == unit and result.delay_ns == 500 * MS
    times, values = window.analysis.numeric_series(TEMPERATURE)
    expected = [None] + [(a + b) / 2 for a, b in zip(values, values[1:])]  # type: ignore[operator]
    assert result.values_or_none() == pytest.approx(expected)
    assert tool.overlay.curve.name() == (
        f"temperature [{unit}] moving average, 2 samples trailing (2000 ms), delay 500 ms"
    )
    assert "delay 500 ms" in tool.status.text()


def test_overlay_goes_when_the_signal_changes_or_the_plot_resets(app, qtbot):
    window, _ = app
    choose(window, qtbot, "process")
    select(window, PRESSURE)
    tool = filters(window)
    set_params(tool, window=5, gap_ms=30.0)
    assert tool.apply() is not None
    overlay = tool.overlay
    assert overlay is not None and overlay.legend.scene() is not None

    select(window, TEMPERATURE)
    assert tool.overlay is None
    assert "different signal" in tool.status.text()
    assert overlay.curve not in data_items(window)
    assert overlay.legend.scene() is None
    assert not tool.remove_button.isEnabled()

    select(window, PRESSURE)
    assert tool.apply() is not None
    overlay = tool.overlay
    assert overlay is not None
    window.load_csv(SAMPLES / "sample_baja_log.csv")
    assert tool.overlay is None
    assert "main plot was reset" in tool.status.text()
    assert overlay.legend.scene() is None
    assert overlay.curve not in data_items(window)
    tool.refresh()
    assert "Select a numeric signal" in tool.hint.text()


def test_remove_button_restores_the_plain_plot(app, qtbot):
    window, _ = app
    choose(window, qtbot, "process")
    select(window, TEMPERATURE)
    tool = filters(window)
    set_params(tool, window=1)
    assert tool.apply() is not None
    assert tool.remove_button.isEnabled()
    tool.remove_button.click()
    assert tool.overlay is None
    assert data_items(window) == [window.right_panel.time_plot.curve]


def test_configuration_round_trip_and_invalid_configurations(app, qtbot):
    window, _ = app
    choose(window, qtbot, "process")
    select(window, PRESSURE)
    tool = filters(window)
    set_params(tool, window=7, alignment="trailing", gap_ms=25.5, jitter_pct=12.5)
    shown = tool.apply()
    assert shown is not None
    config = tool.configuration()
    assert config == {
        "filter": "moving_average",
        "window_samples": 7,
        "alignment": "trailing",
        "jitter_tolerance": 0.125,
        "gap_ns": 25_500_000,
        "signal": PRESSURE,
    }
    assert {key: config[key] for key in shown.params.as_dict()} == shown.params.as_dict()

    tool.remove_overlay()
    set_params(tool, window=3)
    tool.restore_configuration(config)
    assert tool.configuration() == config
    assert tool.result is not None
    np.testing.assert_array_equal(tool.result.values, shown.values)

    for bad, message in (
        ([], "mapping"),
        ({"filter": "low_pass"}, "Unknown filter"),
        ({"window_samples": 4, "alignment": "centered"}, "odd number"),
        ({"window_samples": 0}, "window_samples"),
        ({"jitter_tolerance": 1.5}, "jitter_tolerance"),
        ({"gap_ns": -1}, "gap_ns"),
        ({"signal": 3}, "signal"),
    ):
        with pytest.raises(ValueError, match=message):
            tool.restore_configuration(bad)
    assert tool.configuration() == config

    tool.restore_configuration({**config, "signal": None})
    assert tool.overlay is None and tool.configuration()["signal"] is None
