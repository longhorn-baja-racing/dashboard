"""Choose how an arbitrary CSV maps to signals, with a preview of what will import (#31).

The dialog only edits a :class:`~lbr_dashboard.core.csv_mapping.CsvMapping`.
The caller passes in the header, the first rows, a suggested mapping, the
points the suggestion wants confirmed, and a ``preview`` callable that applies
a mapping to the start of the file. The dialog never reads files itself.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.csv_mapping import (
    ISO8601,
    ColumnMapping,
    CsvMapping,
    MappedSession,
    SignalKind,
)

#: Rows of the file shown in the preview table.
PREVIEW_ROWS = 20
TIME_UNIT_LABELS = (
    ("Choose the time unit", ""),
    ("seconds (s)", "s"),
    ("milliseconds (ms)", "ms"),
    ("microseconds (us)", "us"),
    ("nanoseconds (ns)", "ns"),
    ("ISO 8601 date and time", ISO8601),
)
KIND_LABELS = (
    ("number", SignalKind.NUMBER),
    ("true/false", SignalKind.BOOLEAN),
    ("text", SignalKind.TEXT),
)
COLUMN, IMPORT, NAME, UNIT, KIND = range(5)

Preview = Callable[[CsvMapping], MappedSession]


class CsvMappingDialog(QDialog):
    """Pick the time column and unit, then name, type or exclude every other column."""

    def __init__(
        self,
        file_name: str,
        headers: Sequence[str],
        rows: Sequence[Sequence[str]],
        mapping: CsvMapping,
        notes: Sequence[str],
        preview: Preview,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Map CSV columns - {file_name}")
        self.setObjectName("csvMappingDialog")
        self.resize(960, 680)
        self.headers = tuple(headers)
        self.notes = tuple(notes)
        self._preview = preview
        self._encoding = mapping.encoding
        self._delimiter = mapping.delimiter
        self._time_column = -1
        self._blocking: list[str] = []
        self.preview_result: MappedSession | None = None

        intro = QLabel(
            f"Choose how {file_name} becomes signals. The time unit is never guessed: "
            "check it against the file. Nothing is imported until you press Import, "
            "and the file itself is never changed."
        )
        intro.setWordWrap(True)
        self.time_column = QComboBox()
        self.time_column.setAccessibleName("Time column")
        self.time_column.addItem("Choose the time column", -1)
        for index, header in enumerate(self.headers):
            self.time_column.addItem(f"{index + 1}: {header or '(no header)'}", index)
        self.time_unit = QComboBox()
        self.time_unit.setAccessibleName("Time unit")
        for label, unit in TIME_UNIT_LABELS:
            self.time_unit.addItem(label, unit)
        form = QFormLayout()
        form.addRow("Time column", self.time_column)
        form.addRow("Time unit", self.time_unit)

        self.columns = QTableWidget(len(self.headers), 5)
        self.columns.setAccessibleName("Column mapping")
        self.columns.setHorizontalHeaderLabels(("Column", "Import", "Signal name", "Unit", "Type"))
        self.columns.verticalHeader().setVisible(False)
        self.columns.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        mapped = {signal.column: signal for signal in mapping.signals}
        for index, header in enumerate(self.headers):
            signal = mapped.get(index)
            label = QTableWidgetItem(f"{index + 1}: {header or '(no header)'}")
            label.setFlags(label.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.columns.setItem(index, COLUMN, label)
            check = QTableWidgetItem()
            check.setFlags(
                (check.flags() | Qt.ItemFlag.ItemIsUserCheckable) & ~Qt.ItemFlag.ItemIsEditable
            )
            check.setCheckState(Qt.CheckState.Checked if signal else Qt.CheckState.Unchecked)
            self.columns.setItem(index, IMPORT, check)
            name = signal.name if signal else (header or f"column_{index + 1}")
            self.columns.setItem(index, NAME, QTableWidgetItem(name))
            self.columns.setItem(index, UNIT, QTableWidgetItem(signal.unit if signal else ""))
            kind = QComboBox()
            kind.setAccessibleName(f"Type of column {index + 1}")
            for text, value in KIND_LABELS:
                kind.addItem(text, value.value)
            kind.setCurrentIndex(
                kind.findData((signal.kind if signal else SignalKind.NUMBER).value)
            )
            kind.currentIndexChanged.connect(self._changed)
            self.columns.setCellWidget(index, KIND, kind)

        self.sample = QTableWidget(min(len(rows), PREVIEW_ROWS), len(self.headers))
        self.sample.setAccessibleName("First rows of the file")
        self.sample.setHorizontalHeaderLabels(
            [h or f"column_{i + 1}" for i, h in enumerate(headers)]
        )
        self.sample.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        for row, cells in enumerate(rows[:PREVIEW_ROWS]):
            for column, cell in enumerate(cells[: len(self.headers)]):
                self.sample.setItem(row, column, QTableWidgetItem(cell))

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        self.problems = QListWidget()
        self.problems.setAccessibleName("Mapping and row problems")
        self.problems.setWordWrap(True)

        splitter = QSplitter(Qt.Orientation.Vertical)
        for title, widget in (
            ("Columns: untick Import to leave a column out", self.columns),
            (f"First {PREVIEW_ROWS} data rows of the file", self.sample),
            ("Check before importing", self.problems),
        ):
            box = QWidget()
            box_layout = QVBoxLayout(box)
            box_layout.setContentsMargins(0, 0, 0, 0)
            box_layout.addWidget(QLabel(title))
            box_layout.addWidget(widget)
            splitter.addWidget(box)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.import_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.import_button.setText("Import")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addLayout(form)
        layout.addWidget(splitter, 1)
        layout.addWidget(self.summary)
        layout.addWidget(self.buttons)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(150)
        self._timer.timeout.connect(self.refresh)
        self.time_column.setCurrentIndex(self.time_column.findData(mapping.time_column))
        if self.time_column.currentIndex() < 0:
            self.time_column.setCurrentIndex(0)
        self.time_unit.setCurrentIndex(max(0, self.time_unit.findData(mapping.time_unit)))
        self._apply_time_column()
        self.time_column.currentIndexChanged.connect(self._time_column_changed)
        self.time_unit.currentIndexChanged.connect(self._changed)
        self.columns.itemChanged.connect(self._changed)
        self.refresh()

    # Editing -----------------------------------------------------------------------------

    def _item(self, column: int, part: int) -> QTableWidgetItem:
        item = self.columns.item(column, part)
        assert item is not None
        return item

    def kind_box(self, column: int) -> QComboBox:
        widget = self.columns.cellWidget(column, KIND)
        assert isinstance(widget, QComboBox)
        return widget

    def set_time(self, column: int, unit: str) -> None:
        """Choose the time column and unit, as the two combo boxes do."""

        self.time_column.setCurrentIndex(self.time_column.findData(column))
        self.time_unit.setCurrentIndex(self.time_unit.findData(unit))

    def set_column(
        self,
        column: int,
        *,
        included: bool | None = None,
        name: str | None = None,
        unit: str | None = None,
        kind: SignalKind | None = None,
    ) -> None:
        """Edit one row of the column table, as a user would."""

        if included is not None:
            item = self._item(column, IMPORT)
            item.setCheckState(Qt.CheckState.Checked if included else Qt.CheckState.Unchecked)
        if name is not None:
            self._item(column, NAME).setText(name)
        if unit is not None:
            self._item(column, UNIT).setText(unit)
        if kind is not None:
            box = self.kind_box(column)
            box.setCurrentIndex(box.findData(kind.value))

    def _time_column_changed(self) -> None:
        self._apply_time_column()
        self._changed()

    def _apply_time_column(self) -> None:
        """The time column cannot also be a signal; a released column can be imported again."""

        chosen = self.time_column.currentData()
        self.columns.blockSignals(True)
        for index in range(len(self.headers)):
            is_time = index == chosen
            check = self._item(index, IMPORT)
            if is_time:
                check.setCheckState(Qt.CheckState.Unchecked)
            elif index == self._time_column:
                check.setCheckState(Qt.CheckState.Checked)
            check.setText("time" if is_time else "")
            enabled = Qt.ItemFlag.ItemIsEnabled
            for part in (IMPORT, NAME, UNIT):
                item = self._item(index, part)
                item.setFlags(item.flags() & ~enabled if is_time else item.flags() | enabled)
            self.kind_box(index).setEnabled(not is_time)
        self.columns.blockSignals(False)
        self._time_column = chosen

    def _changed(self) -> None:
        self._timer.start()

    # Result ------------------------------------------------------------------------------

    def mapping(self) -> CsvMapping:
        """The mapping as currently shown."""

        signals = []
        time_column = int(self.time_column.currentData())
        for index in range(len(self.headers)):
            if index == time_column:
                continue
            if self._item(index, IMPORT).checkState() != Qt.CheckState.Checked:
                continue
            signals.append(
                ColumnMapping(
                    index,
                    self._item(index, NAME).text().strip(),
                    self._item(index, UNIT).text().strip(),
                    SignalKind(self.kind_box(index).currentData()),
                )
            )
        return CsvMapping(
            time_column,
            str(self.time_unit.currentData()),
            tuple(signals),
            self._encoding,
            self._delimiter,
        )

    def blocking_problems(self) -> list[str]:
        """What must change before Import is allowed."""

        mapping = self.mapping()
        found = mapping.problems(self.headers)
        for signal in mapping.signals:
            if any(character.isspace() for character in signal.unit):
                found.append(
                    f"The unit {signal.unit!r} of {signal.name!r} cannot contain spaces "
                    "(for example write degC or km/h)."
                )
        return found

    def refresh(self) -> None:
        """Check the mapping and apply it to the start of the file."""

        self._timer.stop()
        self.problems.clear()
        self._blocking = self.blocking_problems()
        self.preview_result = None
        for message in self._blocking:
            self.problems.addItem(f"Fix: {message}")
        for message in self.notes:
            self.problems.addItem(f"Check: {message}")
        if not self._blocking:
            try:
                self.preview_result = self._preview(self.mapping())
            except ValueError as error:
                self._blocking.append(str(error))
                self.problems.insertItem(0, f"Fix: {error}")
        result = self.preview_result
        if result is None:
            self.summary.setText("Fix the items marked Fix before importing.")
        else:
            for problem in result.problems:
                where = f", {problem.column!r}" if problem.column else ""
                self.problems.addItem(f"Line {problem.line}{where}: {problem.message}")
            if result.problems_not_listed:
                self.problems.addItem(f"... and {result.problems_not_listed} more problems.")
            for note in result.notes:
                self.problems.addItem(f"Note: {note}")
            scope = "the first rows" if result.truncated else "the whole file"
            self.summary.setText(f"Checked {scope}: {result.summary()}.")
        if not self.problems.count():
            self.problems.addItem("No problems found.")
        self.import_button.setEnabled(not self._blocking)

    def problem_texts(self) -> list[str]:
        return [self.problems.item(row).text() for row in range(self.problems.count())]

    def accept(self) -> None:
        if self._timer.isActive():
            self.refresh()
        if self._blocking:
            return
        super().accept()
