"""Native workspace docks addressed by stable widget and signal identities."""

from __future__ import annotations

import json
from collections.abc import Callable
from html import escape
from math import nan
from typing import Literal

import pyqtgraph as pg
from PySide6.QtCore import QMimeData, Qt
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import QDockWidget, QLabel, QMainWindow, QPushButton, QVBoxLayout, QWidget

from ..core import SessionSnapshot, SignalDescriptor
from .signal_browser import MIME_TYPE

DockKind = Literal["plot", "value"]


class SignalDock(QDockWidget):
    def __init__(self, key: str, label: str, kind: DockKind, workspace: WorkspaceDocks) -> None:
        super().__init__(label, workspace.window)
        self.setObjectName(key)
        self.kind = kind
        self.workspace = workspace
        self.binding: tuple[str, str] | None = None
        self.setAcceptDrops(True)
        self.feedback = QLabel("Drop a signal or use Bind selected signal.")
        self.feedback.setTextFormat(Qt.TextFormat.PlainText)
        self.feedback.setWordWrap(True)
        self.readout = QLabel()
        self.readout.setTextFormat(Qt.TextFormat.PlainText)
        self.readout.setWordWrap(True)
        self.plot = pg.PlotWidget() if kind == "plot" else None
        bind = QPushButton("Bind selected signal")
        bind.clicked.connect(self.bind_selected)
        remove = QPushButton("Remove widget")
        remove.clicked.connect(lambda: workspace.remove(key))
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.addWidget(self.feedback)
        layout.addWidget(self.readout)
        if self.plot is not None:
            self.plot.setLabel("bottom", "Time since first shown reading", units="s")
            layout.addWidget(self.plot)
        layout.addWidget(bind)
        layout.addWidget(remove)
        self.setWidget(body)

    def bind_selected(self) -> None:
        descriptor = self.workspace.descriptor(self.workspace.selected_id())
        if descriptor is None:
            self.feedback.setText("Select an available signal in the browser first.")
        else:
            self.bind(descriptor.identifier, descriptor.source_id)

    def bind(self, signal_id: str, source_id: str) -> bool:
        descriptor = self.workspace.descriptor(signal_id)
        if descriptor is None or descriptor.source_id != source_id:
            self.feedback.setText("That signal is missing from the current source.")
            return False
        if self.kind == "plot" and (
            descriptor.data_type not in {"numeric", "boolean"} or descriptor.shape
        ):
            self.feedback.setText("Choose a scalar numeric or boolean signal for a plot.")
            return False
        self.binding = (signal_id, source_id)
        self.feedback.setText(f"Bound to {descriptor.display_name} ({signal_id}).")
        self.render()
        return True

    def bind_payload(self, mime: QMimeData) -> bool:
        raw = mime.data(MIME_TYPE)
        try:
            if not mime.hasFormat(MIME_TYPE) or not 0 < raw.size() <= 4096:
                raise ValueError
            payload = json.loads(bytes(raw.data()))
            if type(payload["version"]) is not int or payload["version"] != 1:
                raise ValueError
            (signal,) = payload["signals"]
            signal_id, source_id = signal["signal_id"], signal["source_id"]
            if not isinstance(signal_id, str) or not isinstance(source_id, str):
                raise ValueError
        except (KeyError, RecursionError, TypeError, ValueError):
            self.feedback.setText("Drop one signal from the dashboard signal browser.")
            return False
        return self.bind(signal_id, source_id)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasFormat(MIME_TYPE):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        if self.bind_payload(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def render(self) -> None:
        if self.plot is not None:
            self.plot.clear()
            self.plot.setLabel("left", "Value")
        snapshot = self.workspace.snapshot
        if self.binding is None:
            return
        signal_id, source_id = self.binding
        descriptor = self.workspace.descriptor(signal_id)
        if descriptor is None or descriptor.source_id != source_id or snapshot is None:
            self.readout.setText(f"Missing signal: {signal_id}")
            return
        records = snapshot.series[signal_id][-1000:]
        if not records:
            self.readout.setText("No readings yet.")
            return
        latest = records[-1].sample
        unit = latest.unit or descriptor.canonical_unit or ""
        self.readout.setText(
            f"Latest: {latest.value} {unit}".rstrip() if latest.available else "Latest: unavailable"
        )
        if self.plot is not None:
            self.plot.setLabel("left", escape(descriptor.display_name), units=escape(unit))
            origin = records[0].sample.timestamp_ns
            times = [(record.sample.timestamp_ns - origin) / 1e9 for record in records]
            values = [self._numeric_value(record.sample.value) for record in records]
            self.plot.plot(
                times, values, connect="finite", symbol="o" if len(times) < 100 else None
            )

    @staticmethod
    def _numeric_value(value: object) -> float:
        try:
            return float(value) if isinstance(value, (int, float)) else nan
        except OverflowError:
            return nan


class WorkspaceDocks:
    def __init__(self, window: QMainWindow, selected_id: Callable[[], str | None]) -> None:
        self.window = window
        self.selected_id = selected_id
        self.snapshot: SessionSnapshot | None = None
        self.docks: dict[str, SignalDock] = {}
        self._descriptors: dict[str, SignalDescriptor] = {}
        self._next_id = 0
        menu_bar = window.menuBar()
        assert menu_bar is not None
        self.menu = menu_bar.addMenu("Workspace")
        self.menu.addAction("Add plot widget", lambda: self.add("plot"))
        self.menu.addAction("Add value widget", lambda: self.add("value"))
        self.menu.addSeparator()

    def descriptor(self, signal_id: str | None) -> SignalDescriptor | None:
        return self._descriptors.get(signal_id) if signal_id is not None else None

    def add(self, kind: DockKind) -> SignalDock:
        self._next_id += 1
        key = f"workspace.{kind}.{self._next_id}"
        dock = SignalDock(key, f"{kind.title()} {self._next_id}", kind, self)
        self.docks[key] = dock
        self.window.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        self.menu.addAction(dock.toggleViewAction())
        dock.show()
        return dock

    def remove(self, key: str) -> None:
        dock = self.docks.pop(key)
        self.menu.removeAction(dock.toggleViewAction())
        self.window.removeDockWidget(dock)
        dock.hide()
        dock.deleteLater()

    def update_snapshot(self, snapshot: SessionSnapshot | None) -> None:
        self.snapshot = snapshot
        self._descriptors = (
            {item.identifier: item for item in snapshot.schema.signals} if snapshot else {}
        )
        for dock in self.docks.values():
            dock.render()
