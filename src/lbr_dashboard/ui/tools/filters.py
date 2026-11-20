"""Filters tool: overlay a moving average of the selected signal on the main time plot.

The raw trace drawn by the main plot is never touched. The filtered trace is
an extra curve on the same axes (seconds since the plot origin), named with
the filter and every parameter, and listed in a small legend next to the raw
trace. It is removed when the selected signal changes, when the main plot is
reset, or when new data can no longer be filtered with the same parameters.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from html import escape

import pyqtgraph as pg
from pyqtgraph.graphicsItems.PlotItem import PlotItem
from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...core.filters import (
    ALIGNMENTS,
    DEFAULT_JITTER_TOLERANCE,
    MOVING_AVERAGE,
    FilteredSignal,
    MovingAverageParams,
    moving_average,
)
from ..analysis import AnalysisContext, AnalysisTool, is_numeric, refresh_soon
from ..filter_overlay import filtered_name, filtered_points, plot_filtered
from ..right_panel import RightPanel

DEFAULT_WINDOW = 5
MAX_WINDOW = 100_001
NS_PER_MS = 1_000_000
# Gap thresholds are edited in milliseconds with nanosecond resolution.
GAP_DECIMALS = 6
MAX_GAP_MS = 10_000_000.0


@dataclass
class _Overlay:
    identity: tuple[object, str]
    origin_ns: int
    params: MovingAverageParams
    curve: pg.PlotDataItem
    legend: pg.LegendItem


class FilterTool(QWidget):
    """Choose moving-average parameters and draw the result over the raw trace."""

    def __init__(
        self,
        context: AnalysisContext,
        panel: RightPanel | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setMinimumSize(320, 220)
        self.context = context
        self.panel = panel
        self.overlay: _Overlay | None = None
        self.result: FilteredSignal | None = None

        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.window_samples = QSpinBox()
        self.window_samples.setRange(1, MAX_WINDOW)
        self.window_samples.setValue(DEFAULT_WINDOW)
        self.window_samples.setSuffix(" samples")
        self.window_samples.setToolTip(
            "Number of consecutive samples averaged with equal weight. "
            "A centered window must be odd."
        )
        self.alignment = QComboBox()
        for alignment in ALIGNMENTS:
            self.alignment.addItem(alignment, alignment)
        self.alignment.setToolTip(
            "centered: no delay, uses samples on both sides. "
            "trailing: past samples only, delayed by (window - 1) / 2 periods."
        )
        self.jitter = QDoubleSpinBox()
        self.jitter.setRange(0.0, 99.9)
        self.jitter.setDecimals(2)
        self.jitter.setSuffix(" %")
        self.jitter.setValue(DEFAULT_JITTER_TOLERANCE * 100)
        self.jitter.setToolTip(
            "Largest allowed deviation of a sample spacing from the nominal period."
        )
        self.split_gaps = QCheckBox("Split at gaps of at least")
        self.gap_ms = QDoubleSpinBox()
        self.gap_ms.setDecimals(GAP_DECIMALS)
        self.gap_ms.setRange(10.0**-GAP_DECIMALS, MAX_GAP_MS)
        self.gap_ms.setValue(100.0)
        self.gap_ms.setSuffix(" ms")
        self.gap_ms.setEnabled(False)
        self.split_gaps.toggled.connect(self.gap_ms.setEnabled)
        gap_row = QHBoxLayout()
        gap_row.addWidget(self.split_gaps)
        gap_row.addWidget(self.gap_ms, 1)

        self.apply_button = QPushButton("Overlay on main plot")
        self.apply_button.clicked.connect(self.apply)
        self.remove_button = QPushButton("Remove overlay")
        self.remove_button.clicked.connect(lambda: self.remove_overlay("Overlay removed."))
        buttons = QHBoxLayout()
        buttons.addWidget(self.apply_button)
        buttons.addWidget(self.remove_button)
        buttons.addStretch(1)
        self.status = QLabel()
        self.status.setWordWrap(True)

        form = QFormLayout()
        form.addRow("Filter", QLabel("Moving average"))
        form.addRow("Window", self.window_samples)
        form.addRow("Alignment", self.alignment)
        form.addRow("Jitter tolerance", self.jitter)
        form.addRow("Time gaps", gap_row)
        layout = QVBoxLayout(self)
        layout.addWidget(self.hint)
        layout.addLayout(form)
        layout.addLayout(buttons)
        layout.addWidget(self.status)
        layout.addStretch(1)

        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.refresh)
        context.snapshot_changed.connect(refresh_soon(self.timer))
        context.signal_changed.connect(self._signal_changed)
        context.theme_changed.connect(self._restyle_legend)
        if panel is not None:
            panel.plot_reset.connect(self._plot_reset)
        self.refresh()

    # Parameters -----------------------------------------------------------------

    def params(self) -> dict[str, object]:
        """Keyword arguments for ``moving_average`` from the current controls."""

        gap_ns = round(self.gap_ms.value() * NS_PER_MS) if self.split_gaps.isChecked() else None
        return {
            "window_samples": self.window_samples.value(),
            "alignment": self.alignment.currentData(),
            "jitter_tolerance": round(self.jitter.value(), 2) / 100,
            "gap_ns": gap_ns,
        }

    def configuration(self) -> dict[str, object]:
        """Filter parameters and the signal whose overlay is shown (or None)."""

        return {
            "filter": MOVING_AVERAGE,
            **self.params(),
            "signal": self.overlay.identity[1] if self.overlay is not None else None,
        }

    def restore_configuration(self, config: object) -> None:
        """Apply a saved configuration; re-draw its overlay if that signal is selected.

        Raises ``ValueError`` (and changes nothing) for unknown filters, wrong
        field types or parameters ``moving_average`` would reject.
        """

        if not isinstance(config, Mapping):
            raise ValueError("Filter configuration must be a mapping.")
        if config.get("filter", MOVING_AVERAGE) != MOVING_AVERAGE:
            raise ValueError(f"Unknown filter {config.get('filter')!r}.")
        signal = config.get("signal")
        if signal is not None and not isinstance(signal, str):
            raise ValueError("Filter configuration signal must be a signal id or None.")
        # The filter validates its own parameters (an empty input runs only
        # that check), so restored settings follow the same rules as typed ones.
        checked = moving_average(
            [],
            [],
            window_samples=config.get("window_samples", DEFAULT_WINDOW),  # type: ignore[arg-type]
            alignment=config.get("alignment", "centered"),  # type: ignore[arg-type]
            jitter_tolerance=config.get(  # type: ignore[arg-type]
                "jitter_tolerance", DEFAULT_JITTER_TOLERANCE
            ),
            gap_ns=config.get("gap_ns"),  # type: ignore[arg-type]
        ).params
        if checked.window_samples > MAX_WINDOW:
            raise ValueError(f"window_samples must be at most {MAX_WINDOW}.")
        if checked.gap_ns is not None and checked.gap_ns > MAX_GAP_MS * NS_PER_MS:
            raise ValueError(f"gap_ns must be at most {int(MAX_GAP_MS * NS_PER_MS)}.")
        self.window_samples.setValue(checked.window_samples)
        self.alignment.setCurrentIndex(self.alignment.findData(checked.alignment))
        self.jitter.setValue(checked.jitter_tolerance * 100)
        self.split_gaps.setChecked(checked.gap_ns is not None)
        if checked.gap_ns is not None:
            self.gap_ms.setValue(checked.gap_ns / NS_PER_MS)
        if signal is None:
            self.remove_overlay("")
        elif signal == self.context.selected_id:
            self.apply()

    # Overlay --------------------------------------------------------------------

    def apply(self) -> FilteredSignal | None:
        """Filter the selected signal with the current parameters and draw it."""

        return self._draw(self.params())

    def remove_overlay(self, message: str = "") -> None:
        overlay, self.overlay, self.result = self.overlay, None, None
        if overlay is not None:
            plot_item = self._plot_item()
            if plot_item is not None:
                # A no-op when a plot reset already removed the curve.
                plot_item.removeItem(overlay.curve)
            scene = overlay.legend.scene()
            if scene is not None:
                scene.removeItem(overlay.legend)
        if message or overlay is not None:
            self.status.setText(message)
        self._update_buttons()

    def refresh(self) -> None:
        """Re-check the selection; keep an active overlay current with new data."""

        self.timer.stop()
        self._update_hint()
        overlay = self.overlay
        if overlay is None:
            return
        if overlay.identity != self._plotted_identity() or (
            self.panel is not None and self.panel.time_plot.origin_ns != overlay.origin_ns
        ):
            self.remove_overlay("Overlay removed: the main plot no longer shows that signal.")
            return
        self._draw(overlay.params.as_dict())

    # Internals ------------------------------------------------------------------

    def _draw(self, params: dict[str, object]) -> FilteredSignal | None:
        context = self.context
        descriptor = context.descriptor(context.selected_id)
        if descriptor is None or not is_numeric(descriptor):
            self.remove_overlay("Select a numeric signal first.")
            return None
        plot_item = self._plot_item()
        identity = (context.session, descriptor.identifier)
        if plot_item is None or self.panel is None or self._plotted_identity() != identity:
            self.remove_overlay(f"The main plot is not showing {descriptor.display_name} yet.")
            return None
        times, values = context.numeric_series(descriptor.identifier)
        try:
            result = moving_average(
                times,
                values,
                signal=descriptor.identifier,
                unit=descriptor.canonical_unit or "",
                **params,  # type: ignore[arg-type]
            )
        except ValueError as error:
            self.remove_overlay(f"Not filtered: {error}. The raw trace is unchanged.")
            return None
        origin = self.panel.time_plot.origin_ns
        overlay = self.overlay
        if overlay is not None and (overlay.identity, overlay.origin_ns) == (identity, origin):
            # New data for the same trace: update the curve in place.
            name = filtered_name(result, descriptor.display_name)
            xs, ys = filtered_points(result, origin)
            overlay.curve.setData(xs, ys, connect="finite", name=name)
            overlay.legend.removeItem(overlay.curve)
            overlay.legend.addItem(overlay.curve, escape(name))
            overlay.params = result.params
            return self._shown(result)
        self.remove_overlay()
        curve = plot_filtered(plot_item, result, name=descriptor.display_name, origin_ns=origin)
        curve.setZValue(5)
        curve.setDownsampling(auto=True, method="peak")
        curve.setClipToView(True)
        legend = self._legend(plot_item)
        raw = self.panel.time_plot.curve
        if raw is not None:
            unit = f" [{result.unit}]" if result.unit else ""
            legend.addItem(raw, escape(f"{descriptor.display_name}{unit} raw"))
        legend.addItem(curve, escape(curve.name() or ""))
        self.overlay = _Overlay(identity, origin, result.params, curve, legend)
        return self._shown(result)

    def _shown(self, result: FilteredSignal) -> FilteredSignal:
        self.result = result
        self.status.setText(_summary(result))
        self._update_buttons()
        return result

    def _legend(self, plot_item: PlotItem) -> pg.LegendItem:
        background, text = self.context.plot_colors
        brush = QColor(background)
        brush.setAlpha(210)
        legend = pg.LegendItem(offset=(-10, 10), labelTextColor=text, brush=pg.mkBrush(brush))
        legend.setParentItem(plot_item.getViewBox())
        legend.setZValue(20)
        return legend

    def _restyle_legend(self) -> None:
        if self.overlay is not None:
            background, text = self.context.plot_colors
            brush = QColor(background)
            brush.setAlpha(210)
            self.overlay.legend.setBrush(pg.mkBrush(brush))
            self.overlay.legend.setLabelTextColor(text)

    def _plot_item(self) -> PlotItem | None:
        return self.panel.plot_widget.getPlotItem() if self.panel is not None else None

    def _plotted_identity(self) -> tuple[object, str] | None:
        if self.panel is None or self.panel.time_plot.curve is None:
            return None
        identity = self.panel.time_plot.identity
        return identity if isinstance(identity, tuple) else None

    def _signal_changed(self) -> None:
        if self.overlay is not None:
            self.remove_overlay("Overlay removed: a different signal was selected.")
        self._update_hint()

    def _plot_reset(self) -> None:
        if self.overlay is not None:
            self.remove_overlay("Overlay removed: the main plot was reset.")

    def _update_hint(self) -> None:
        context = self.context
        descriptor = context.descriptor(context.selected_id)
        if descriptor is None:
            self.hint.setText("Select a numeric signal in the browser to filter it.")
        elif not is_numeric(descriptor):
            self.hint.setText(
                f"{descriptor.display_name} is {descriptor.data_type}; "
                "filters need a numeric signal."
            )
        else:
            self.hint.setText(
                f"{descriptor.display_name} ({descriptor.identifier}): the filtered trace "
                "is drawn over the raw trace on the main plot, which stays unchanged."
            )
        self._update_buttons()

    def _update_buttons(self) -> None:
        self.apply_button.setEnabled(is_numeric(self.context.descriptor(self.context.selected_id)))
        self.remove_button.setEnabled(self.overlay is not None)


def _summary(result: FilteredSignal) -> str:
    unit = f" [{result.unit}]" if result.unit else ""
    lines = [
        f"Showing {result.signal}{unit}: moving average of {result.params.window_samples} "
        f"samples, {result.params.alignment}.",
        f"{result.output_count} of {result.input_count} samples have output "
        f"({result.missing_count} missing, {result.edge_count} at segment edges, "
        f"{result.overflow_count} overflowed; {result.segment_count} segment(s)).",
    ]
    if result.nominal_period_ns is not None:
        deviation = result.max_spacing_deviation or 0.0
        timing = (
            f"Nominal period {result.nominal_period_ns / NS_PER_MS:g} ms, "
            f"largest spacing deviation {deviation * 100:.1f}% "
            f"(tolerance {result.params.jitter_tolerance * 100:g}%)"
        )
        if result.window_span_ns is not None:
            timing += f", window {result.window_span_ns / NS_PER_MS:g} ms"
        if result.delay_ns is not None:
            timing += f", delay {result.delay_ns / NS_PER_MS:g} ms"
        lines.append(timing + ".")
    else:
        lines.append("No nominal period: too few samples between gaps.")
    if result.params.gap_ns is not None:
        gap_ms = result.params.gap_ns / NS_PER_MS
        lines.append(f"Spacings of at least {gap_ms:g} ms split segments.")
    return "\n".join(lines)


def _create(context: AnalysisContext) -> FilterTool:
    # The main window owns the context and the right panel with the time plot.
    window = context.parent()
    panel = getattr(window, "right_panel", None)
    return FilterTool(context, panel if isinstance(panel, RightPanel) else None)


TOOL = AnalysisTool("filters", "Filters", _create)
