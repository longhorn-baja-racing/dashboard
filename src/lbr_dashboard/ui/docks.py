"""Native workspace docks addressed by stable widget and signal identities."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Literal

import pyqtgraph as pg
from PySide6.QtCore import QMimeData, Qt
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QComboBox,
    QDockWidget,
    QLabel,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core import SessionSnapshot, SignalDescriptor
from ..core.signals import SignalSample
from ..core.value_display import describe_value
from .multi_trace_plot import Axis, MultiTracePlot
from .plot_style import apply_plot_colors
from .signal_browser import MIME_TYPE
from .value_widget import SignalValueWidget

DockKind = Literal["plot", "value"]


class SignalDock(QDockWidget):
    def __init__(self, key: str, label: str, kind: DockKind, workspace: WorkspaceDocks) -> None:
        super().__init__(label, workspace.window)
        self.setObjectName(key)
        self.kind = kind
        self.workspace = workspace
        self.binding: tuple[str, str] | None = None
        self.origin_ns: int | None = None
        self.cursor: pg.InfiniteLine | None = None
        self.setAcceptDrops(True)
        self.feedback = QLabel("Drop a signal or use Bind selected signal.")
        self.feedback.setTextFormat(Qt.TextFormat.PlainText)
        self.feedback.setWordWrap(True)
        self.readout = QLabel()
        self.readout.setTextFormat(Qt.TextFormat.PlainText)
        self.readout.setWordWrap(True)
        self.selected_reading = QLabel("Selected: no aligned reading")
        self.selected_reading.setTextFormat(Qt.TextFormat.PlainText)
        self.selected_reading.setWordWrap(True)
        self.value_widget = SignalValueWidget("") if kind == "value" else None
        self.value_reference = QLabel("Latest reading")
        self.presenting_replay_value = False
        self.plot = pg.PlotWidget() if kind == "plot" else None
        self.multi_plot = MultiTracePlot(self.plot) if self.plot is not None else None
        bind = QPushButton("Bind selected signal")
        bind.clicked.connect(self.bind_selected)
        remove = QPushButton("Remove widget")
        remove.clicked.connect(lambda: workspace.remove(key))
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.addWidget(self.feedback)
        if self.value_widget is not None:
            layout.addWidget(self.value_reference)
            layout.addWidget(self.value_widget)
        else:
            layout.addWidget(self.readout)
            layout.addWidget(self.selected_reading)
        if self.plot is not None:
            self.plot.setLabel("bottom", "Time since first shown reading", units="s")
            layout.addWidget(self.plot)
            self.trace_list = QComboBox()
            self.trace_list.setAccessibleName("Plot traces")
            self.axis_choice = QComboBox()
            self.axis_choice.addItems(["Automatic axis", "Left axis", "Right axis"])
            self.axis_choice.setAccessibleName("Trace axis")
            add = QPushButton("Add selected signal")
            add.clicked.connect(self.add_selected)
            assign = QPushButton("Assign trace axis")
            assign.clicked.connect(self.assign_trace_axis)
            remove_trace = QPushButton("Remove selected trace")
            remove_trace.clicked.connect(self.remove_trace)
            fit = QPushButton("Fit traces")
            assert self.multi_plot is not None
            fit.clicked.connect(self.multi_plot.fit)
            layout.addWidget(self.trace_list)
            layout.addWidget(self.axis_choice)
            layout.addWidget(add)
            layout.addWidget(assign)
            layout.addWidget(remove_trace)
            layout.addWidget(fit)
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
        if self.multi_plot is not None:
            # Validate a replacement before discarding the current plot.
            previous = self.multi_plot.configuration()
            self.multi_plot.clear()
            if not self.add_trace(signal_id, source_id):
                self.multi_plot.restore_configuration(previous)
                return False
        self.binding = (signal_id, source_id)
        self.feedback.setText(f"Bound to {descriptor.display_name} ({signal_id}).")
        self.render()
        return True

    def add_selected(self) -> None:
        descriptor = self.workspace.descriptor(self.workspace.selected_id())
        if descriptor is None:
            self.feedback.setText("Select an available signal in the browser first.")
            return
        self.add_trace(descriptor.identifier, descriptor.source_id)

    def _chosen_axis(self) -> Axis | None:
        if self.axis_choice.currentIndex() == 1:
            return "left"
        if self.axis_choice.currentIndex() == 2:
            return "right"
        return None

    def add_trace(self, signal_id: str, source_id: str) -> bool:
        descriptor = self.workspace.descriptor(signal_id)
        if self.multi_plot is None or descriptor is None or descriptor.source_id != source_id:
            self.feedback.setText("That signal is missing from the current source.")
            return False
        self.multi_plot.snapshot = self.workspace.snapshot
        try:
            self.multi_plot.add(descriptor, self._chosen_axis())
        except ValueError as error:
            self.feedback.setText(str(error))
            return False
        if self.binding is None:
            self.binding = (signal_id, source_id)
        self.feedback.setText(f"Added {descriptor.display_name}.")
        self.render()
        return True

    def _refresh_trace_list(self) -> None:
        assert self.multi_plot is not None
        selected = self.trace_list.currentData()
        self.trace_list.clear()
        for key in self.multi_plot.bindings:
            self.trace_list.addItem(self.multi_plot.label(key), key)
        index = self.trace_list.findData(selected)
        if index >= 0:
            self.trace_list.setCurrentIndex(index)

    def remove_trace(self) -> None:
        assert self.multi_plot is not None
        signal_id = self.trace_list.currentData()
        if signal_id is None:
            return
        self.multi_plot.remove(signal_id)
        first = next(iter(self.multi_plot.bindings.values()), None)
        self.binding = (first.signal_id, first.source_id) if first is not None else None
        self.render()

    def assign_trace_axis(self) -> None:
        assert self.multi_plot is not None
        signal_id, axis = self.trace_list.currentData(), self._chosen_axis()
        if signal_id is None or axis is None:
            self.feedback.setText("Choose a trace and Left axis or Right axis.")
            return
        try:
            self.multi_plot.assign_axis(signal_id, axis)
        except ValueError as error:
            self.feedback.setText(str(error))
            return
        self.render()

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
        if self.multi_plot is not None:
            return self.add_trace(signal_id, source_id)
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
        self.origin_ns = None
        if self.multi_plot is not None:
            problems = self.multi_plot.update_snapshot(self.workspace.snapshot)
            self._refresh_trace_list()
            if problems:
                self.feedback.setText("\n".join(problems))
            elif self.multi_plot.bindings:
                self.feedback.setText(
                    "Drop a signal or use Add selected signal to add another trace."
                )
            else:
                self.feedback.setText("Drop a signal or use Bind selected signal.")
            self.origin_ns = self.multi_plot.origin_ns if self.multi_plot.has_origin else None
        snapshot = self.workspace.snapshot
        if self.binding is None:
            self.readout.clear()
            if self.value_widget is not None:
                self.value_widget.set_signal_id("")
            return
        signal_id, source_id = self.binding
        descriptor = self.workspace.descriptor(signal_id)
        if descriptor is None or descriptor.source_id != source_id or snapshot is None:
            self.readout.setText(f"Missing signal: {signal_id}")
            if self.value_widget is not None:
                self.presenting_replay_value = False
                self.feedback.setText(f"Missing signal: {signal_id}. Bind an available signal.")
                self.value_reference.setText("Latest reading")
                self.value_widget.set_signal_id(signal_id)
            return
        records = snapshot.series[signal_id]
        if self.value_widget is not None:
            self.feedback.setText(f"Bound to {descriptor.display_name} ({signal_id}).")
            self.present_value(descriptor, records[-1].sample if records else None)
            return
        if not records:
            self.readout.setText("No readings yet.")
            return
        latest = records[-1].sample
        unit = latest.unit or descriptor.canonical_unit or ""
        self.readout.setText(
            f"Latest: {latest.value} {unit}".rstrip() if latest.available else "Latest: unavailable"
        )

    def present_value(
        self,
        descriptor: SignalDescriptor,
        sample: SignalSample | None,
        *,
        selected_ns: int | None = None,
        max_age_ns: int | None = None,
    ) -> None:
        widget = self.value_widget
        assert widget is not None
        if widget.signal_id != descriptor.identifier:
            widget.set_signal_id(descriptor.identifier)
        self.presenting_replay_value = selected_ns is not None
        self.value_reference.setText(
            "Latest reading" if selected_ns is None else "Reading at replay time"
        )
        widget.show_display(
            describe_value(descriptor, sample, now_ns=selected_ns, max_age_ns=max_age_ns)
        )


class WorkspaceDocks:
    def __init__(self, window: QMainWindow, selected_id: Callable[[], str | None]) -> None:
        self.window = window
        self.selected_id = selected_id
        self.snapshot: SessionSnapshot | None = None
        self.docks: dict[str, SignalDock] = {}
        self._descriptors: dict[str, SignalDescriptor] = {}
        self._next_id = 0
        self._plot_colors = ("#202020", "#eeeeee")
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
        if dock.plot is not None:
            apply_plot_colors(dock.plot, *self._plot_colors)
        if dock.multi_plot is not None:
            dock.multi_plot.set_background(self._plot_colors[0])
        self.docks[key] = dock
        self.window.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        self.menu.addAction(dock.toggleViewAction())
        dock.show()
        return dock

    def set_plot_colors(self, background: str, text: str) -> None:
        self._plot_colors = (background, text)
        for dock in self.docks.values():
            if dock.plot is not None:
                apply_plot_colors(dock.plot, *self._plot_colors)
            if dock.multi_plot is not None:
                dock.multi_plot.set_background(background)

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
