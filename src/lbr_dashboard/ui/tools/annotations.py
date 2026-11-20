"""Annotations tool: timestamped session markers listed, edited and drawn on the main plot.

Markers of a file-backed session are saved atomically to the sidecar file next
to the log (``<log name>.annotations.json``); the log itself is only read.
Sessions without a file, such as simulators, keep their markers in memory.
"""

from __future__ import annotations

from pathlib import Path

import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QGridLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core.annotations import (
    MAX_CATEGORY_LENGTH,
    MAX_TEXT_LENGTH,
    Annotation,
    AnnotationFileError,
    AnnotationSet,
    LogFingerprint,
    LogStatus,
    load_annotations,
    new_annotation,
    save_annotations,
    sidecar_path,
)
from ...core.statistics import format_seconds
from ..analysis import AnalysisContext, AnalysisTool

MARKER_COLOR = "#8e44ad"
DEFAULT_NOTE = "Marker"
ID_ROLE = Qt.ItemDataRole.UserRole

# Sessions without a log file still need a set; this identity is never saved.
_IN_MEMORY = LogFingerprint("in-memory session", 0, "0" * 64)


class AnnotationsTool(QWidget):
    """List, add, edit, remove and jump to the markers of the loaded session."""

    def __init__(self, context: AnalysisContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(360, 220)
        self.context = context
        self.annotations: AnnotationSet | None = None
        self.sidecar: Path | None = None
        self.markers: list[pg.InfiniteLine] = []
        self._marker_plot: pg.PlotItem | None = None
        self._session: object | None = None
        self._loaded = False
        self._reattach: LogFingerprint | None = None

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet("color: #c0392b;")
        self.error.hide()
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(("Time", "Label", "Note"))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        self.label_edit = QLineEdit()
        self.label_edit.setPlaceholderText("Label (optional)")
        self.label_edit.setMaxLength(MAX_CATEGORY_LENGTH)
        self.note_edit = QLineEdit()
        self.note_edit.setPlaceholderText(f"Note (empty: '{DEFAULT_NOTE}')")
        self.note_edit.setMaxLength(MAX_TEXT_LENGTH)
        self.add_cursor = QPushButton("Add at cursor")
        self.add_interval = QPushButton("Add for interval")
        self.add_interval.setToolTip(
            "Add one marker at the start and one at the end of the interval"
        )
        self.apply_edit = QPushButton("Save edit")
        self.move_to_cursor = QPushButton("Move to cursor")
        self.remove = QPushButton("Remove")
        self.jump = QPushButton("Go to marker")
        self.jump.setToolTip(
            "Move the replay cursor to the marker's exact time (double-click a row)"
        )

        form = QGridLayout()
        form.addWidget(QLabel("Label"), 0, 0)
        form.addWidget(self.label_edit, 0, 1, 1, 3)
        form.addWidget(QLabel("Note"), 1, 0)
        form.addWidget(self.note_edit, 1, 1, 1, 3)
        form.addWidget(self.add_cursor, 2, 0, 1, 2)
        form.addWidget(self.add_interval, 2, 2, 1, 2)
        form.addWidget(self.apply_edit, 3, 0)
        form.addWidget(self.move_to_cursor, 3, 1)
        form.addWidget(self.remove, 3, 2)
        form.addWidget(self.jump, 3, 3)
        layout = QVBoxLayout(self)
        layout.addWidget(self.status)
        layout.addWidget(self.error)
        layout.addWidget(self.table, 1)
        layout.addLayout(form)

        self.add_cursor.clicked.connect(self.add_at_cursor)
        self.add_interval.clicked.connect(self.add_for_interval)
        self.apply_edit.clicked.connect(self.edit_selected)
        self.move_to_cursor.clicked.connect(self.move_selected_to_cursor)
        self.remove.clicked.connect(self.remove_selected)
        self.jump.clicked.connect(self.jump_to_selected)
        self.table.cellDoubleClicked.connect(self._jump_to_row)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        context.snapshot_changed.connect(self._follow_session)
        context.cursor_changed.connect(self._update_actions)
        context.interval_changed.connect(self._update_actions)
        context.plot_changed.connect(self.draw_markers)
        self._follow_session()

    # Session and storage

    def _follow_session(self) -> None:
        if self._loaded and self.context.session is self._session:
            return
        self._loaded = True
        self._session = self.context.session
        self.load()

    def load(self) -> None:
        """Read the markers of the current session (sidecar file or a new in-memory set)."""

        self.annotations, self.sidecar, self._reattach = None, None, None
        self._show_error("")
        log_path = self.context.log_path
        if self.context.session is None:
            self.status.setText("Open a CSV file or start a simulator to add markers.")
        elif log_path is None:
            self.annotations = AnnotationSet(_IN_MEMORY)
            self.status.setText(
                "This source has no file, so markers are kept in memory only and are "
                "lost when another source is opened or the app closes."
            )
        else:
            self._load_sidecar(log_path)
        self._refresh()

    def _load_sidecar(self, log_path: Path) -> None:
        path = sidecar_path(log_path)
        try:
            loaded = load_annotations(path, log_path=log_path)
        except (AnnotationFileError, OSError) as error:
            reason = error.reason if isinstance(error, AnnotationFileError) else str(error)
            self.status.setText(f"Markers for {log_path.name} are unavailable.")
            self._show_error(
                f"Could not read {path}: {reason}. The file is left untouched; "
                "fix or move it to add markers."
            )
            return
        self.annotations, self.sidecar = loaded.annotations, path
        note = f"Markers are saved to {path.name} next to the log; the log is never changed."
        if loaded.status is LogStatus.LOG_CHANGED:
            self._reattach = loaded.current_fingerprint
            note += (
                f" These markers were saved for different content of {log_path.name}; "
                "the next edit attaches them to the current file."
            )
        elif loaded.status is LogStatus.LOG_MISSING:
            note += f" {log_path.name} was not found at its original place."
        self.status.setText(note)

    def _commit(self, changed: AnnotationSet) -> bool:
        """Save ``changed``; on failure keep the previous markers and file and show why."""

        if self._reattach is not None:
            changed = changed.with_fingerprint(self._reattach)
        if self.sidecar is not None:
            try:
                save_annotations(self.sidecar, changed)
            except AnnotationFileError as error:
                self._show_error(
                    f"Could not save markers: {error.reason}. "
                    f"The previous markers in {self.sidecar.name} were kept."
                )
                return False
        self._reattach = None
        self.annotations = changed
        self._show_error("")
        return True

    def _show_error(self, message: str) -> None:
        self.error.setText(message)
        self.error.setVisible(bool(message))

    # Editing

    def selected(self) -> Annotation | None:
        items = self.table.selectedItems()
        if not items or self.annotations is None:
            return None
        try:
            return self.annotations.get(items[0].data(ID_ROLE))
        except KeyError:
            return None

    def add_at_cursor(self) -> Annotation | None:
        cursor = self.context.cursor_ns
        if self.annotations is None or cursor is None:
            return None
        added = self._add([(cursor, self._note())])
        return added[0] if added else None

    def add_for_interval(self) -> tuple[Annotation, ...]:
        interval = self.context.interval
        if self.annotations is None or interval is None:
            return ()
        note = self._note()
        return self._add(
            [(interval.start_ns, f"{note} (start)"), (interval.end_ns, f"{note} (end)")]
        )

    def _add(self, entries: list[tuple[int, str]]) -> tuple[Annotation, ...]:
        assert self.annotations is not None
        label = self.label_edit.text().strip()
        try:
            added = tuple(
                new_annotation(time_ns, text, category=label) for time_ns, text in entries
            )
        except ValueError as error:
            self._show_error(f"Marker not added: {error}.")
            return ()
        changed = self.annotations
        for item in added:
            changed = changed.add(item)
        if not self._commit(changed):
            return ()
        self._refresh(select=added[0].annotation_id)
        return added

    def edit_selected(self) -> bool:
        current = self.selected()
        if current is None or self.annotations is None:
            return False
        return self._update(current, text=self._note(), category=self.label_edit.text().strip())

    def move_selected_to_cursor(self) -> bool:
        current, cursor = self.selected(), self.context.cursor_ns
        if current is None or cursor is None:
            return False
        return self._update(current, time_ns=cursor)

    def _update(self, current: Annotation, **fields) -> bool:
        assert self.annotations is not None
        try:
            changed = self.annotations.update(current.annotation_id, **fields)
        except ValueError as error:
            self._show_error(f"Marker not changed: {error}.")
            return False
        if not self._commit(changed):
            return False
        self._refresh(select=current.annotation_id)
        return True

    def remove_selected(self) -> bool:
        current = self.selected()
        if current is None or self.annotations is None:
            return False
        if not self._commit(self.annotations.remove(current.annotation_id)):
            return False
        self._refresh()
        return True

    def jump_to_selected(self) -> bool:
        """Move the replay cursor to the selected marker's exact session time."""

        current = self.selected()
        if current is None:
            return False
        self.context.request_seek(current.time_ns)
        if self.context.cursor_ns != current.time_ns:
            self._show_error(
                "The replay cursor cannot move there now (the session is loading, live, "
                "or the marker lies outside the recorded time)."
            )
            return False
        self._show_error("")
        return True

    def _jump_to_row(self, row: int, _column: int) -> None:
        self.table.selectRow(row)
        self.jump_to_selected()

    def _note(self) -> str:
        return self.note_edit.text().strip() or DEFAULT_NOTE

    # Presentation

    def _refresh(self, select: str | None = None) -> None:
        items = self.annotations.annotations if self.annotations is not None else ()
        keep = select or (current.annotation_id if (current := self.selected()) else None)
        self.table.blockSignals(True)
        self.table.clearContents()
        self.table.setRowCount(len(items))
        selected_row = -1
        for row, item in enumerate(items):
            cells = (format_seconds(item.time_ns), item.category, item.text)
            for column, text in enumerate(cells):
                cell = QTableWidgetItem(text)
                cell.setData(ID_ROLE, item.annotation_id)
                cell.setToolTip(f"Session time {item.time_ns} ns")
                self.table.setItem(row, column, cell)
            if item.annotation_id == keep:
                selected_row = row
        if selected_row >= 0:
            self.table.selectRow(selected_row)
        else:
            self.table.clearSelection()
        self.table.blockSignals(False)
        self._selection_changed()
        self.draw_markers()

    def _selection_changed(self) -> None:
        current = self.selected()
        if current is not None:
            self.label_edit.setText(current.category)
            self.note_edit.setText(current.text)
        self._update_actions()

    def _update_actions(self) -> None:
        editable = self.annotations is not None
        has_cursor = self.context.cursor_ns is not None
        chosen = self.selected() is not None
        self.add_cursor.setEnabled(editable and has_cursor)
        self.add_interval.setEnabled(editable and self.context.interval is not None)
        self.apply_edit.setEnabled(chosen)
        self.move_to_cursor.setEnabled(chosen and has_cursor)
        self.remove.setEnabled(chosen)
        self.jump.setEnabled(chosen)

    def draw_markers(self) -> None:
        """Draw every marker on the main plot again, e.g. after the plot was cleared."""

        if self._marker_plot is not None:
            for line in self.markers:
                self._marker_plot.removeItem(line)  # No-op once the plot was cleared.
        self.markers, self._marker_plot = [], None
        plot, origin = self.context.plot, self.context.plot_origin_ns
        item = plot.getPlotItem() if plot is not None else None
        if item is None or origin is None or self.annotations is None:
            return
        self._marker_plot = item
        # Plain lines without text items: pyqtgraph text labels fail to paint while
        # the view range is degenerate. The note is in the tooltip and the table.
        pen = pg.mkPen(MARKER_COLOR, width=2, style=Qt.PenStyle.DashLine)
        for annotation in self.annotations.annotations:
            line = pg.InfiniteLine(
                pos=(annotation.time_ns - origin) / 1e9, angle=90, movable=False, pen=pen
            )
            title = f"[{annotation.category}] " if annotation.category else ""
            line.setToolTip(f"{format_seconds(annotation.time_ns)} {title}{annotation.text}")
            item.addItem(line, ignoreBounds=True)
            self.markers.append(line)


TOOL = AnalysisTool("annotations", "Annotations", AnnotationsTool)
