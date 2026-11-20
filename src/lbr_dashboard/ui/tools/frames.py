"""Coordinate frames tool: define, preview and apply rotations between named frames."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.frames import (
    ROTATION_FORMATS,
    FrameTransform,
    RotationError,
    TransformError,
    declared_frame,
    is_transformed,
    parse_rotation,
    plan,
    rotate_vector,
)
from ..analysis import AnalysisContext, AnalysisTool, refresh_soon

_PLACEHOLDERS = {
    "euler": "0, 0, 90",
    "quaternion": "0.7071068, 0, 0, 0.7071068",
    "matrix": "0, -1, 0; 1, 0, 0; 0, 0, 1",
}


def _vector_text(value: object, unit: str | None) -> str:
    if not isinstance(value, (tuple, list)):
        return "missing"
    text = ", ".join(f"{float(item):.4g}" for item in value)
    return f"({text}){f' {unit}' if unit else ''}"


class FramesTool(QWidget):
    """Edit the session's frame transforms; results appear as new signals in the browser.

    A transform rotates every three-component vector signal whose declared frame
    equals its source frame. Signals without a declared frame are listed with
    the reason they are left alone. Raw signals are never changed.
    """

    def __init__(self, context: AnalysisContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self.draft: FrameTransform | None = None
        self.draft_error = ""

        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.name = QLineEdit()
        self.name.setPlaceholderText("vehicle")
        self.name.setAccessibleName("Transform name")
        self.source = QComboBox()
        self.source.setEditable(True)
        self.source.setAccessibleName("Source frame")
        self.source.setToolTip("Frame declared by the vector signals to rotate.")
        self.target = QLineEdit()
        self.target.setPlaceholderText("vehicle")
        self.target.setAccessibleName("Target frame")
        self.form = QComboBox()
        self.form.setAccessibleName("Rotation format")
        for key, label in ROTATION_FORMATS.items():
            self.form.addItem(label, key)
        self.rotation = QLineEdit()
        self.rotation.setAccessibleName("Rotation")
        self.rotation.setToolTip(
            "The rotation R with v_target = R v_source: the columns of R are the source "
            "frame's axes written in target-frame coordinates."
        )
        self.apply_button = QPushButton("Apply transform")
        self.remove_button = QPushButton("Remove selected")
        self.applied = QListWidget()
        self.applied.setAccessibleName("Applied transforms")
        self.applied.setMaximumHeight(90)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setAccessibleName("Transform preview")

        fields = QFormLayout()
        fields.addRow("Name", self.name)
        fields.addRow("Source frame", self.source)
        fields.addRow("Target frame", self.target)
        fields.addRow("Rotation format", self.form)
        fields.addRow("Rotation", self.rotation)
        buttons = QHBoxLayout()
        buttons.addWidget(self.apply_button)
        buttons.addWidget(self.remove_button)
        buttons.addStretch(1)
        layout = QVBoxLayout(self)
        layout.addWidget(self.hint)
        layout.addLayout(fields)
        layout.addLayout(buttons)
        layout.addWidget(QLabel("Applied transforms"))
        layout.addWidget(self.applied)
        layout.addWidget(self.preview, 1)
        self.setMinimumSize(360, 420)

        for edit in (self.name, self.target, self.rotation):
            edit.textChanged.connect(self._edited)
        self.source.editTextChanged.connect(self._edited)
        self.form.currentIndexChanged.connect(self._form_changed)
        self.apply_button.clicked.connect(self.apply)
        self.remove_button.clicked.connect(self.remove_selected)
        self.applied.currentItemChanged.connect(self._load_applied)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.refresh)
        for change in (context.snapshot_changed, context.cursor_changed):
            change.connect(refresh_soon(self.timer))
        context.frames_changed.connect(self._frames_changed)
        self._form_changed()
        self._list_applied()

    # Configuration -------------------------------------------------------

    def configuration(self) -> dict[str, object]:
        """Return the applied transforms (see FrameTransformSet.configuration)."""

        return self.context.frames.configuration()

    def restore_configuration(self, config: object) -> None:
        """Replace the applied transforms; a ValueError leaves them unchanged."""

        self.context.frames.restore(config)

    # Editing -------------------------------------------------------------

    def set_draft(self, name: str, source: str, target: str, form: str, rotation: str) -> None:
        """Fill the editor, e.g. from a test or a saved transform."""

        self.name.setText(name)
        self.source.setEditText(source)
        self.target.setText(target)
        self.form.setCurrentIndex(self.form.findData(form))
        self.rotation.setText(rotation)
        self._edited()

    def _form_changed(self) -> None:
        self.rotation.setPlaceholderText(_PLACEHOLDERS.get(self.form.currentData(), ""))
        self._edited()

    def _edited(self) -> None:
        self.draft, self.draft_error = None, ""
        if not any(
            (self.name.text().strip(), self.target.text().strip(), self.rotation.text().strip())
        ):
            self.draft_error = "Enter a name, the target frame and a rotation."
        else:
            try:
                rotation = parse_rotation(self.form.currentData(), self.rotation.text())
                self.draft = FrameTransform(
                    self.name.text().strip(),
                    self.source.currentText(),
                    self.target.text(),
                    rotation,
                )
            except (RotationError, TransformError) as error:
                self.draft_error = f"Not a valid transform: {error}."
        applied = {item.name for item in self.context.frames.transforms}
        self.apply_button.setEnabled(self.draft is not None)
        self.apply_button.setText(
            "Replace transform"
            if self.draft is not None and self.draft.name in applied
            else "Apply transform"
        )
        self.refresh()

    def apply(self) -> None:
        if self.draft is not None:
            self.context.frames.apply(self.draft)

    def remove_selected(self) -> None:
        item = self.applied.currentItem()
        if item is not None:
            self.context.frames.remove(item.data(Qt.ItemDataRole.UserRole))

    def _frames_changed(self) -> None:
        self._list_applied()
        self._edited()

    def _list_applied(self) -> None:
        self.applied.blockSignals(True)
        self.applied.clear()
        for transform in self.context.frames.transforms:
            roll, pitch, yaw = transform.rotation.euler()
            item = QListWidgetItem(
                f"{transform.name}: {transform.source_frame} -> {transform.target_frame} "
                f"(roll {roll:.4g}, pitch {pitch:.4g}, yaw {yaw:.4g} deg)"
            )
            item.setData(Qt.ItemDataRole.UserRole, transform.name)
            self.applied.addItem(item)
        self.applied.blockSignals(False)
        self.remove_button.setEnabled(self.applied.count() > 0)

    def _load_applied(self, item: QListWidgetItem | None) -> None:
        if item is None:
            return
        transform = next(
            (
                each
                for each in self.context.frames.transforms
                if each.name == item.data(Qt.ItemDataRole.UserRole)
            ),
            None,
        )
        if transform is not None:
            w, x, y, z = transform.rotation.canonical()
            self.set_draft(
                transform.name,
                transform.source_frame,
                transform.target_frame,
                "quaternion",
                f"{w:.9g}, {x:.9g}, {y:.9g}, {z:.9g}",
            )

    # Preview -------------------------------------------------------------

    def refresh(self) -> None:
        self.timer.stop()
        context = self.context
        descriptors = context.descriptors
        raw = [item for item in descriptors if not is_transformed(item)]
        frames = sorted(
            {
                frame
                for item in raw
                if item.data_type == "vector" and (frame := declared_frame(item)) is not None
            }
        )
        current = self.source.currentText()
        if [self.source.itemText(index) for index in range(self.source.count())] != frames:
            self.source.blockSignals(True)
            self.source.clear()
            self.source.addItems(frames)
            self.source.setEditText(current)
            self.source.blockSignals(False)
        applied = len(context.frames.transforms)
        outputs = sum(1 for item in descriptors if is_transformed(item))
        if context.snapshot is None:
            self.hint.setText(
                "Load a session to rotate vector signals with a declared frame. "
                f"{applied} transform(s) applied."
            )
        else:
            self.hint.setText(
                f"{applied} transform(s) applied, {outputs} transformed signal(s) in the "
                "signal browser under 'transformed'. Only three-component vectors with a "
                "declared frame are rotated; raw signals stay unchanged."
            )
        self.preview.setPlainText("\n".join(self._preview_lines(raw)))

    def _preview_lines(self, raw) -> list[str]:
        draft = self.draft
        if draft is None:
            return [self.draft_error]
        rotation = draft.rotation
        matrix = rotation.matrix
        lines = [
            f"Transform '{draft.name}': frame '{draft.source_frame}' -> '{draft.target_frame}'",
            f"Rotation angle {rotation.angle_degrees():.4g} deg; v_target = R v_source with R =",
            *("  [" + ", ".join(f"{value: .4f}" for value in row) + "]" for row in matrix),
        ]
        items = plan(raw, (draft,))
        snapshot = self.context.snapshot
        cursor = self.context.cursor_ns
        applies = [item for item in items if item.applies]
        refused = [item for item in items if not item.applies]
        lines.append("")
        if applies:
            lines.append("Rotates:")
        else:
            lines.append(f"No vector signal in this session declares frame '{draft.source_frame}'.")
        for item in applies:
            descriptor = item.descriptor
            lines.append(f"  {descriptor.identifier} -> {item.output_id}")
            if snapshot is not None and cursor is not None:
                record = snapshot.at_or_before(descriptor.identifier, cursor)
                if record is not None and record.sample.available:
                    value = record.sample.value
                    unit = descriptor.canonical_unit
                    lines.append(
                        f"    at cursor: {_vector_text(value, unit)} -> "
                        f"{_vector_text(rotate_vector(rotation, value), unit)}"
                    )
                elif record is not None:
                    lines.append("    at cursor: missing reading stays missing")
        if refused:
            lines.append("Not transformed:")
            lines.extend(f"  {item.descriptor.identifier} {item.reason}" for item in refused)
        others = sum(1 for item in raw if item.data_type != "vector")
        if others:
            lines.append(f"{others} other signal(s) are not vectors and are never transformed.")
        return lines


TOOL = AnalysisTool("frames", "Coordinate frames", FramesTool)
