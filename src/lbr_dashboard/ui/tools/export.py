"""Export tool: raw records of chosen signals to CSV, and the main plot to PNG."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from pathlib import Path

import pyqtgraph as pg
from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.export import (
    ExportError,
    ExportResult,
    export_csv,
    metadata_path_for,
    snapshot_signals,
)
from ...core.lifecycle import CancellationRequested, CancellationToken
from ...core.session import SessionRecord
from ...core.statistics import format_seconds
from ..analysis import AnalysisContext, AnalysisTool, refresh_soon
from ..plot_export import export_plot_image

ID_ROLE = Qt.ItemDataRole.UserRole
PROGRESS_STEPS = 1000


def _time(record: SessionRecord) -> int:
    return record.sample.timestamp_ns


class ExportTool(QWidget):
    """Choose signals and export their raw records inside the shared interval."""

    def __init__(self, context: AnalysisContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self.setMinimumSize(320, 240)
        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.signals = QListWidget()
        self.signals.setAccessibleName("Signals to export")
        self.all_button = QPushButton("Check all")
        self.none_button = QPushButton("Check none")
        self.scope = QLabel()
        self.scope.setWordWrap(True)
        self.export_button = QPushButton("Export CSV...")
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        checks = QHBoxLayout()
        checks.addWidget(self.all_button)
        checks.addWidget(self.none_button)
        checks.addStretch(1)
        layout = QVBoxLayout(self)
        layout.addWidget(self.hint)
        layout.addWidget(self.signals, 1)
        layout.addLayout(checks)
        layout.addWidget(self.scope)
        layout.addWidget(self.export_button)
        layout.addWidget(self.status)
        self._ids: tuple[str, ...] = ()
        self._default_id: str | None = None
        self._directory = ""
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.refresh)
        for change in (context.snapshot_changed, context.interval_changed):
            change.connect(refresh_soon(self.timer))
        context.signal_changed.connect(self._follow_selected)
        self.signals.itemChanged.connect(lambda _item: self._update_scope())
        self.all_button.clicked.connect(lambda: self._check_all(True))
        self.none_button.clicked.connect(lambda: self._check_all(False))
        self.export_button.clicked.connect(self.export_selected)
        self.refresh()

    def checked_ids(self) -> list[str]:
        """Return the checked signal ids in list order."""

        return [
            str(item.data(ID_ROLE))
            for item in self._items()
            if item.checkState() == Qt.CheckState.Checked
        ]

    def set_checked(self, signal_ids: set[str] | list[str]) -> None:
        wanted = set(signal_ids)
        self.signals.blockSignals(True)
        try:
            for item in self._items():
                checked = item.data(ID_ROLE) in wanted
                item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        finally:
            self.signals.blockSignals(False)
        self._update_scope()

    def refresh(self) -> None:
        self.timer.stop()
        descriptors = self.context.descriptors
        ids = tuple(item.identifier for item in descriptors)
        if ids != self._ids:
            kept = set(self.checked_ids())
            self._ids = ids
            self.signals.blockSignals(True)
            try:
                self.signals.clear()
                for descriptor in descriptors:
                    unit = descriptor.canonical_unit or "no unit"
                    item = QListWidgetItem(
                        f"{descriptor.display_name} ({descriptor.identifier}) - "
                        f"{descriptor.data_type}, {unit}"
                    )
                    item.setData(ID_ROLE, descriptor.identifier)
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    item.setCheckState(Qt.CheckState.Unchecked)
                    self.signals.addItem(item)
            finally:
                self.signals.blockSignals(False)
            kept &= set(ids)
            if not kept and self.context.selected_id in ids:
                self._default_id = self.context.selected_id
                kept = {self.context.selected_id}
            self.set_checked(kept)
        if not ids:
            self.hint.setText("Load a session (Source: Connect) to export its raw telemetry.")
        else:
            self.hint.setText(
                "Check the signals to export. Every signal keeps its own raw timestamps; "
                "nothing is aligned, resampled or filled in."
            )
        self._update_scope()

    def export_selected(self) -> ExportResult | None:
        """Ask for a file name and export the checked signals; return the result."""

        self.refresh()
        snapshot = self.context.snapshot
        ids = self.checked_ids()
        if snapshot is None or not ids:
            self._set_status("Check at least one signal of a loaded session to export.")
            return None
        interval = self.context.interval
        start_ns = interval.start_ns if interval is not None else None
        end_ns = interval.end_ns if interval is not None else None
        signals = snapshot_signals(snapshot, ids, start_ns=start_ns, end_ns=end_ns)
        suggested = str(Path(self._directory) / "telemetry-export.csv")
        path_text, _ = QFileDialog.getSaveFileName(
            self,
            "Export raw telemetry",
            suggested,
            "CSV files (*.csv)",
            "",
            QFileDialog.Option.DontConfirmOverwrite,
        )
        if not path_text:
            return None
        path = Path(path_text)
        if path.suffix.lower() != ".csv":
            path = path.with_name(path.name + ".csv")
        self._directory = str(path.parent)
        overwrite = False
        existing = [item for item in (path, metadata_path_for(path)) if item.exists()]
        if existing:
            names = "\n".join(item.name for item in existing)
            answer = QMessageBox.question(
                self,
                "Replace existing export?",
                f"These files already exist in {path.parent}:\n{names}\n\nReplace them?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                self._set_status("Export not started; the existing files were kept.")
                return None
            overwrite = True

        total = sum(len(signal.times_ns) for signal in signals)
        dialog = QProgressDialog(f"Exporting {total} records...", "Cancel", 0, PROGRESS_STEPS, self)
        dialog.setWindowTitle("Exporting raw telemetry")
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.setMinimumDuration(400)
        token = CancellationToken()

        def progress(done: int, count: int) -> None:
            dialog.setValue(PROGRESS_STEPS * done // count if count else PROGRESS_STEPS)
            if dialog.wasCanceled():
                token.cancel()

        sources = sorted(
            {item.source_id for item in snapshot.schema.signals if item.identifier in ids}
        )
        self.export_button.setEnabled(False)
        try:
            result = export_csv(
                path,
                signals,
                start_ns=start_ns,
                end_ns=end_ns,
                source=", ".join(sources) or None,
                overwrite=overwrite,
                cancel=token,
                progress=progress,
            )
        except CancellationRequested:
            self._set_status("Export cancelled; no file was written and existing files are kept.")
            return None
        except (ExportError, ValueError) as error:
            message = f"{error}\n\nNo export file was written; existing files are unchanged."
            self._set_status(f"Export failed: {message}")
            QMessageBox.warning(self, "Export failed", message)
            return None
        finally:
            dialog.close()
            dialog.deleteLater()
            self.export_button.setEnabled(True)
        self._set_status(
            f"Exported {result.row_count} records to {result.csv_path}; "
            f"units, frame and interval are described in {result.metadata_path.name}."
        )
        return result

    def _items(self) -> list[QListWidgetItem]:
        items = (self.signals.item(row) for row in range(self.signals.count()))
        return [item for item in items if item is not None]

    def _check_all(self, checked: bool) -> None:
        self.set_checked(list(self._ids) if checked else [])

    def _follow_selected(self) -> None:
        """Keep the default choice on the browser selection until the user changes it."""

        selected = self.context.selected_id
        checked = self.checked_ids()
        if selected in self._ids and checked in ([], [self._default_id]):
            self._default_id = selected
            self.set_checked([selected])

    def _update_scope(self) -> None:
        snapshot = self.context.snapshot
        ids = self.checked_ids()
        interval = self.context.interval
        if snapshot is None:
            self.scope.setText("")
            self.export_button.setEnabled(False)
            return
        count = 0
        for signal_id in ids:
            records = snapshot.series.get(signal_id, ())
            if interval is None:
                count += len(records)
            else:
                count += bisect_right(records, interval.end_ns, key=_time) - bisect_left(
                    records, interval.start_ns, key=_time
                )
        if interval is None:
            where = (
                "No time interval is selected, so the whole session is exported "
                "(Analysis > Select visible time range limits it)."
            )
        else:
            where = (
                f"Interval {format_seconds(interval.start_ns)} to "
                f"{format_seconds(interval.end_ns)} (session clock); samples exactly at "
                "either edge are included."
            )
        self.scope.setText(f"{where} {count} records from {len(ids)} signals.")
        self.export_button.setEnabled(bool(ids))

    def _set_status(self, text: str) -> None:
        self.status.setText(text)


class PlotImageSaver(QObject):
    """A "Save main plot as PNG" action for the window's main plot."""

    def __init__(self, window: QMainWindow, plot: pg.PlotWidget) -> None:
        super().__init__(window)
        self.window = window
        self.plot = plot
        self._directory = ""
        self.action = QAction("Save main plot as PNG...", window)
        self.action.triggered.connect(self.save)

    def save(self) -> Path | None:
        """Ask for a file name and write the plot, with axes, title and legend."""

        suggested = str(Path(self._directory) / "plot.png")
        path_text, _ = QFileDialog.getSaveFileName(
            self.window,
            "Save main plot as PNG",
            suggested,
            "PNG images (*.png)",
            "",
            QFileDialog.Option.DontConfirmOverwrite,
        )
        if not path_text:
            return None
        path = Path(path_text)
        if path.suffix.lower() != ".png":
            path = path.with_name(path.name + ".png")
        self._directory = str(path.parent)
        overwrite = False
        if path.exists():
            answer = QMessageBox.question(
                self.window,
                "Replace existing image?",
                f"{path.name} already exists in {path.parent}.\n\nReplace it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return None
            overwrite = True
        try:
            width, height = export_plot_image(self.plot, path, overwrite=overwrite, legend=True)
        except (ExportError, ValueError) as error:
            QMessageBox.warning(
                self.window,
                "Could not save the plot",
                f"{error}\n\nNo image was written; an existing file is unchanged.",
            )
            return None
        self.window.statusBar().showMessage(
            f"Saved the main plot ({width} x {height} px) to {path}", 8000
        )
        return path


TOOL = AnalysisTool("export", "Export", ExportTool)
