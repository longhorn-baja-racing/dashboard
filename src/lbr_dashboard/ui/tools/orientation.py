"""Orientation tool: attitude of a declared quaternion signal at the replay cursor."""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QGridLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from ...core.orientation import (
    AttitudeReading,
    AttitudeState,
    OrientationError,
    QuaternionLayout,
    attitude_at,
    default_max_age_ns,
    quaternion_layout,
)
from ..analysis import AnalysisContext, AnalysisTool
from ..attitude_view import AttitudeView

# Redraw at most this often while the cursor plays, independently of acquisition.
REFRESH_MS = 33
MAX_AGE_LIMIT_MS = 3_600_000.0


def _calibration_text(calibration) -> str:
    if not calibration:
        return "not declared"
    return ", ".join(f"{key} = {value}" for key, value in sorted(calibration.items()))


class OrientationTool(QWidget):
    """Show roll, pitch and yaw and a drawn body for the reading at the cursor.

    Only a fresh, available, unit-norm reading is drawn as the current pose.
    Missing, stale and invalid readings show their state instead; nothing is
    interpolated across gaps.
    """

    def __init__(self, context: AnalysisContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self.signal_id: str | None = None
        self.max_age_ns: int | None = None
        self.layout_info: QuaternionLayout | None = None
        self.reading: AttitudeReading | None = None
        self._choices: tuple[str, ...] | None = None

        self.choice = QComboBox()
        self.choice.setAccessibleName("Orientation signal")
        self.choice.setMinimumContentsLength(18)
        self.max_age = QDoubleSpinBox()
        self.max_age.setAccessibleName("Stale after")
        self.max_age.setDecimals(1)
        self.max_age.setRange(0.0, MAX_AGE_LIMIT_MS)
        self.max_age.setSuffix(" ms")
        self.max_age.setSpecialValueText("Auto (3 sample periods)")
        self.max_age.setToolTip(
            "A reading older than this at the cursor is stale and is not drawn as a pose."
        )
        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.metadata = QLabel()
        self.metadata.setWordWrap(True)
        self.angles = QLabel()
        self.angles.setWordWrap(True)
        self.angles.setAccessibleName("Attitude angles")
        self.view = AttitudeView()

        controls = QGridLayout()
        controls.addWidget(QLabel("Signal"), 0, 0)
        controls.addWidget(self.choice, 0, 1)
        controls.addWidget(QLabel("Stale after"), 0, 2)
        controls.addWidget(self.max_age, 0, 3)
        controls.setColumnStretch(1, 1)
        layout = QVBoxLayout(self)
        layout.addLayout(controls)
        layout.addWidget(self.hint)
        layout.addWidget(self.metadata)
        layout.addWidget(self.angles)
        layout.addWidget(self.view, 1)
        self.setMinimumSize(360, 380)

        self.choice.currentIndexChanged.connect(self._choose)
        self.max_age.valueChanged.connect(self._choose_age)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(REFRESH_MS)
        self.timer.timeout.connect(self.refresh)
        for change in (context.snapshot_changed, context.cursor_changed):
            change.connect(self._schedule)
        context.signal_changed.connect(self._follow_selection)
        self._follow_selection()
        self.refresh()

    # Configuration -------------------------------------------------------

    def configuration(self) -> dict[str, object]:
        return {"signal_id": self.signal_id, "max_age_ns": self.max_age_ns}

    def restore_configuration(self, config: object) -> None:
        """Apply a saved configuration; a ValueError changes nothing."""

        if not isinstance(config, dict) or not set(config) <= {"signal_id", "max_age_ns"}:
            raise ValueError("Orientation configuration must map signal_id and max_age_ns.")
        signal_id = config.get("signal_id")
        if signal_id is not None and not (isinstance(signal_id, str) and signal_id.strip()):
            raise ValueError("signal_id must be a signal id or None.")
        max_age = config.get("max_age_ns")
        if max_age is not None and (
            isinstance(max_age, bool)
            or not isinstance(max_age, int)
            or not 0 < max_age <= MAX_AGE_LIMIT_MS * 1_000_000
        ):
            raise ValueError("max_age_ns must be None (auto) or a positive integer.")
        self.signal_id = signal_id
        self.max_age_ns = max_age
        self.max_age.blockSignals(True)
        self.max_age.setValue(0.0 if max_age is None else max_age / 1_000_000)
        self.max_age.blockSignals(False)
        self._choices = None
        self.refresh()

    # Choices -------------------------------------------------------------

    def _schedule(self) -> None:
        if not self.timer.isActive():
            self.timer.start()

    def _follow_selection(self) -> None:
        descriptor = self.context.descriptor(self.context.selected_id)
        # Follow the browser only to possible quaternions, so a 3-vector selection
        # does not hide the attitude; other vectors are still offered in the list.
        if descriptor is not None and descriptor.data_type == "vector" and descriptor.shape == (4,):
            if descriptor.identifier != self.signal_id:
                self.signal_id = descriptor.identifier
                self.refresh()

    def _choose(self, index: int) -> None:
        self.signal_id = self.choice.itemData(index)
        self.refresh()

    def _choose_age(self, value: float) -> None:
        self.max_age_ns = round(value * 1_000_000) if value > 0 else None
        self.refresh()

    def _sync_choices(self) -> None:
        vectors = [item for item in self.context.descriptors if item.data_type == "vector"]
        if self.signal_id is None:
            declared = []
            for item in vectors:
                try:
                    quaternion_layout(item)
                except OrientationError:
                    continue
                declared.append(item.identifier)
            if declared:
                self.signal_id = declared[0]
        entries = tuple(item.identifier for item in vectors)
        key = (*entries, self.signal_id or "")
        if key == self._choices:
            return
        self._choices = key
        self.choice.blockSignals(True)
        self.choice.clear()
        for item in vectors:
            self.choice.addItem(f"{item.display_name} - {item.identifier}", item.identifier)
        if self.signal_id is not None and self.choice.findData(self.signal_id) < 0:
            self.choice.addItem(f"{self.signal_id} (not in this session)", self.signal_id)
        self.choice.setCurrentIndex(
            self.choice.findData(self.signal_id) if self.signal_id is not None else -1
        )
        self.choice.blockSignals(False)

    # Display -------------------------------------------------------------

    def refresh(self) -> None:
        self.timer.stop()
        context = self.context
        self._sync_choices()
        self.layout_info = None
        self.reading = None
        if context.snapshot is None:
            self._show_empty("Load a session with a declared quaternion signal.")
            return
        descriptor = context.descriptor(self.signal_id)
        if descriptor is None:
            if self.signal_id is None:
                self._show_empty(
                    "No signal in this session declares a quaternion. The orientation "
                    "view needs a four-component vector signal with quaternion_order "
                    "and quaternion_convention metadata; no sensor is assumed."
                )
            else:
                self._show_empty(f"{self.signal_id} is not in this session.")
            return
        try:
            layout = quaternion_layout(descriptor)
        except OrientationError as error:
            self._show_empty(f"{error}. Orientation is shown only for declared quaternions.")
            return
        self.layout_info = layout
        max_age = self.max_age_ns or default_max_age_ns(descriptor)
        reading = attitude_at(
            context.snapshot, descriptor.identifier, context.cursor_ns, layout, max_age
        )
        self.reading = reading
        reference = layout.reference or "not declared"
        yaw = layout.yaw_reference or "not declared: treat yaw as relative, it may drift"
        self.hint.setText(f"{descriptor.display_name} ({descriptor.identifier})")
        self.metadata.setText(
            f"Body frame: {layout.frame} | Reference frame: {reference}\n"
            f"Components {layout.order}, {layout.convention} convention: the quaternion "
            f"rotates {layout.frame}-frame vectors into the reference frame. "
            "Angles are Z-Y-X (yaw, then pitch, then roll).\n"
            f"Yaw reference: {yaw}\n"
            f"Calibration: {_calibration_text(descriptor.calibration)} | "
            f"stale after {max_age / 1e6:.4g} ms"
        )
        self.angles.setText(self._angles_text(reading))
        style = (
            "valid"
            if reading.state is AttitudeState.VALID
            else "uncalibrated"
            if reading.state is AttitudeState.UNCALIBRATED
            else "none"
        )
        caption = "" if reading.is_current else reading.message
        self.view.show_attitude(reading.rotation, style, caption)

    def _angles_text(self, reading: AttitudeReading) -> str:
        parts = [reading.message]
        euler = reading.euler
        if euler is not None:
            roll, pitch, yaw = (round(angle, 1) + 0.0 for angle in euler)
            qualifier = "" if reading.is_current else " (NOT a valid current pose)"
            parts.append(
                f"Roll {roll:.1f} deg, pitch {pitch:.1f} deg, yaw {yaw:.1f} deg{qualifier}"
            )
        if reading.sample_ns is not None and reading.age_ns is not None:
            bounds = self.context.bounds
            at = (reading.sample_ns - bounds.start_ns) / 1e9 if bounds is not None else 0.0
            parts.append(
                f"Reading at {at:.3f} s, {reading.age_ns / 1e6:.1f} ms before the cursor"
                f"{f', quality {reading.quality}' if reading.quality else ''}"
            )
        return "\n".join(parts)

    def _show_empty(self, message: str) -> None:
        self.hint.setText(message)
        self.metadata.clear()
        self.angles.clear()
        self.view.show_attitude(None, "none", "No orientation to show.")


TOOL = AnalysisTool("orientation", "Orientation", OrientationTool)
