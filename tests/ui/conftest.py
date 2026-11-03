"""Keep QApplication alive until every UI test and widget has finished."""

import gc
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QMainWindow


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
