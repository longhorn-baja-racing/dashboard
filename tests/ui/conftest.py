"""Keep QApplication alive until every UI test and widget has finished."""

import gc
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QMainWindow

from lbr_dashboard.app import create_application


@pytest.fixture(scope="session", autouse=True)
def application_lifetime():
    application = create_application([])
    yield application
    application.closeAllWindows()
    application.processEvents()


@pytest.fixture(autouse=True)
def widget_cleanup(application_lifetime):
    yield
    for widget in QApplication.topLevelWidgets():
        if isinstance(widget, QMainWindow):
            widget.close()
            widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    application_lifetime.processEvents()
    gc.collect()
