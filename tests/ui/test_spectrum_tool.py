"""The Analysis > Spectrum tool in the real window with CSV and simulator sessions."""

import re
from pathlib import Path

import pyqtgraph as pg
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFileDialog

from lbr_dashboard.app import create_main_window
from lbr_dashboard.source_controller import SourceController
from lbr_dashboard.ui.signal_browser import ID_ROLE
from lbr_dashboard.ui.theme import apply_theme
from lbr_dashboard.ui.tools.spectrum import SpectrumTool

SAMPLES = Path(__file__).resolve().parents[2] / "sample_data"
DEMO = SAMPLES / "timestamped_demo.csv"
MS = 1_000_000
# process.pressure: 100 Hz cycle 0, 5, 8, 3 for 3 s (301 scheduled samples), five
# lost, five stale (omitted) and five unavailable (null) readings.
PRESSURE = "process.pressure"
PRESSURE_RECORDS = 291


@pytest.fixture
def window(qtbot):
    window = create_main_window()
    window.show()
    owner = window.source_owner
    assert isinstance(owner, SourceController)
    owner.replay._now = lambda: 0
    yield window
    window.close()
    qtbot.waitUntil(lambda: not window.isVisible())
    window.deleteLater()


def connect(window, key):
    picker = window.source_picker
    picker.choices.setCurrentIndex(picker.choices.findData(key))
    picker.connect_button.click()


def open_demo_csv(qtbot, window, monkeypatch):
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(DEMO), ""))
    connect(window, "csv")
    qtbot.waitUntil(lambda: window.replay_bar.slider.isEnabled())


def open_process_simulator(qtbot, window):
    connect(window, "process")

    def complete():
        snapshot = window.analysis.snapshot
        return (
            snapshot is not None
            and len(snapshot.series.get(PRESSURE, ())) == PRESSURE_RECORDS
            and window.analysis.bounds is not None
            and window.analysis.bounds.end_ns == 3000 * MS
        )

    qtbot.waitUntil(complete, timeout=10_000)


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


def spectrum_tool(window) -> SpectrumTool:
    dock = window.analysis_docks.open("spectrum")
    tool = dock.widget()
    assert isinstance(tool, SpectrumTool)
    tool.refresh()
    return tool


def curves(tool: SpectrumTool) -> list:
    plot = tool.view.plot_widget.getPlotItem()
    assert plot is not None
    return [item for item in plot.items if isinstance(item, pg.PlotDataItem)]


def peak(tool: SpectrumTool):
    marker = tool.view.peak_marker
    assert marker is not None
    return marker.getData()


def test_menu_lists_spectrum_and_reuses_its_dock(window):
    labels = [action.text() for action in window.analysis_docks.menu.actions()]
    assert "Spectrum" in labels
    first = window.analysis_docks.open("spectrum")
    assert window.analysis_docks.open("spectrum") is first


def test_known_tone_over_the_shared_interval_is_explicit(qtbot, window):
    open_process_simulator(qtbot, window)
    select(window, PRESSURE)
    window.analysis.selection.select(1000 * MS, 2990 * MS)
    tool = spectrum_tool(window)
    assert tool.view.plot_widget.isVisible()
    assert curves(tool) == [tool.view.curve]
    # The cycle -4, 1, 4, -1 about its mean is one tone at fs / 4 of amplitude sqrt(17).
    marker_x, marker_y = peak(tool)
    assert float(marker_x[0]) == pytest.approx(25.0)
    assert float(marker_y[0]) == pytest.approx(17**0.5, rel=1e-9)
    assert "interval 1.000 s to 2.990 s, 200 sample(s)" in tool.hint.text()
    text = tool.view.summary.text()
    assert "Scaling: one-sided peak amplitude in Pa" in text
    assert "Sample rate: 100 Hz" in text
    assert "Resolution: 0.5 Hz" in text
    assert "Nyquist: 50 Hz" in text
    assert "Window: hann  Detrend: mean" in text
    assert "Max spacing jitter: 0.000%" in text
    assert "Peak: 25 Hz  amplitude 4.12311 Pa" in text
    assert tool.view.plot_widget.getAxis("left").labelText == "amplitude (Pa)"

    tool.window_choice.setCurrentIndex(tool.window_choice.findData("none"))
    tool.scale_choice.setCurrentIndex(tool.scale_choice.findData("psd"))
    tool.refresh()
    text = tool.view.summary.text()
    assert "Window: none" in text
    assert "Scaling: one-sided PSD in Pa^2/Hz" in text
    assert tool.view.plot_widget.getAxis("left").labelText == "PSD (Pa^2/Hz)"
    # Rectangular PSD of a bin-centred tone: A^2 / 2 / resolution = 17 Pa^2/Hz.
    _, marker_y = peak(tool)
    assert float(marker_y[0]) == pytest.approx(17.0, rel=1e-9)


@pytest.mark.parametrize(
    ("interval_ms", "reason"),
    [
        pytest.param(None, "missing or non-finite value.*at 0.500 s", id="whole-session-nulls"),
        pytest.param((600, 900), "not uniform.*gap.*at 0.750 s", id="omitted-stale-readings"),
        pytest.param((1000, 1050), "at least 8 samples for a spectrum, got 6", id="too-few"),
    ],
)
def test_invalid_sampling_shows_the_reason_instead_of_a_plot(qtbot, window, interval_ms, reason):
    open_process_simulator(qtbot, window)
    select(window, PRESSURE)
    if interval_ms is not None:
        window.analysis.selection.select(interval_ms[0] * MS, interval_ms[1] * MS)
    tool = spectrum_tool(window)
    if interval_ms is None:
        assert "whole session (no interval selected), 0.000 s to 3.000 s" in tool.hint.text()
    assert not tool.view.plot_widget.isVisible()
    assert tool.view.curve is None and tool.view.peak_marker is None and curves(tool) == []
    text = tool.view.summary.text()
    assert text.startswith("pressure: no spectrum.")
    assert re.search(reason, text), text
    assert "Scaling" not in text and "Peak" not in text


def test_tool_follows_signal_and_interval_changes(qtbot, window):
    open_process_simulator(qtbot, window)
    window.left_panel.signal_browser.tree.clearSelection()
    window.analysis.set_selected(None)
    tool = spectrum_tool(window)
    assert "Select a numeric signal" in tool.hint.text()
    select(window, "process.status")
    qtbot.waitUntil(lambda: "a spectrum needs a numeric signal" in tool.hint.text())
    select(window, "process.temperature")
    qtbot.waitUntil(lambda: "at least 8 samples" in tool.view.summary.text())
    select(window, PRESSURE)
    window.analysis.selection.select(1000 * MS, 2990 * MS)
    qtbot.waitUntil(lambda: tool.view.curve is not None)
    window.analysis.selection.clear()
    qtbot.waitUntil(lambda: tool.view.curve is None)
    assert "whole session" in tool.hint.text()


def test_csv_gap_and_non_numeric_signals_never_plot(qtbot, window, monkeypatch):
    open_demo_csv(qtbot, window, monkeypatch)
    select(window, "csv.column.1")
    tool = spectrum_tool(window)
    assert "5 sample(s)" in tool.hint.text()
    assert "at least 8 samples" in tool.view.summary.text()
    assert curves(tool) == []
    select(window, "csv.column.3")
    tool.refresh()
    assert "a spectrum needs a numeric signal" in tool.hint.text()
    assert curves(tool) == []


def test_configuration_round_trip_and_rejects_unknown_values(window):
    tool = spectrum_tool(window)
    assert tool.configuration() == {"window": "hann", "detrend": "mean", "scale": "amplitude"}
    saved = {"window": "none", "detrend": "linear", "scale": "psd"}
    tool.restore_configuration(saved)
    assert tool.configuration() == saved
    with pytest.raises(ValueError, match="window"):
        tool.restore_configuration({"window": "flattop"})
    with pytest.raises(ValueError, match="dict"):
        tool.restore_configuration(["none"])
    assert tool.configuration() == saved
    tool.restore_configuration({"scale": "psd", "extra": 1})
    assert tool.configuration() == {"window": "hann", "detrend": "mean", "scale": "psd"}


def test_plot_and_title_follow_the_theme(qtbot, window):
    open_process_simulator(qtbot, window)
    select(window, PRESSURE)
    window.analysis.selection.select(1000 * MS, 2990 * MS)
    tool = spectrum_tool(window)
    plot_item = tool.view.plot_widget.getPlotItem()
    assert plot_item is not None
    for theme, (background, text) in (
        ("light", ("#ffffff", "#202020")),
        ("dark", ("#202020", "#eeeeee")),
    ):
        apply_theme(window, theme)
        tool.refresh()
        assert tool.view.plot_widget.backgroundBrush().color().name() == background
        assert plot_item.getAxis("left").textPen().color().name() == text
        assert plot_item.titleLabel.opts["color"] == text
        assert "pressure amplitude spectrum" in plot_item.titleLabel.text
