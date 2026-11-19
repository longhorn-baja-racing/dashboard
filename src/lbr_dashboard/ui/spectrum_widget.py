"""Line view of an amplitude spectrum or PSD with a plain-text sampling summary."""

from __future__ import annotations

from typing import Literal

import pyqtgraph as pg
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from ..core.spectrum import Spectrum

EMPTY_TITLE = "No spectrum"
Mode = Literal["amplitude", "psd"]


def summary_text(result: Spectrum) -> str:
    """Spell out sampling, scaling and the peak so the plot is never read blind."""

    peak = result.peak
    return "\n".join(
        [
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
        self.plot_widget.getAxis("bottom").setPen(pg.mkPen("#cccccc"))
        self.plot_widget.getAxis("left").setPen(pg.mkPen("#cccccc"))
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
        self.curve = None
        self.peak_marker = None
        self.plot_widget.setTitle(EMPTY_TITLE, color="#aaaaaa")
        self.plot_widget.setLabel("bottom", "frequency (Hz)")
        self.plot_widget.setLabel("left", "")
        self.summary.setText("")

    def show_error(self, name: str, message: str) -> None:
        """Show why no spectrum was computed, with no curve that could be misread."""

        self.clear()
        self.plot_widget.setTitle(f"{name}: no spectrum", color="#aaaaaa")
        self.summary.setText(message)

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
        self.plot_widget.setTitle(f"{name} {mode} spectrum", color="#aaaaaa")
        self.curve = self.plot_widget.plot(
            result.frequencies_hz, ys, pen=pg.mkPen("#4c9be8", width=1)
        )
        self.peak_marker = pg.ScatterPlotItem(
            [peak.frequency_hz], [peak_y], symbol="o", size=8, brush="#e8a24c"
        )
        self.plot_widget.addItem(self.peak_marker)
        self.summary.setText(summary_text(result))
        self.plot_widget.enableAutoRange()
