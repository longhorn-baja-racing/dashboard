"""Verify workspace actions, browser MIME interoperability and failed drops."""

import json

from PySide6.QtCore import QMimeData, QPointF, Qt
from PySide6.QtGui import QDropEvent
from PySide6.QtWidgets import QPushButton

from lbr_dashboard.app import create_main_window
from lbr_dashboard.core import SignalDescriptor, SignalSample, SignalSchema, TelemetrySession
from lbr_dashboard.ui.signal_browser import MIME_TYPE


def test_native_docks_bind_move_reopen_and_remove_without_losing_valid_binding(qtbot):
    window = create_main_window()
    qtbot.addWidget(window)
    owner = window.source_owner
    assert owner is not None
    window.show()
    workspace = window.workspace_docks
    pressure = SignalDescriptor("lab.pressure", "Pressure", ("lab",), "numeric", "Bench")
    state = SignalDescriptor("lab.state", "State", ("lab",), "string", "Bench")
    session = TelemetrySession(SignalSchema(1, (pressure, state)))
    session.append(pressure.identifier, SignalSample(0, 0))
    session.append(pressure.identifier, SignalSample(10, None, available=False))
    session.append(state.identifier, SignalSample(0, "ready"))
    workspace.update_snapshot(session.snapshot())
    browser = window.left_panel.signal_browser
    browser.refresh(session.schema, {pressure.identifier: (True, ""), state.identifier: (True, "")})
    browser.select_id(pressure.identifier)

    workspace.menu.actions()[0].trigger()
    plot = next(iter(workspace.docks.values()))
    current = browser.tree.currentIndex()
    model = browser.tree.model()
    assert model is not None
    mime = model.mimeData([current])
    assert mime.hasFormat(MIME_TYPE)
    drop = QDropEvent(
        QPointF(0, 0),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    plot.dropEvent(drop)
    assert drop.isAccepted() and plot.binding == (pressure.identifier, "Bench")
    assert plot.readout.text() == "Latest: unavailable"

    for body in (
        b"{" * 1001,
        json.dumps({"version": True, "signals": []}).encode(),
        json.dumps(
            {"version": 1, "signals": [{"signal_id": "missing", "source_id": "Bench"}]}
        ).encode(),
        json.dumps(
            {"version": 1, "signals": [{"signal_id": pressure.identifier, "source_id": "Other"}]}
        ).encode(),
    ):
        invalid = QMimeData()
        invalid.setData(MIME_TYPE, body)
        assert not plot.bind_payload(invalid)
        assert plot.binding == (pressure.identifier, "Bench")
    browser.select_id(state.identifier)
    assert not plot.bind(state.identifier, "Bench")
    workspace.menu.actions()[1].trigger()
    value = workspace.docks["workspace.value.2"]
    bind = next(button for button in value.findChildren(QPushButton) if "Bind" in button.text())
    qtbot.keyClick(bind, Qt.Key.Key_Space)
    assert value.readout.text() == "Latest: ready"
    window.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, value)
    assert window.dockWidgetArea(value) == Qt.DockWidgetArea.LeftDockWidgetArea
    value.close()
    assert not value.isVisible()
    value.toggleViewAction().trigger()
    assert value.isVisible() and value.binding == (state.identifier, "Bench")
    workspace.update_snapshot(None)
    assert "Missing signal" in value.readout.text()
    key = value.objectName()
    remove = next(button for button in value.findChildren(QPushButton) if "Remove" in button.text())
    qtbot.keyClick(remove, Qt.Key.Key_Space)
    assert key not in workspace.docks
    window.close()
