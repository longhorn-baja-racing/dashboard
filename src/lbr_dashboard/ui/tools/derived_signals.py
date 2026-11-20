"""Derived signals tool: define, preview and apply signals computed from other signals.

Applied definitions live in ``AnalysisContext.derived``. The source controller
evaluates them into every snapshot it shows, so a derived signal appears in the
signal browser under ``derived`` and can be plotted and analysed like a
recorded one. Recorded signals are never changed.
"""

from __future__ import annotations

import re

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core.derived import FUNCTIONS, DerivedSignalError, parse_expression
from ...core.derived_session import DerivedPreview, missing_text, provenance
from ..analysis import AnalysisContext, AnalysisTool, is_numeric, refresh_soon

PREVIEW_ROWS = 20
NANOSECONDS = 1_000_000_000


def default_id(name: str) -> str:
    """Turn a display name into a derived signal id such as ``derived.flow_rate_x2``."""

    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    if not slug:
        slug = "signal"
    if not slug[0].isalpha():
        slug = "s_" + slug
    return f"derived.{slug}"


def _table(headers: tuple[str, ...]) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(list(headers))
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.verticalHeader().setVisible(False)
    header = table.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    header.setStretchLastSection(True)
    return table


class DerivedSignalsTool(QWidget):
    """Edit one candidate definition with a live preview, and manage applied ones."""

    def __init__(self, context: AnalysisContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self.editing: str | None = None
        self.preview: DerivedPreview | None = None
        self._id_auto = True
        self._inputs: tuple[str, ...] = ()
        self._choices: tuple[tuple[str, str], ...] = ()
        self.setMinimumSize(420, 320)

        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.name = QLineEdit()
        self.name.setPlaceholderText("Display name, e.g. Flow rate x2")
        self.output_id = QLineEdit()
        self.output_id.setPlaceholderText("derived.flow_rate_x2")
        self.expression = QLineEdit()
        self.expression.setPlaceholderText('sig("signal.id") * 2')
        self.expression.setAccessibleName("Derived signal expression")
        self.insert_combo = QComboBox()
        self.insert_combo.setAccessibleName("Signal to insert")
        self.insert_button = QPushButton("Insert signal")
        self.unit = QLineEdit()
        self.unit.setPlaceholderText("blank = use the unit the expression gives")
        self.reference = QComboBox()
        self.reference.setAccessibleName("Input that supplies the timestamps")
        self.hold = QDoubleSpinBox()
        self.hold.setRange(0.0, 3_600_000.0)
        self.hold.setDecimals(3)
        self.hold.setSuffix(" ms")
        self.hold.setToolTip(
            "Other inputs use their latest sample at or before each reference time, "
            "if it is at most this old (0 = exact timestamp match)."
        )
        insert_row = QHBoxLayout()
        insert_row.addWidget(self.insert_combo, 1)
        insert_row.addWidget(self.insert_button)
        form = QFormLayout()
        form.addRow("Name", self.name)
        form.addRow("ID", self.output_id)
        form.addRow("Expression", self.expression)
        form.addRow("", insert_row)
        form.addRow("Unit", self.unit)
        form.addRow("Timestamps from", self.reference)
        form.addRow("Hold other inputs", self.hold)

        self.apply_button = QPushButton("Apply")
        self.new_button = QPushButton("New")
        actions = QHBoxLayout()
        actions.addWidget(self.apply_button)
        actions.addWidget(self.new_button)
        actions.addStretch(1)
        self.preview_label = QLabel()
        self.preview_label.setWordWrap(True)
        self.preview_table = _table(("Time (s)", "Value", "Missing reason"))
        self.applied_label = QLabel("Applied derived signals")
        self.applied = _table(("Name", "ID", "Expression", "Inputs and alignment", "Status"))
        self.edit_button = QPushButton("Edit")
        self.remove_button = QPushButton("Remove")
        applied_actions = QHBoxLayout()
        applied_actions.addWidget(self.edit_button)
        applied_actions.addWidget(self.remove_button)
        applied_actions.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addWidget(self.hint)
        layout.addLayout(form)
        layout.addLayout(actions)
        layout.addWidget(self.preview_label)
        layout.addWidget(self.preview_table, 1)
        layout.addWidget(self.applied_label)
        layout.addWidget(self.applied, 1)
        layout.addLayout(applied_actions)

        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.refresh)
        self.name.textEdited.connect(self._name_edited)
        self.output_id.textEdited.connect(self._id_edited)
        for field in (self.expression, self.unit):
            field.textChanged.connect(self.timer.start)
        self.reference.currentIndexChanged.connect(refresh_soon(self.timer))
        self.hold.valueChanged.connect(refresh_soon(self.timer))
        self.insert_button.clicked.connect(self.insert_signal)
        self.apply_button.clicked.connect(self.apply)
        self.new_button.clicked.connect(self.new)
        self.edit_button.clicked.connect(self.edit_selected)
        self.remove_button.clicked.connect(self.remove_selected)
        self.applied.itemSelectionChanged.connect(self._update_buttons)
        context.snapshot_changed.connect(refresh_soon(self.timer))
        context.derived_changed.connect(refresh_soon(self.timer))
        self.refresh()

    # Form -------------------------------------------------------------------

    def _name_edited(self, text: str) -> None:
        if self._id_auto:
            self.output_id.setText(default_id(text) if text.strip() else "")
        self.timer.start()

    def _id_edited(self, _text: str) -> None:
        self._id_auto = not self.output_id.text().strip()
        self.timer.start()

    def insert_signal(self) -> None:
        signal_id = self.insert_combo.currentData()
        if signal_id:
            self.expression.insert(f'sig("{signal_id}")')
            self.expression.setFocus()

    def new(self) -> None:
        """Start a new definition with an empty form."""

        self.editing = None
        self._id_auto = True
        for field in (self.name, self.output_id, self.expression, self.unit):
            field.clear()
        self.hold.setValue(0.0)
        self.refresh()

    def load(self, output_id: str) -> None:
        """Put an applied definition into the form; Apply then replaces it."""

        definition = self.context.derived.definition(output_id)
        if definition is None:
            return
        self.editing = output_id
        self._id_auto = False
        self.name.setText(definition.name)
        self.output_id.setText(definition.output_id)
        self.expression.setText(definition.expression)
        self.unit.setText(definition.unit or "")
        self.hold.setValue(definition.max_age_ns / 1_000_000)
        self._set_inputs(definition.inputs, definition.reference)
        self.refresh()

    def _set_inputs(self, inputs: tuple[str, ...], selected: str | None) -> None:
        if inputs == self._inputs and selected is None:
            return
        current = selected or self.reference.currentData()
        self._inputs = inputs
        self.reference.blockSignals(True)
        self.reference.clear()
        for signal_id in inputs:
            self.reference.addItem(signal_id, signal_id)
        index = self.reference.findData(current)
        self.reference.setCurrentIndex(index if index >= 0 else 0)
        self.reference.blockSignals(False)

    def _hold_ns(self) -> int:
        return round(self.hold.value() * 1_000_000)

    def compute_preview(self) -> DerivedPreview:
        try:
            inputs = parse_expression(self.expression.text()).signal_ids
        except DerivedSignalError:
            inputs = ()
        if inputs:
            self._set_inputs(inputs, None)
        name = self.name.text().strip()
        output_id = self.output_id.text().strip()
        if not name:
            name = output_id
        self.preview = self.context.derived.preview(
            self.context.snapshot,
            output_id=output_id,
            name=name,
            expression=self.expression.text(),
            unit=self.unit.text().strip() or None,
            reference_id=self.reference.currentData() if inputs else None,
            max_age_ns=self._hold_ns(),
            replacing=self.editing,
        )
        return self.preview

    # Refresh ----------------------------------------------------------------

    def refresh(self) -> None:
        self.timer.stop()
        self._refresh_choices()
        self._refresh_applied()
        if self.context.snapshot is None:
            self.hint.setText(
                "Load a session to preview derived signals. Applied definitions are kept "
                "and evaluated for the next session."
            )
        else:
            functions = ", ".join(FUNCTIONS)
            self.hint.setText(
                'Combine numeric signals with sig("<id>"), numbers, + - * / and '
                f"{functions}. Applied signals appear in the signal browser under derived."
            )
        if not self.expression.text().strip():
            self.preview = None
            self.preview_label.setText("Enter an expression to preview the derived signal.")
            self.preview_table.setRowCount(0)
            self._update_buttons()
            return
        self._show_preview(self.compute_preview())
        self._update_buttons()

    def _refresh_choices(self) -> None:
        choices = tuple(
            (d.identifier, f"{d.display_name} - {d.identifier}")
            for d in self.context.descriptors
            if is_numeric(d) and d.availability.available
        )
        if choices == self._choices:
            return
        self._choices = choices
        current = self.insert_combo.currentData()
        self.insert_combo.clear()
        for identifier, label in choices:
            self.insert_combo.addItem(label, identifier)
        index = self.insert_combo.findData(current)
        if index >= 0:
            self.insert_combo.setCurrentIndex(index)

    def _show_preview(self, preview: DerivedPreview) -> None:
        table = self.preview_table
        if not preview.ok:
            self.preview_label.setText(f"Cannot apply: {preview.error}")
            table.setRowCount(0)
            return
        series = preview.series
        assert preview.definition is not None
        verb = "replaces" if self.editing else "adds"
        lines = [f"Preview: Apply {verb} {preview.definition.output_id}."]
        if series is not None:
            values = [value for value in series.values if value is not None]
            span = f", min {min(values):g}, max {max(values):g}" if values else ""
            scope = (
                f" (first {len(series)} of {preview.total} reference samples)"
                if preview.truncated
                else ""
            )
            lines.append(
                f"{len(series)} samples{scope}, {series.missing_count} missing, "
                f"unit {series.unit}{span}."
            )
            lines.append(
                f"Timestamps of {series.reference_id}; other inputs held up to "
                f"{series.max_age_ns / 1_000_000:g} ms."
            )
        lines.extend(f"Input: {line}" for line in preview.inputs)
        lines.extend(f"Note: {note}" for note in preview.notes)
        self.preview_label.setText("\n".join(lines))
        if series is None:
            table.setRowCount(0)
            return
        count = min(len(series), PREVIEW_ROWS)
        origin = series.times_ns[0] if len(series) else 0
        table.setRowCount(count)
        for row in range(count):
            value = series.values[row]
            cells = (
                f"{(series.times_ns[row] - origin) / NANOSECONDS:.3f}",
                "" if value is None else f"{value:g}",
                missing_text(series, row),
            )
            for column, text in enumerate(cells):
                table.setItem(row, column, QTableWidgetItem(text))

    def _refresh_applied(self) -> None:
        derived = self.context.derived
        table = self.applied
        selected = self.selected_id()
        table.setRowCount(len(derived.definitions))
        for row, definition in enumerate(derived.definitions):
            status = derived.statuses.get(definition.output_id)
            if status is None:
                state = "waiting for a session"
            else:
                state = status.message
                if status.notes:
                    state += " (" + "; ".join(status.notes) + ")"
            alignment = (
                f"{', '.join(definition.inputs)}; timestamps of {definition.reference}; "
                f"hold {definition.max_age_ns / 1_000_000:g} ms"
            )
            cells = (
                definition.name,
                definition.output_id,
                definition.expression,
                alignment,
                state,
            )
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setToolTip(provenance(definition))
                table.setItem(row, column, item)
            if definition.output_id == selected:
                table.selectRow(row)
        self.applied_label.setText(
            f"Applied derived signals ({len(derived.definitions)})"
            if derived.definitions
            else "No derived signals applied yet."
        )

    def _update_buttons(self) -> None:
        self.apply_button.setEnabled(self.preview is not None and self.preview.ok)
        has_row = self.selected_id() is not None
        self.edit_button.setEnabled(has_row)
        self.remove_button.setEnabled(has_row)

    # Applied definitions ----------------------------------------------------

    def selected_id(self) -> str | None:
        rows = self.applied.selectionModel().selectedRows()
        if not rows:
            return None
        item = self.applied.item(rows[0].row(), 1)
        return item.text() if item is not None else None

    def apply(self) -> bool:
        """Apply the form's definition after checking it again; return success."""

        preview = self.compute_preview()
        if not preview.ok or preview.definition is None:
            self._show_preview(preview)
            self._update_buttons()
            return False
        try:
            self.context.derived.apply(preview.definition, replacing=self.editing)
        except DerivedSignalError as error:
            self.preview_label.setText(f"Cannot apply: {error}")
            return False
        self.editing = preview.definition.output_id
        self._id_auto = False
        self.refresh()
        return True

    def edit_selected(self) -> None:
        output_id = self.selected_id()
        if output_id is not None:
            self.load(output_id)

    def remove_selected(self) -> None:
        output_id = self.selected_id()
        if output_id is None:
            return
        if self.editing == output_id:
            self.editing = None
        self.context.derived.remove(output_id)
        self.refresh()


TOOL = AnalysisTool("derived_signals", "Derived signals", DerivedSignalsTool)
