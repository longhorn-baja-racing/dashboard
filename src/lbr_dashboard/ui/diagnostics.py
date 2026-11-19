"""Qt presentation for diagnostics: error notices, report dialog and Help menu."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import (
    QMessageLogContext,
    QObject,
    QtMsgType,
    QUrl,
    Signal,
    qInstallMessageHandler,
    qVersion,
)
from PySide6.QtGui import QAction, QDesktopServices, QFontDatabase, QGuiApplication
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..diagnostics import APP_LOGGER, DiagnosticsSession, ErrorReport, build_report

qt_logger = logging.getLogger(f"{APP_LOGGER}.qt")

_QT_LEVELS = {
    QtMsgType.QtDebugMsg: logging.DEBUG,
    QtMsgType.QtInfoMsg: logging.INFO,
    QtMsgType.QtWarningMsg: logging.WARNING,
    QtMsgType.QtCriticalMsg: logging.ERROR,
    QtMsgType.QtFatalMsg: logging.CRITICAL,
}


def install_qt_message_handler() -> Callable[[], None]:
    """Send Qt's own warnings to the log; return a function that undoes it."""

    def handler(kind: QtMsgType, context: QMessageLogContext, message: str) -> None:
        category = context.category or "qt"
        qt_logger.log(_QT_LEVELS.get(kind, logging.WARNING), "[%s] %s", category, message)

    previous = qInstallMessageHandler(handler)

    def restore() -> None:
        qInstallMessageHandler(previous)

    return restore


def report_text(session: DiagnosticsSession | None) -> str:
    return build_report(session, extra={"Qt runtime": qVersion()})


class DiagnosticsDialog(QDialog):
    """Non-modal window with a copyable, redacted diagnostics report."""

    def __init__(self, session: DiagnosticsSession | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._session = session
        self.setWindowTitle("Diagnostics")
        self.resize(720, 480)

        intro = QLabel(
            "Copy this report into an issue or message when something goes wrong. "
            "It lists versions, the log location and recent warnings and errors. "
            "Telemetry values and file contents are not included."
        )
        intro.setWordWrap(True)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.status = QLabel()

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.copy_button = QPushButton("Copy report")
        self.save_button = QPushButton("Save report...")
        self.folder_button = QPushButton("Open log folder")
        self.refresh_button = QPushButton("Refresh")
        for button in (self.copy_button, self.save_button, self.folder_button, self.refresh_button):
            buttons.addButton(button, QDialogButtonBox.ButtonRole.ActionRole)
        self.folder_button.setEnabled(session is not None and session.log_file is not None)

        self.copy_button.clicked.connect(self.copy_report)
        self.save_button.clicked.connect(self.save_report)
        self.folder_button.clicked.connect(self.open_log_folder)
        self.refresh_button.clicked.connect(self.refresh)
        buttons.rejected.connect(self.close)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.text)
        layout.addWidget(self.status)
        layout.addWidget(buttons)
        self.refresh()

    def refresh(self) -> None:
        self.text.setPlainText(report_text(self._session))
        self.status.clear()

    def copy_report(self) -> None:
        QGuiApplication.clipboard().setText(self.text.toPlainText())
        self.status.setText("Report copied to the clipboard.")

    def save_report(self) -> None:
        name, _ = QFileDialog.getSaveFileName(
            self, "Save diagnostics report", "lbr-dashboard-diagnostics.txt", "Text files (*.txt)"
        )
        if not name:
            return
        try:
            Path(name).write_text(self.text.toPlainText(), encoding="utf-8")
        except OSError as error:
            self.status.setText(f"Could not save the report: {error.strerror or error}")
        else:
            self.status.setText(f"Saved to {name}")

    def open_log_folder(self) -> None:
        if self._session is None or self._session.log_file is None:
            return
        folder = self._session.log_file.parent
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))):
            self.status.setText(f"Could not open {folder}; the path is shown in the report.")


class ErrorNotifier(QObject):
    """Tell the user about uncaught errors once, without blocking or stacking dialogs.

    :meth:`notify` is safe to call from any thread; the notice is shown on the
    GUI thread. A repeated failure (same type and location) is counted but not
    shown again, and while one notice is open further ones are only counted.
    """

    _received = Signal(object)

    def __init__(
        self,
        session: DiagnosticsSession | None,
        parent: QWidget | None = None,
        show_diagnostics: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._session = session
        self._parent = parent
        self._show_diagnostics = show_diagnostics
        self.counts: dict[str, int] = {}
        self.box: QMessageBox | None = None
        self.pending = 0
        self._received.connect(self._present)

    def set_parent_window(self, window: QWidget, show_diagnostics: Callable[[], None]) -> None:
        self._parent = window
        self._show_diagnostics = show_diagnostics

    def notify(self, report: ErrorReport) -> None:
        self._received.emit(report)

    def _present(self, report: ErrorReport) -> None:
        seen = self.counts.get(report.signature, 0)
        self.counts[report.signature] = seen + 1
        if seen:
            return
        if self.box is not None and self.box.isVisible():
            self.pending += 1
            self.box.setInformativeText(self._informative(report.location))
            return
        self.pending = 0
        box = QMessageBox(self._parent)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Unexpected error")
        box.setText(f"Something went wrong: {report.summary}")
        box.setInformativeText(self._informative(report.location))
        box.setDetailedText(
            f"{report.summary}\nLocation: {report.location}\nThread: {report.thread}"
        )
        copy = box.addButton("Copy details", QMessageBox.ButtonRole.ActionRole)
        diagnostics = box.addButton("Diagnostics...", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Close)
        copy.clicked.connect(
            lambda: QGuiApplication.clipboard().setText(report_text(self._session))
        )
        diagnostics.clicked.connect(self._open_diagnostics)
        box.setModal(False)
        box.show()
        self.box = box

    def _informative(self, location: str) -> str:
        if self._session is not None and self._session.log_file is not None:
            where = "Details were saved to the log."
        else:
            where = "The log file is unavailable, so copy the details now."
        text = (
            f"At {location}. The dashboard is still running, but the last action may not "
            f"have finished. {where} Help > Diagnostics shows a report you can copy."
        )
        if self.pending:
            text += f"\n\n{self.pending} more error(s) occurred; see Diagnostics."
        return text

    def _open_diagnostics(self) -> None:
        if self._show_diagnostics is not None:
            self._show_diagnostics()


class DiagnosticsMenu(QObject):
    """Help menu entries for diagnostics, added to an existing main window."""

    def __init__(self, window: QMainWindow, session: DiagnosticsSession | None) -> None:
        super().__init__(window)
        self._window = window
        self._session = session
        self.dialog: DiagnosticsDialog | None = None
        menu = window.menuBar().addMenu("&Help")
        self.diagnostics_action = QAction("&Diagnostics...", window)
        self.diagnostics_action.triggered.connect(self.show_dialog)
        menu.addAction(self.diagnostics_action)

    def show_dialog(self) -> None:
        if self.dialog is None:
            self.dialog = DiagnosticsDialog(self._session, self._window)
        else:
            self.dialog.refresh()
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()


def attach_diagnostics(
    window: QMainWindow, session: DiagnosticsSession | None, notifier: ErrorNotifier
) -> DiagnosticsMenu:
    """Add the Help > Diagnostics menu and let error notices open it."""

    menu = DiagnosticsMenu(window, session)
    notifier.set_parent_window(window, menu.show_dialog)
    return menu
