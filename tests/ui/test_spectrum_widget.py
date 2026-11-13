"""The spectrum widget labels its axes and spells out sampling and the peak."""

import numpy as np
import pyqtgraph as pg
import pytest
from pytestqt.qtbot import QtBot

from lbr_dashboard.core.spectrum import spectrum
from lbr_dashboard.ui.spectrum_widget import EMPTY_TITLE, SpectrumWidget


@pytest.fixture
def widget(qtbot: QtBot) -> SpectrumWidget:
    view = SpectrumWidget()
    qtbot.addWidget(view)
    view.show()
    return view


def _sine_spectrum(unit: str = "V"):
    rate, count = 1000.0, 1000
    times = [index * 1_000_000 for index in range(count)]
    values = list(2.0 * np.sin(2 * np.pi * 50.0 * np.arange(count) / rate))
    return spectrum(times, values, unit=unit)


def _title(view: SpectrumWidget) -> str:
    plot = view.plot_widget.getPlotItem()
    assert plot is not None
    return plot.titleLabel.text


def test_amplitude_curve_labels_peak_and_summary(widget: SpectrumWidget) -> None:
    result = _sine_spectrum()
    widget.show_spectrum(result, "shock voltage")
    assert widget.curve is not None and widget.peak_marker is not None
    xs, ys = widget.curve.getData()
    assert xs is not None and ys is not None
    assert np.array_equal(xs, result.frequencies_hz)
    assert np.array_equal(ys, result.amplitude)
    marker_x, marker_y = widget.peak_marker.getData()
    assert float(marker_x[0]) == pytest.approx(50.0)
    assert float(marker_y[0]) == pytest.approx(2.0)
    assert widget.plot_widget.getAxis("bottom").labelText == "frequency (Hz)"
    assert widget.plot_widget.getAxis("left").labelText == "amplitude (V)"
    assert "shock voltage amplitude spectrum" in _title(widget)
    text = widget.summary.text()
    assert "Sample rate: 1000 Hz" in text
    assert "Resolution: 1 Hz" in text
    assert "Nyquist: 500 Hz" in text
    assert "Samples: 1000" in text
    assert "Window: hann  Detrend: mean" in text
    assert "Peak: 50 Hz  amplitude 2 V" in text
    assert "V^2/Hz" in text


def test_psd_mode_plots_density_with_its_unit(widget: SpectrumWidget) -> None:
    result = _sine_spectrum("m/s^2")
    widget.show_spectrum(result, "accel", mode="psd")
    assert widget.curve is not None
    _, ys = widget.curve.getData()
    assert ys is not None
    assert np.array_equal(ys, result.psd)
    assert widget.plot_widget.getAxis("left").labelText == "PSD ((m/s^2)^2/Hz)"


def test_unitless_signal_has_plain_amplitude_label(widget: SpectrumWidget) -> None:
    widget.show_spectrum(_sine_spectrum(""), "ratio")
    assert widget.plot_widget.getAxis("left").labelText == "amplitude"


def test_error_shows_message_and_no_curve(widget: SpectrumWidget) -> None:
    widget.show_spectrum(_sine_spectrum(), "a")
    widget.show_error("rpm", "sample spacing is not uniform; resample")
    assert widget.curve is None and widget.peak_marker is None
    plot = widget.plot_widget.getPlotItem()
    assert plot is not None
    assert not [item for item in plot.items if isinstance(item, pg.PlotDataItem)]
    assert "rpm: no spectrum" in _title(widget)
    assert "resample" in widget.summary.text()


def test_replace_clear_and_bad_mode(widget: SpectrumWidget) -> None:
    widget.show_spectrum(_sine_spectrum(), "a")
    widget.show_spectrum(_sine_spectrum(), "b", mode="psd")
    plot = widget.plot_widget.getPlotItem()
    assert plot is not None
    assert [item for item in plot.items if isinstance(item, pg.PlotDataItem)] == [widget.curve]
    widget.clear()
    assert widget.curve is None
    assert widget.summary.text() == ""
    assert EMPTY_TITLE in _title(widget)
    with pytest.raises(ValueError, match="mode"):
        widget.show_spectrum(_sine_spectrum(), "c", mode="log")  # type: ignore[arg-type]
