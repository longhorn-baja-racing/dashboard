"""Application lifecycle and the supported desktop entry point."""

from __future__ import annotations

import signal
import sys
from collections.abc import Sequence

from PySide6.QtWidgets import QApplication

from .core.log import LogImporter
from .core.preferences import PreferencesStore
from .core.registry import Registry
from .dashboard_controller import DashboardController
from .importers.csv_importer import CsvImporter
from .preferences_controller import PreferencesController
from .ui.main_window import MainWindow


def create_application(argv: Sequence[str] | None = None) -> QApplication:
    """Create or return the process QApplication instance."""

    application = QApplication.instance()
    if isinstance(application, QApplication):
        return application
    return QApplication(list(sys.argv if argv is None else argv))


def create_main_window(*, preferences_store: PreferencesStore | None = None) -> MainWindow:
    """Compose the application shell without showing it."""

    importers: Registry[LogImporter] = Registry("importers")
    importers.register("csv", CsvImporter)
    window = MainWindow()
    window.attach_controller(DashboardController(window, importers.create("csv")))
    window.preferences_controller = PreferencesController(window, preferences_store)
    return window


def main(argv: Sequence[str] | None = None) -> int:
    """Run the dashboard and return the Qt event-loop exit code."""

    signal.signal(signal.SIGINT, signal.SIG_DFL)
    application = create_application(argv)
    window = create_main_window(preferences_store=PreferencesStore())
    window.show()
    return application.exec()
