"""Spectrum tool: amplitude spectrum or PSD of the selected signal over the shared interval."""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QComboBox, QFormLayout, QLabel, QVBoxLayout, QWidget

from ...core.spectrum import DETRENDS, WINDOWS, SamplingError, spectrum
from ...core.statistics import format_seconds
from ..analysis import AnalysisContext, AnalysisTool, is_numeric
from ..spectrum_widget import SpectrumWidget

WINDOW_LABELS = {"hann": "Hann", "none": "None (rectangular)"}
DETREND_LABELS = {"mean": "Remove mean", "linear": "Remove linear trend", "none": "None"}
SCALE_LABELS = {"amplitude": "Amplitude (peak)", "psd": "Power spectral density"}
DEFAULTS = {"window": "hann", "detrend": "mean", "scale": "amplitude"}


def _combo(labels: dict[str, str], tooltip: str) -> QComboBox:
    combo = QComboBox()
    for value, label in labels.items():
        combo.addItem(label, value)
    combo.setToolTip(tooltip)
    return combo


class SpectrumTool(QWidget):
    """Follow the selected signal and interval; recompute at most every 100 ms."""

    def __init__(self, context: AnalysisContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self.hint = QLabel()
        self.hint.setWordWrap(True)
        assert set(WINDOW_LABELS) == set(WINDOWS) and set(DETREND_LABELS) == set(DETRENDS)
        self.window_choice = _combo(
            WINDOW_LABELS, "Hann leaks less and reads off-bin tones closer to their amplitude"
        )
        self.detrend_choice = _combo(DETREND_LABELS, "Applied before the window")
        self.scale_choice = _combo(
            SCALE_LABELS, "Amplitude reads tones; PSD compares broadband noise levels"
        )
        options = QFormLayout()
        options.addRow("Window", self.window_choice)
        options.addRow("Detrend", self.detrend_choice)
        options.addRow("Scale", self.scale_choice)
        self.view = SpectrumWidget()
        # Let the application theme color the text; the plot follows it via style_plot.
        self.view.setStyleSheet("")
        self.view.plot_widget.setMinimumSize(320, 200)
        context.style_plot(self.view.plot_widget)
        layout = QVBoxLayout(self)
        layout.addWidget(self.hint)
        layout.addLayout(options)
        layout.addWidget(self.view, 1)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.refresh)
        for change in (context.snapshot_changed, context.signal_changed, context.interval_changed):
            change.connect(self.timer.start)
        for combo in (self.window_choice, self.detrend_choice, self.scale_choice):
            combo.currentIndexChanged.connect(self.timer.start)
        self.refresh()

    def configuration(self) -> dict[str, str]:
        """Return the chosen window, detrend and scale for saving in a workspace."""

        return {
            "window": self.window_choice.currentData(),
            "detrend": self.detrend_choice.currentData(),
            "scale": self.scale_choice.currentData(),
        }

    def restore_configuration(self, config: object) -> None:
        """Apply a saved configuration; missing keys keep their default.

        Raises ValueError, changing nothing, for a non-dict or an unknown value.
        """

        if not isinstance(config, dict):
            raise ValueError("Spectrum configuration must be a dict.")
        chosen = {**DEFAULTS, **{key: config[key] for key in DEFAULTS if key in config}}
        combos = {
            "window": self.window_choice,
            "detrend": self.detrend_choice,
            "scale": self.scale_choice,
        }
        indexes = {}
        for key, combo in combos.items():
            index = combo.findData(chosen[key]) if isinstance(chosen[key], str) else -1
            if index < 0:
                raise ValueError(f"Unknown spectrum {key}: {chosen[key]!r}")
            indexes[key] = index
        for key, combo in combos.items():
            combo.setCurrentIndex(indexes[key])
        self.refresh()

    def refresh(self) -> None:
        self.timer.stop()
        context = self.context
        descriptor = context.descriptor(context.selected_id)
        if descriptor is None:
            self.hint.setText("Select a numeric signal in the browser to see its spectrum.")
            self.view.clear()
            return
        name = descriptor.display_name
        if not is_numeric(descriptor):
            self.hint.setText(
                f"{name} is {descriptor.data_type}; a spectrum needs a numeric signal."
            )
            self.view.clear()
            return
        interval = context.interval
        times, values = context.numeric_series(descriptor.identifier, interval)
        if interval is None:
            bounds = context.bounds
            where = "whole session (no interval selected)"
            if bounds is not None:
                where += f", {format_seconds(bounds.start_ns)} to {format_seconds(bounds.end_ns)}"
        else:
            where = (
                f"interval {format_seconds(interval.start_ns)} to {format_seconds(interval.end_ns)}"
            )
        self.hint.setText(
            f"{name} ({descriptor.identifier}): {where}, {len(times)} sample(s). "
            "Select a gap-free range with Analysis > Select visible time range."
        )
        try:
            result = spectrum(
                times,
                values,
                unit=descriptor.canonical_unit or "",
                window=self.window_choice.currentData(),
                detrend=self.detrend_choice.currentData(),
            )
        except SamplingError as error:
            message = str(error)
            if error.index is not None and 0 <= error.index < len(times):
                message += f" (at {format_seconds(times[error.index])})"
            self.view.show_error(name, message)
            return
        self.view.show_spectrum(result, name, self.scale_choice.currentData())


TOOL = AnalysisTool("spectrum", "Spectrum", SpectrumTool)
