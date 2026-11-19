"""Keep QApplication alive until every UI test and widget has finished."""

import gc
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QMainWindow

from lbr_dashboard.app import install_garbage_collector


@pytest.fixture(scope="session", autouse=True)
def gui_thread_garbage_collection(qapp):
    """Collect cycles on the GUI thread only, as the app does.

    Without this, a background source thread can trigger a collection that
    destroys Qt widgets from an earlier test off the GUI thread and deadlocks.
    """

    collector = install_garbage_collector(qapp)
    yield collector
    collector.stop()
    collector.deleteLater()


@pytest.fixture(autouse=True)
def widget_cleanup(qapp, request):
    yield
    if "qtbot" in request.fixturenames:
        return  # pytest-qt owns and releases widgets registered with qtbot.
    for widget in QApplication.topLevelWidgets():
        if isinstance(widget, QMainWindow):
            widget.close()
            widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()
    gc.collect()
