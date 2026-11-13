"""Application lifecycle and the supported desktop entry point."""

from __future__ import annotations

import gc
import signal
import sys
from collections.abc import Sequence

from PySide6.QtCore import QObject, QTimer
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


class GuiThreadGarbageCollector(QObject):
    """Run Python's cyclic garbage collector only on the GUI thread.

    Python collects cycles on whichever thread happens to allocate when a
    threshold is crossed. If that is a background worker and the garbage holds
    Qt objects, their destructors run off the GUI thread and can deadlock with
    the GUI thread (QObject mutex vs the GIL). Automatic collection is disabled
    while this object is active and a GUI-thread timer collects instead, using
    the same generation thresholds.
    """

    def __init__(self, parent: QObject | None = None, interval_ms: int = 1000) -> None:
        super().__init__(parent)
        self._thresholds = gc.get_threshold()
        self._was_enabled = gc.isenabled()
        gc.disable()
        self.timer = QTimer(self)
        self.timer.setInterval(interval_ms)
        self.timer.timeout.connect(self.collect)
        self.timer.start()

    def collect(self) -> int:
        """Collect each generation whose allocation count passed its threshold."""

        found = 0
        for generation, (count, threshold) in enumerate(
            zip(gc.get_count(), self._thresholds, strict=True)
        ):
            if threshold <= 0 or count <= threshold:
                break
            found += gc.collect(generation)
        return found

    def stop(self) -> None:
        """Stop the timer, collect once and restore automatic collection."""

        self.timer.stop()
        gc.collect()
        if self._was_enabled:
            gc.enable()


def install_garbage_collector(application: QApplication) -> GuiThreadGarbageCollector:
    """Keep cyclic garbage collection on the GUI thread for the app's lifetime."""

    existing = application.findChild(GuiThreadGarbageCollector)
    if existing is not None:
        return existing
    return GuiThreadGarbageCollector(application)


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
    install_garbage_collector(application)
    window = create_main_window(preferences_store=PreferencesStore())
    window.show()
    return application.exec()
