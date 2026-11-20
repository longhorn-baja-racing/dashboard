"""XY plot tool: one numeric signal of the session against another."""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtGui import QStandardItemModel
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QGridLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from ...core import SignalDescriptor
from ...core.interval import TimeInterval
from ...core.xy_pairing import AlignmentPolicy, pair_signals
from ..analysis import AnalysisContext, AnalysisTool, is_numeric
from ..xy_plot import DEFAULT_MAX_POINTS, XYPlotWidget, format_duration_ns

DEFAULT_TOLERANCE_NS = 100_000_000
MAX_TOLERANCE_NS = 3_600_000_000_000
_MODES = {
    AlignmentPolicy.NEAREST: "Nearest sample",
    AlignmentPolicy.PREVIOUS: "Previous sample (hold)",
}


def signal_label(descriptor: SignalDescriptor) -> str:
    """Return ``name (unit) - id``, leaving out the unit when it is unknown."""

    unit = f" ({descriptor.canonical_unit})" if descriptor.canonical_unit else ""
    return f"{descriptor.display_name}{unit} - {descriptor.identifier}"


def unavailable_label(signal_id: str) -> str:
    return f"{signal_id} (unavailable)"


class XYTool(QWidget):
    """Pair two numeric signals by time and draw Y against X.

    The chosen signal ids are kept even when the loaded session does not have
    them, so a restored configuration shows them as unavailable instead of
    silently switching to other signals. Refreshes are coalesced to at most one
    every 100 ms while data streams.
    """

    def __init__(
        self,
        context: AnalysisContext,
        parent: QWidget | None = None,
        *,
        max_points: int = DEFAULT_MAX_POINTS,
    ) -> None:
        super().__init__(parent)
        self.context = context
        self.x_id: str | None = None
        self.y_id: str | None = None
        self.policy = AlignmentPolicy.NEAREST
        self.tolerance_ns = DEFAULT_TOLERANCE_NS
        self._choices: tuple[tuple[str, str], ...] | None = None
        self._plotted: tuple[object, ...] | None = None

        self.x_choice = QComboBox()
        self.y_choice = QComboBox()
        for choice in (self.x_choice, self.y_choice):
            choice.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
            )
            choice.setMinimumContentsLength(18)
        self.x_choice.setAccessibleName("X signal")
        self.y_choice.setAccessibleName("Y signal")
        self.mode_choice = QComboBox()
        self.mode_choice.setAccessibleName("Pairing mode")
        for policy, text in _MODES.items():
            self.mode_choice.addItem(text, policy.value)
        self.tolerance = QDoubleSpinBox()
        self.tolerance.setAccessibleName("Pairing tolerance")
        self.tolerance.setDecimals(3)
        self.tolerance.setRange(0.0, MAX_TOLERANCE_NS / 1_000_000)
        self.tolerance.setSuffix(" ms")
        self.tolerance.setValue(DEFAULT_TOLERANCE_NS / 1_000_000)
        self.tolerance.setToolTip(
            "Largest time difference between an X sample and the Y sample it is paired with."
        )
        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.plot = XYPlotWidget(max_points=max_points)
        self.plot.plot_widget.setMinimumSize(240, 160)
        context.style_plot(self.plot.plot_widget)

        controls = QGridLayout()
        controls.addWidget(QLabel("X"), 0, 0)
        controls.addWidget(self.x_choice, 0, 1)
        controls.addWidget(QLabel("Pairing"), 0, 2)
        controls.addWidget(self.mode_choice, 0, 3)
        controls.addWidget(QLabel("Y"), 1, 0)
        controls.addWidget(self.y_choice, 1, 1)
        controls.addWidget(QLabel("Tolerance"), 1, 2)
        controls.addWidget(self.tolerance, 1, 3)
        controls.setColumnStretch(1, 1)
        layout = QVBoxLayout(self)
        layout.addLayout(controls)
        layout.addWidget(self.hint)
        layout.addWidget(self.plot, 1)
        self.setMinimumSize(360, 300)

        self.x_choice.currentIndexChanged.connect(self._choose_x)
        self.y_choice.currentIndexChanged.connect(self._choose_y)
        self.mode_choice.currentIndexChanged.connect(self._choose_mode)
        self.tolerance.valueChanged.connect(self._choose_tolerance)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.refresh)
        for change in (context.snapshot_changed, context.interval_changed):
            change.connect(self.timer.start)
        context.cursor_changed.connect(self._highlight)
        self.refresh()

    # Configuration -------------------------------------------------------

    def configuration(self) -> dict[str, object]:
        """Return the chosen signal ids, pairing mode and tolerance."""

        return {
            "x_signal_id": self.x_id,
            "y_signal_id": self.y_id,
            "mode": self.policy.value,
            "tolerance_ns": self.tolerance_ns,
        }

    def restore_configuration(self, config: object) -> None:
        """Apply a saved configuration; signals missing from the session show as unavailable.

        Raises ``ValueError`` for a malformed configuration and changes nothing then.
        """

        if not isinstance(config, dict):
            raise ValueError("XY plot configuration must be a mapping.")
        ids: list[str | None] = []
        for key in ("x_signal_id", "y_signal_id"):
            value = config.get(key)
            if value is not None and not (isinstance(value, str) and value.strip()):
                raise ValueError(f"{key} must be a signal id or None.")
            ids.append(value)
        try:
            policy = AlignmentPolicy(config.get("mode", AlignmentPolicy.NEAREST.value))
        except ValueError:
            raise ValueError("mode must be 'nearest' or 'previous'.") from None
        tolerance = config.get("tolerance_ns", DEFAULT_TOLERANCE_NS)
        if (
            isinstance(tolerance, bool)
            or not isinstance(tolerance, int)
            or not 0 <= tolerance <= MAX_TOLERANCE_NS
        ):
            raise ValueError(f"tolerance_ns must be an integer from 0 to {MAX_TOLERANCE_NS}.")
        self.x_id, self.y_id = ids
        self.policy = policy
        self.tolerance_ns = tolerance
        self.mode_choice.blockSignals(True)
        self.mode_choice.setCurrentIndex(self.mode_choice.findData(policy.value))
        self.mode_choice.blockSignals(False)
        self.tolerance.blockSignals(True)
        self.tolerance.setValue(tolerance / 1_000_000)
        self.tolerance.blockSignals(False)
        self._choices = None
        self.refresh()

    # Choices -------------------------------------------------------------

    def _choose_x(self, index: int) -> None:
        self.x_id = self.x_choice.itemData(index)
        self.refresh()

    def _choose_y(self, index: int) -> None:
        self.y_id = self.y_choice.itemData(index)
        self.refresh()

    def _choose_mode(self, index: int) -> None:
        self.policy = AlignmentPolicy(self.mode_choice.itemData(index))
        self.refresh()

    def _choose_tolerance(self, value: float) -> None:
        self.tolerance_ns = round(value * 1_000_000)
        self.refresh()

    def _numeric(self) -> tuple[SignalDescriptor, ...]:
        return tuple(item for item in self.context.descriptors if is_numeric(item))

    def _sync_choices(self, numeric: tuple[SignalDescriptor, ...]) -> None:
        """Fill both choosers, keeping the chosen ids even when they are unavailable."""

        if self.x_id is None and numeric:
            self.x_id = numeric[0].identifier
        if self.y_id is None and numeric:
            self.y_id = numeric[min(1, len(numeric) - 1)].identifier
        entries = tuple((item.identifier, signal_label(item)) for item in numeric)
        if entries + ((self.x_id or "", self.y_id or ""),) == self._choices:
            return
        self._choices = entries + ((self.x_id or "", self.y_id or ""),)
        for choice, wanted in ((self.x_choice, self.x_id), (self.y_choice, self.y_id)):
            choice.blockSignals(True)
            choice.clear()
            for identifier, text in entries:
                choice.addItem(text, identifier)
            if wanted is not None and choice.findData(wanted) < 0:
                choice.addItem(unavailable_label(wanted), wanted)
                model = choice.model()
                if isinstance(model, QStandardItemModel):
                    item = model.item(choice.count() - 1)
                    if item is not None:
                        item.setEnabled(False)
            choice.setCurrentIndex(choice.findData(wanted) if wanted is not None else -1)
            choice.blockSignals(False)

    # Plotting ------------------------------------------------------------

    def refresh(self) -> None:
        self.timer.stop()
        context = self.context
        numeric = self._numeric()
        self._sync_choices(numeric)
        if context.snapshot is None:
            self._show_empty("Load a session to plot one numeric signal against another.")
            return
        if not numeric:
            self._show_empty("This session has no numeric signals to plot against each other.")
            return
        x = context.descriptor(self.x_id)
        y = context.descriptor(self.y_id)
        missing = [
            f"{axis} signal {wanted} is not in this session"
            for axis, wanted, descriptor in (("X", self.x_id, x), ("Y", self.y_id, y))
            if descriptor is None or not is_numeric(descriptor)
        ]
        if missing or x is None or y is None:
            self._show_empty("; ".join(missing) + ". Choose another signal.")
            return
        domains = context.snapshot.domains
        x_domain, y_domain = domains.get(x.identifier), domains.get(y.identifier)
        if x_domain is not None and y_domain is not None and x_domain != y_domain:
            self._show_empty(
                f"{x.display_name} and {y.display_name} use different time bases "
                f"({x_domain} and {y_domain}), so their samples cannot be paired."
            )
            return

        interval = context.interval
        tolerance = self.tolerance_ns
        x_times, x_values = context.numeric_series(x.identifier, interval)
        # The other signal may be matched up to the tolerance outside the interval.
        around = (
            None
            if interval is None
            else TimeInterval(interval.start_ns - tolerance, interval.end_ns + tolerance)
        )
        y_times, y_values = context.numeric_series(y.identifier, around)
        series = pair_signals(
            x_times,
            x_values,
            y_times,
            y_values,
            policy=self.policy,
            tolerance_ns=tolerance,
            start_ns=None if interval is None else interval.start_ns,
            end_ns=None if interval is None else interval.end_ns,
            x_unit=x.canonical_unit,
            y_unit=y.canonical_unit,
        )
        key = (x.identifier, y.identifier, self.policy, tolerance, interval)
        self.plot.show_pairs(series, x.display_name, y.display_name, fit=key != self._plotted)
        self._plotted = key
        self.hint.setText(
            f"{y.display_name} against {x.display_name} over "
            f"{self._interval_text(interval)}. Pairs follow each X sample; "
            f"the tolerance is {format_duration_ns(tolerance)}."
        )
        self._highlight()

    def _interval_text(self, interval: TimeInterval | None) -> str:
        bounds = self.context.bounds
        if interval is None or bounds is None:
            return "the whole session"
        start = (interval.start_ns - bounds.start_ns) / 1e9
        end = (interval.end_ns - bounds.start_ns) / 1e9
        return f"the selected interval {start:.3f} s to {end:.3f} s"

    def _show_empty(self, message: str) -> None:
        self._plotted = None
        self.hint.setText(message)
        self.plot.clear("No pairs to plot.")

    def _highlight(self) -> None:
        """Mark the pair nearest the replay cursor while it is inside the interval."""

        cursor = self.context.cursor_ns
        interval = self.context.interval
        if cursor is not None and interval is not None and not interval.contains(cursor):
            cursor = None
        self.plot.highlight_time(cursor)


TOOL = AnalysisTool("xy", "XY plot", XYTool)
