"""Line view of an amplitude spectrum or PSD with a plain-text sampling summary."""

from __future__ import annotations

from typing import Literal

import pyqtgraph as pg
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from ..core.spectrum import Spectrum

EMPTY_TITLE = "No spectrum"
Mode = Literal["amplitude", "psd"]


def scaling_text(result: Spectrum, mode: Mode = "amplitude") -> str:
    """Say what the plotted y values are and which window correction they carry."""

    if mode == "psd":
        return (
            f"Scaling: one-sided PSD in {result.psd_unit}, corrected for window noise power "
            f"(noise bandwidth {result.noise_bandwidth_bins:.3g} bins)"
        )
    unit = f" in {result.amplitude_unit}" if result.unit else ""
    return (
        f"Scaling: one-sided peak amplitude{unit}, corrected for window coherent gain "
        f"(gain {result.coherent_gain:.3g})"
    )


def summary_text(result: Spectrum, mode: Mode = "amplitude") -> str:
    """Spell out sampling, scaling and the peak so the plot is never read blind."""

    peak = result.peak
    return "\n".join(
        [
            scaling_text(result, mode),
            f"Sample rate: {result.sample_rate_hz:.6g} Hz  "
            f"Resolution: {result.resolution_hz:.6g} Hz  "
            f"Nyquist: {result.nyquist_hz:.6g} Hz  Samples: {result.sample_count}",
            f"Window: {result.window}  Detrend: {result.detrend}  "
            f"Max spacing jitter: {result.max_jitter:.3%}",
            f"Peak: {peak.frequency_hz:.6g} Hz  amplitude {peak.amplitude:.6g} "
            f"{result.amplitude_unit}  PSD {peak.psd:.6g} {result.psd_unit}".rstrip(),
        ]
    )


class SpectrumWidget(QWidget):
    """Show one :class:`Spectrum` as amplitude or PSD versus frequency."""

    def __init__(self) -> None:
        super().__init__()
        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setBackground("#3a3a3a")
        self.plot_widget.showGrid(x=True, y=True, alpha=0.2)
        for name in ("bottom", "left"):
            axis = self.plot_widget.getAxis(name)
            axis.setPen(pg.mkPen("#cccccc"))
            axis.setTextPen(pg.mkPen("#cccccc"))
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.curve: pg.PlotDataItem | None = None
        self.peak_marker: pg.ScatterPlotItem | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.plot_widget)
        layout.addWidget(self.summary)
        self.setStyleSheet("background-color: #3a3a3a; color: #ffffff;")
        self.clear()

    def clear(self) -> None:
        """Remove the curve, peak marker and summary."""

        self.plot_widget.clear()
        self.plot_widget.show()
        self.curve = None
        self.peak_marker = None
        self._set_title(EMPTY_TITLE)
        self.plot_widget.setLabel("bottom", "frequency (Hz)")
        self.plot_widget.setLabel("left", "")
        self.summary.setText("")

    def show_error(self, name: str, message: str) -> None:
        """Show why no spectrum was computed in place of the plot, so nothing can be misread."""

        self.clear()
        self._set_title(f"{name}: no spectrum")
        self.plot_widget.hide()
        self.summary.setText(f"{name}: no spectrum. {message}")

    def show_spectrum(self, result: Spectrum, name: str, mode: Mode = "amplitude") -> None:
        """Draw ``result`` for the signal ``name`` as amplitude or PSD."""

        if mode not in ("amplitude", "psd"):
            raise ValueError(f"mode must be 'amplitude' or 'psd', got {mode!r}")
        self.clear()
        if mode == "psd":
            ys = result.psd
            label = f"PSD ({result.psd_unit})"
        else:
            ys = result.amplitude
            label = f"amplitude ({result.amplitude_unit})" if result.unit else "amplitude"
        peak = result.peak
        peak_y = peak.psd if mode == "psd" else peak.amplitude
        self.plot_widget.setLabel("left", label)
        self._set_title(f"{name} {mode} spectrum")
        self.curve = self.plot_widget.plot(
            result.frequencies_hz, ys, pen=pg.mkPen("#4c9be8", width=1)
        )
        self.peak_marker = pg.ScatterPlotItem(
            [peak.frequency_hz], [peak_y], symbol="o", size=8, brush="#e8a24c"
        )
        self.plot_widget.addItem(self.peak_marker)
        self.summary.setText(summary_text(result, mode))
        self.plot_widget.enableAutoRange()

    def _set_title(self, text: str) -> None:
        # Follow the axis text color so a themed plot keeps a readable title.
        color = self.plot_widget.getAxis("left").textPen().color().name()
        self.plot_widget.setTitle(text, color=color)
