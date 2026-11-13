"""Error notices appear once, never block, and diagnostics can be copied or saved."""

from __future__ import annotations

import io
import logging
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import qWarning
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox, QPushButton
from pytestqt.qtbot import QtBot

from lbr_dashboard import app
from lbr_dashboard.diagnostics import (
    DiagnosticsSession,
    ErrorReport,
    configure_logging,
    install_exception_hooks,
)
from lbr_dashboard.ui import diagnostics as ui_diagnostics
from lbr_dashboard.ui.diagnostics import (
    DiagnosticsDialog,
    ErrorNotifier,
    attach_diagnostics,
    install_qt_message_handler,
)


@pytest.fixture
def session(tmp_path: Path) -> Iterator[DiagnosticsSession]:
    active = configure_logging(tmp_path / "logs", environ={}, stderr=io.StringIO())
    yield active
    active.close()


@pytest.fixture
def window(qtbot: QtBot) -> QMainWindow:
    widget = QMainWindow()
    qtbot.addWidget(widget)
    return widget


def report(summary: str = "ValueError: bad", location: str = "plot.py:10 in draw") -> ErrorReport:
    return ErrorReport(summary, location, "MainThread")


def button(box: QMessageBox, text: str) -> QPushButton:
    found = next(b for b in box.buttons() if b.text() == text)
    assert isinstance(found, QPushButton)
    return found


def close_box(notifier: ErrorNotifier) -> None:
    assert notifier.box is not None
    notifier.box.close()


# Notices ----------------------------------------------------------------------------


def test_same_error_is_shown_once(
    qtbot: QtBot, window: QMainWindow, session: DiagnosticsSession
) -> None:
    notifier = ErrorNotifier(session, window)
    notifier.notify(report())
    box = notifier.box
    assert box is not None and box.isVisible() and not box.isModal()
    assert box.text() == "Something went wrong: ValueError: bad"
    assert "plot.py:10 in draw" in box.informativeText()
    assert "saved to the log" in box.informativeText()
    assert "Thread: MainThread" in box.detailedText()
    close_box(notifier)
    notifier.notify(report("ValueError: bad again"))
    assert notifier.box is box and not box.isVisible()
    assert notifier.counts == {"ValueError@plot.py:10 in draw": 2}


def test_new_errors_while_a_notice_is_open_are_counted_not_stacked(
    window: QMainWindow, session: DiagnosticsSession
) -> None:
    notifier = ErrorNotifier(session, window)
    notifier.notify(report())
    first = notifier.box
    notifier.notify(report("KeyError: x", "table.py:3 in fill"))
    assert notifier.box is first and notifier.pending == 1
    assert first is not None and "1 more error(s)" in first.informativeText()
    close_box(notifier)
    notifier.notify(report("OSError: disk", "save.py:9 in write"))
    assert notifier.box is not first and notifier.pending == 0
    close_box(notifier)


def test_notice_explains_missing_log_file(window: QMainWindow) -> None:
    notifier = ErrorNotifier(None, window)
    notifier.notify(report())
    assert notifier.box is not None
    assert "log file is unavailable" in notifier.box.informativeText()
    close_box(notifier)


def test_copy_and_diagnostics_buttons(window: QMainWindow, session: DiagnosticsSession) -> None:
    opened: list[bool] = []
    notifier = ErrorNotifier(session, window, show_diagnostics=lambda: opened.append(True))
    notifier.notify(report())
    assert notifier.box is not None
    button(notifier.box, "Copy details").click()
    assert QGuiApplication.clipboard().text().startswith("LBR Dashboard diagnostics")
    notifier.notify(report("TypeError: y", "a.py:1 in f"))
    assert notifier.box is not None
    button(notifier.box, "Diagnostics...").click()
    assert opened == [True]
    notifier.notify(report("TypeError: z", "b.py:1 in g"))
    notifier._show_diagnostics = None
    assert notifier.box is not None
    button(notifier.box, "Diagnostics...").click()


def test_injected_worker_failure_is_logged_and_shown_once(
    qtbot: QtBot, window: QMainWindow, session: DiagnosticsSession
) -> None:
    notifier = ErrorNotifier(session, window)
    restore = install_exception_hooks(notifier.notify)
    try:

        def fail() -> None:
            raise RuntimeError("telemetry worker crashed")

        for _ in range(3):
            worker = threading.Thread(target=fail, name="telemetry-worker")
            worker.start()
            worker.join()
        qtbot.waitUntil(lambda: notifier.box is not None and notifier.box.isVisible())
        qtbot.waitUntil(lambda: sum(notifier.counts.values()) == 3)
    finally:
        restore()
    assert len(notifier.counts) == 1
    assert notifier.box is not None
    assert "telemetry worker crashed" in notifier.box.text()
    assert "Thread: telemetry-worker" in notifier.box.detailedText()
    assert session.log_file is not None and session.file_handler is not None
    session.file_handler.flush()
    log_text = session.log_file.read_text(encoding="utf-8")
    assert log_text.count("Uncaught exception in thread telemetry-worker") == 3
    assert "Traceback" in log_text
    assert window.isEnabled()
    close_box(notifier)


# Dialog ------------------------------------------------------------------------------


def test_dialog_shows_copies_and_refreshes_report(
    qtbot: QtBot, session: DiagnosticsSession
) -> None:
    dialog = DiagnosticsDialog(session)
    qtbot.addWidget(dialog)
    text = dialog.text.toPlainText()
    assert text.startswith("LBR Dashboard diagnostics") and "Qt runtime: " in text
    assert dialog.text.isReadOnly() and dialog.folder_button.isEnabled()
    dialog.copy_report()
    assert QGuiApplication.clipboard().text() == text
    assert "copied" in dialog.status.text()
    logging.getLogger("lbr_dashboard.test").error("new problem")
    dialog.refresh_button.click()
    assert "new problem" in dialog.text.toPlainText() and dialog.status.text() == ""


def test_dialog_saves_report(
    qtbot: QtBot, session: DiagnosticsSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dialog = DiagnosticsDialog(session)
    qtbot.addWidget(dialog)
    target = tmp_path / "report.txt"
    monkeypatch.setattr(
        ui_diagnostics.QFileDialog, "getSaveFileName", lambda *args: (str(target), "")
    )
    dialog.save_report()
    assert target.read_text(encoding="utf-8") == dialog.text.toPlainText()
    assert dialog.status.text() == f"Saved to {target}"

    monkeypatch.setattr(
        ui_diagnostics.QFileDialog, "getSaveFileName", lambda *args: (str(tmp_path), "")
    )
    dialog.save_report()
    assert dialog.status.text().startswith("Could not save the report")

    monkeypatch.setattr(ui_diagnostics.QFileDialog, "getSaveFileName", lambda *args: ("", ""))
    dialog.status.clear()
    dialog.save_report()
    assert dialog.status.text() == ""


def test_dialog_opens_log_folder(
    qtbot: QtBot, session: DiagnosticsSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    dialog = DiagnosticsDialog(session)
    qtbot.addWidget(dialog)
    opened: list[str] = []
    monkeypatch.setattr(
        ui_diagnostics.QDesktopServices,
        "openUrl",
        lambda url: opened.append(url.toLocalFile()) or True,
    )
    dialog.open_log_folder()
    assert session.log_file is not None
    assert Path(opened[0]) == session.log_file.parent
    monkeypatch.setattr(ui_diagnostics.QDesktopServices, "openUrl", lambda url: False)
    dialog.open_log_folder()
    assert dialog.status.text().startswith("Could not open")


def test_dialog_without_log_file_disables_folder(qtbot: QtBot) -> None:
    dialog = DiagnosticsDialog(None)
    qtbot.addWidget(dialog)
    assert not dialog.folder_button.isEnabled()
    dialog.open_log_folder()
    assert "logging not configured" in dialog.text.toPlainText()


# Menu and app wiring -----------------------------------------------------------------


def test_help_menu_opens_and_reuses_the_dialog(
    window: QMainWindow, session: DiagnosticsSession
) -> None:
    notifier = ErrorNotifier(session)
    menu = attach_diagnostics(window, session, notifier)
    titles = [action.text() for action in window.menuBar().actions()]
    assert "&Help" in titles
    menu.diagnostics_action.trigger()
    dialog = menu.dialog
    assert dialog is not None and dialog.isVisible()
    menu.diagnostics_action.trigger()
    assert menu.dialog is dialog
    notifier.notify(report())
    assert notifier.box is not None and notifier.box.parent() is window
    button(notifier.box, "Diagnostics...").click()
    assert menu.dialog is dialog and dialog.isVisible()
    dialog.close()


def test_qt_messages_are_logged(session: DiagnosticsSession) -> None:
    restore = install_qt_message_handler()
    try:
        qWarning("platform plugin hiccup")
    finally:
        restore()
    assert any("platform plugin hiccup" in line for line in session.recent_records())


def test_entry_point_logs_lifecycle_and_restores_hooks(
    qapp: QApplication,
    qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
    isolated_log_directory: Path,
) -> None:
    window = app.create_main_window()
    qtbot.addWidget(window)
    monkeypatch.setattr(app, "create_main_window", lambda: window)
    monkeypatch.setattr(app.signal, "signal", lambda *args: None)
    previous_hook = sys.excepthook
    root_handlers = list(logging.getLogger().handlers)
    from PySide6.QtCore import QTimer

    QTimer.singleShot(0, lambda: qapp.exit(3))
    assert app.main([]) == 3
    assert sys.excepthook is previous_hook
    assert logging.getLogger().handlers == root_handlers
    assert "&Help" in [action.text() for action in window.menuBar().actions()]
    text = (isolated_log_directory / "dashboard.log").read_text(encoding="utf-8")
    assert "LBR Dashboard 0.1.0 started" in text
    assert "exited with code 3" in text
