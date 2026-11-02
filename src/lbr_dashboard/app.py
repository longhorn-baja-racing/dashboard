"""Application lifecycle and the supported desktop entry point."""

from __future__ import annotations

import logging
import signal
import sys
from collections.abc import Sequence

from PySide6.QtWidgets import QApplication

from .diagnostics import configure_logging, install_exception_hooks
from .importers.csv_importer import CsvImporter
from .ui.diagnostics import ErrorNotifier, attach_diagnostics, install_qt_message_handler
from .ui.main_window import MainWindow
from .version import __version__

logger = logging.getLogger(__name__)


def create_application(argv: Sequence[str] | None = None) -> QApplication:
    """Create or return the process QApplication instance."""

    application = QApplication.instance()
    if isinstance(application, QApplication):
        return application
    return QApplication(list(sys.argv if argv is None else argv))


def create_main_window() -> MainWindow:
    """Compose the application shell without showing it."""

    return MainWindow(importer=CsvImporter())


def main(argv: Sequence[str] | None = None) -> int:
    """Run the dashboard and return the Qt event-loop exit code."""

    signal.signal(signal.SIGINT, signal.SIG_DFL)
    diagnostics = configure_logging()
    restore_qt_messages = install_qt_message_handler()
    try:
        application = create_application(argv)
        notifier = ErrorNotifier(diagnostics)
        restore_hooks = install_exception_hooks(notifier.notify)
        try:
            window = create_main_window()
            attach_diagnostics(window, diagnostics, notifier)
            window.show()
            logger.info("LBR Dashboard %s started", __version__)
            code = application.exec()
            logger.info("LBR Dashboard exited with code %s", code)
            return code
        finally:
            restore_hooks()
    finally:
        restore_qt_messages()
        diagnostics.close()
