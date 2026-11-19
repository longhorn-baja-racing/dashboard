"""Exercise multi-trace docks through their controls and renderer."""

import json
import math
from dataclasses import replace
from itertools import islice

import pyqtgraph as pg
import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QPushButton

from lbr_dashboard.app import create_main_window
from lbr_dashboard.core import SignalDescriptor, SignalSample, SignalSchema, TelemetrySession
from lbr_dashboard.source_controller import SourceController
from lbr_dashboard.ui.time_plot import POINT_LIMIT


def descriptor(identifier, unit: str | None = "Pa", **kwargs):
    return SignalDescriptor(
        identifier,
        identifier.title(),
        ("bench",),
        "numeric",
        "bench",
        canonical_unit=unit,
        timestamp_domain="relative",
        **kwargs,
    )


@pytest.fixture
def workspace(qtbot):
    window = create_main_window()
    qtbot.addWidget(window)
    window.show()
    return window.workspace_docks


def populate(workspace, descriptors, readings=None):
    session = TelemetrySession(SignalSchema(1, tuple(descriptors)))
    for item in descriptors:
        values = (
            readings[item.identifier] if readings is not None else [(0, 0), (1_000_000_000, 10)]
        )
        samples = (
            SignalSample(
                t, value, available=value is not None, timestamp_domain=item.timestamp_domain
            )
            for t, value in values
        )
        while batch := tuple(islice(samples, 5000)):
            session.extend(item.identifier, batch)
    snapshot = session.snapshot()
    workspace.update_snapshot(snapshot)
    workspace.window.left_panel.signal_browser.refresh(
        snapshot.schema, {d.identifier: (True, "") for d in descriptors}
    )
    return session


def press(dock, text):
    button = next(b for b in dock.findChildren(QPushButton) if b.text() == text)
    button.click()


def add_selected(workspace, dock, identifier):
    workspace.window.left_panel.signal_browser.select_id(identifier, emit=True)
    press(dock, "Add selected signal")


def test_controls_group_units_and_preserve_independent_scales_and_measurement_times(workspace):
    pressure, backup, speed = (
        descriptor("lab.pressure"),
        descriptor("lab.backup"),
        descriptor("lab.speed", "m/s"),
    )
    origin = 2**60
    session = populate(
        workspace,
        [pressure, backup, speed],
        {
            pressure.identifier: [(origin, 0), (origin + 50, None), (origin + 120, 10)],
            backup.identifier: [(origin + 30, 5), (origin + 110, 7)],
            speed.identifier: [(origin + 25, 1000), (origin + 125, 2000)],
        },
    )
    before = session.snapshot()
    dock = workspace.add("plot")
    for item in (pressure, backup, speed):
        add_selected(workspace, dock, item.identifier)
    plot = dock.multi_plot
    assert plot is not None
    assert [b.axis for b in plot.bindings.values()] == ["left", "left", "right"]
    x, y = plot.curves[pressure.identifier].getData()
    assert x.tolist() == [0, 50e-9, 120e-9] and math.isnan(y[1])
    assert plot.curves[pressure.identifier].opts["connect"].tolist() == [False, False, False]
    assert plot.curves[speed.identifier].getData()[0].tolist() == [25e-9, 125e-9]
    assert len(plot.legend.items) == 3
    assert all(
        item.identifier in dock.trace_list.itemText(i)
        for i, item in enumerate((pressure, backup, speed))
    )
    assert plot.plot.getAxis("left").labelUnits == "Pa"
    assert plot.plot.getAxis("right").labelUnits == "m/s"
    assert plot.view.viewRange()[1][1] < 20
    assert plot.right_view.viewRange()[1][0] > 900
    assert plot.right_view.viewRange()[0] == pytest.approx(plot.view.viewRange()[0])
    assert session.snapshot() == before


def test_axis_cleanup_explicit_assignment_stable_colors_and_hidden_state(workspace):
    first, second = descriptor("sample.first"), descriptor("sample.second")
    session = populate(workspace, [first, second])
    dock = workspace.add("plot")
    add_selected(workspace, dock, first.identifier)
    dock.axis_choice.setCurrentIndex(2)
    add_selected(workspace, dock, second.identifier)
    plot = dock.multi_plot
    assert plot is not None and plot.right_view is not None
    color = plot.bindings[second.identifier].color
    plot.curves[second.identifier].hide()
    workspace.update_snapshot(session.snapshot())
    assert not plot.curves[second.identifier].isVisible()
    dock.trace_list.setCurrentIndex(dock.trace_list.findData(second.identifier))
    dock.axis_choice.setCurrentIndex(1)
    old_right = plot.right_view
    press(dock, "Assign trace axis")
    assert plot.right_view is None and old_right.scene() is None
    assert not plot.plot.getAxis("right").isVisible()
    assert plot.bindings[second.identifier].color == color
    assert not plot.curves[second.identifier].isVisible()
    press(dock, "Remove selected trace")
    assert second.identifier not in plot.bindings and len(plot.legend.items) == 1
    dock.axis_choice.setCurrentIndex(2)
    add_selected(workspace, dock, second.identifier)
    assert plot.bindings[second.identifier].color == color
    dock.trace_list.setCurrentIndex(dock.trace_list.findData(second.identifier))
    press(dock, "Remove selected trace")
    assert plot.right_view is None
    press(dock, "Remove selected trace")
    assert not plot.curves and not plot.legend.items and dock.binding is None
    assert not plot.has_origin


def test_axis_incompatibility_duplicate_drop_and_failed_bind_preserve_current_plot(workspace):
    pressure, speed, temperature = (
        descriptor("lab.pressure"),
        descriptor("lab.speed", "m/s"),
        descriptor("lab.temperature", "K"),
    )
    populate(workspace, [pressure, speed, temperature])
    dock = workspace.add("plot")
    add_selected(workspace, dock, pressure.identifier)
    add_selected(workspace, dock, speed.identifier)
    plot = dock.multi_plot
    assert plot is not None
    original = plot.configuration()
    add_selected(workspace, dock, temperature.identifier)
    assert "Both axes" in dock.feedback.text() and plot.configuration() == original
    dock.trace_list.setCurrentIndex(dock.trace_list.findData(speed.identifier))
    dock.axis_choice.setCurrentIndex(1)
    press(dock, "Assign trace axis")
    assert "different" in dock.feedback.text() and plot.configuration() == original
    browser = workspace.window.left_panel.signal_browser
    browser.select_id(pressure.identifier)
    model = browser.tree.model()
    assert model is not None
    mime = model.mimeData([browser.tree.currentIndex()])
    assert not dock.bind_payload(mime)
    assert "already" in dock.feedback.text() and plot.configuration() == original
    assert not dock.bind(temperature.identifier, "missing")
    assert plot.configuration() == original


def test_unknown_units_are_not_silently_grouped_and_mixed_units_are_rejected(workspace):
    first, second = descriptor("lab.first", None), descriptor("lab.second", None)
    session = populate(workspace, [first, second])
    dock = workspace.add("plot")
    add_selected(workspace, dock, first.identifier)
    add_selected(workspace, dock, second.identifier)
    plot = dock.multi_plot
    assert plot is not None
    assert [b.axis for b in plot.bindings.values()] == ["left", "right"]
    assert plot.plot.getAxis("left").labelUnits == "unit unspecified"
    session.append(
        first.identifier, SignalSample(2_000_000_000, 4, unit="V", timestamp_domain="relative")
    )
    workspace.update_snapshot(session.snapshot())
    assert "mixed" in dock.feedback.text()
    assert plot.curves[first.identifier].getData() == (None, None)
    assert plot.curves[second.identifier].getData()[1].tolist() == [0, 10]


def test_config_round_trip_is_portable_and_invalid_import_is_transactional(workspace):
    first, second = descriptor("lab.first"), descriptor("lab.second", "m/s")
    populate(workspace, [first, second])
    dock = workspace.add("plot")
    for item in (first, second):
        add_selected(workspace, dock, item.identifier)
    plot = dock.multi_plot
    assert plot is not None
    plot.curves[first.identifier].hide()
    config = json.loads(json.dumps(plot.configuration()))
    other = workspace.add("plot").multi_plot
    assert other is not None
    other.snapshot = workspace.snapshot
    other.restore_configuration(config)
    assert other.configuration() == config
    assert {k: b.color for k, b in other.bindings.items()} == {
        k: b.color for k, b in plot.bindings.items()
    }
    for invalid in (
        None,
        {},
        [None],
        [{**config[0], "axis": "top"}],
        [{**config[0], "visible": 1}],
        [{**config[0], "color": "red"}],
        [{**config[0], "color": 7}],
        [{**config[0], "source_id": []}],
        [{**config[0], "signal_id": []}],
        [{**config[0], "unit": 5}],
        [{**config[0], "unit": ""}],
        [config[0], config[0]],
        [config[0], {**config[1], "axis": "left"}],
        [{**config[0], "path": "C:/private/data.csv"}],
    ):
        with pytest.raises(ValueError):
            other.restore_configuration(invalid)
        assert other.configuration() == config
    other.restore_configuration([])
    assert not other.bindings and other.right_view is None


def test_missing_source_and_empty_bindings_remain_repairable(workspace):
    first, second = descriptor("lab.first"), descriptor("lab.second", "m/s")
    session = populate(workspace, [first, second])
    dock = workspace.add("plot")
    for item in (first, second):
        add_selected(workspace, dock, item.identifier)
    plot = dock.multi_plot
    assert plot is not None
    config = plot.configuration()
    workspace.update_snapshot(None)
    assert "Missing signal" in dock.feedback.text()
    assert all(c.getData() == (None, None) for c in plot.curves.values())
    workspace.update_snapshot(session.snapshot())
    assert plot.configuration() == config
    assert "Missing" not in dock.feedback.text()
    assert all(c.getData()[0] is not None for c in plot.curves.values())
    empty = TelemetrySession(session.schema)
    workspace.update_snapshot(empty.snapshot())
    assert dock.readout.text() == "No readings yet."
    assert all(c.getData() == (None, None) for c in plot.curves.values())


def test_different_time_domains_cannot_share_an_unmapped_time_axis(workspace):
    first = descriptor("lab.first")
    second = replace(descriptor("lab.second"), timestamp_domain="other")
    populate(workspace, [first, second])
    dock = workspace.add("plot")
    add_selected(workspace, dock, first.identifier)
    add_selected(workspace, dock, second.identifier)
    assert "timestamp domain" in dock.feedback.text()
    assert list(dock.multi_plot.bindings) == [first.identifier]


def test_colors_remain_distinct_after_palette_exhaustion_and_removal(workspace):
    signals = [descriptor(f"group.signal{i}") for i in range(12)]
    populate(workspace, signals)
    dock = workspace.add("plot")
    for item in signals:
        add_selected(workspace, dock, item.identifier)
    plot = dock.multi_plot
    assert plot is not None
    colors = {key: binding.color for key, binding in plot.bindings.items()}
    assert len(set(colors.values())) == 12
    plot.remove(signals[0].identifier)
    plot.add(signals[0])
    assert {key: binding.color for key, binding in plot.bindings.items()} == colors


def test_snapshot_domain_change_clears_unaligned_curves_and_invalid_restore_is_atomic(workspace):
    first, second = descriptor("lab.first"), descriptor("lab.second")
    session = populate(workspace, [first, second])
    dock = workspace.add("plot")
    for item in (first, second):
        add_selected(workspace, dock, item.identifier)
    plot = dock.multi_plot
    assert plot is not None
    original = plot.configuration()
    foreign = replace(
        session.snapshot(), domains={first.identifier: "relative", second.identifier: "other"}
    )
    workspace.update_snapshot(foreign)
    assert "timestamp domain" in dock.feedback.text()
    assert all(c.getData() == (None, None) for c in plot.curves.values())
    with pytest.raises(ValueError, match="timestamp domain"):
        plot.restore_configuration(original)
    assert plot.configuration() == original
    workspace.update_snapshot(session.snapshot())
    assert all(c.getData()[0] is not None for c in plot.curves.values())


def test_large_trace_reuses_bounded_windowing_and_keeps_extrema_after_zoom(workspace, qtbot):
    first, second = descriptor("lab.first"), descriptor("lab.second", "m/s")
    session = populate(
        workspace,
        [first, second],
        {
            first.identifier: [(i * 1_000_000, 900 if i == 1001 else 3) for i in range(20_000)],
            second.identifier: [(0, 1000), (19_999_000_000, 2000)],
        },
    )
    dock = workspace.add("plot")
    for item in (first, second):
        add_selected(workspace, dock, item.identifier)
    plot = dock.multi_plot
    assert plot is not None
    x, y = plot.curves[first.identifier].getData()
    assert len(x) <= POINT_LIMIT and 900 in y
    plot.view.setXRange(1, 1.01, padding=0)
    qtbot.waitUntil(lambda: len(plot.curves[first.identifier].getData()[0]) < 20)
    assert 900 in plot.curves[first.identifier].getData()[1]
    workspace.update_snapshot(session.snapshot())
    assert plot.view.viewRange()[0] == pytest.approx([1, 1.01])
    press(dock, "Fit traces")
    assert len(plot.curves[first.identifier].getData()[0]) <= POINT_LIMIT
    assert len(session.snapshot().series[first.identifier]) == 20_000


def test_real_simulator_multitrace_dock_uses_one_replay_clock(qtbot, qapp, monkeypatch):
    monkeypatch.setitem(pg.CONFIG_OPTIONS, "mouseRateLimit", 0)
    window = create_main_window()
    owner = window.source_owner
    assert isinstance(owner, SourceController)
    window.show()
    try:
        owner.choose("process")
        qtbot.waitUntil(lambda: owner._finished_rendered)
        dock = window.workspace_docks.add("plot")
        add_selected(window.workspace_docks, dock, "process.pressure")
        add_selected(window.workspace_docks, dock, "process.temperature")
        qtbot.waitUntil(lambda: dock.cursor is not None)
        assert dock.multi_plot is not None and len(dock.multi_plot.bindings) == 2
        window.replay_bar.slider.setValue(5000)
        assert "pressure" in dock.selected_reading.text()
        assert "temperature" in dock.selected_reading.text()
        before = owner.replay.clock.snapshot()
        assert dock.cursor is not None and owner.replay.cursor is not None
        widget = dock.plot
        assert widget is not None
        qapp.processEvents()
        view = dock.multi_plot.view
        low, high = view.viewRange()[1]
        y = low + (high - low) * 0.2
        start = widget.mapFromScene(view.mapViewToScene(QPointF(1.5, y)))
        end = widget.mapFromScene(view.mapViewToScene(QPointF(1.0, y)))
        hover = QMouseEvent(
            QEvent.Type.MouseMove,
            QPointF(start),
            QPointF(widget.viewport().mapToGlobal(start)),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
        qapp.sendEvent(widget.viewport(), hover)
        qtbot.mousePress(widget.viewport(), Qt.MouseButton.LeftButton, pos=start)
        for step in range(1, 6):
            position = start + (end - start) * step / 5
            # Offscreen Qt has no OS button capture, so supply the pressed-button state.
            event = QMouseEvent(
                QEvent.Type.MouseMove,
                QPointF(position),
                QPointF(widget.viewport().mapToGlobal(position)),
                Qt.MouseButton.NoButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
            qapp.sendEvent(widget.viewport(), event)
        qtbot.mouseRelease(widget.viewport(), Qt.MouseButton.LeftButton, pos=end)
        assert owner.replay.clock.snapshot().revision == before.revision + 1
        assert owner.replay.clock.snapshot().selected_ns == pytest.approx(
            1_000_000_000, abs=20_000_000
        )
        assert owner.replay.cursor.value() == pytest.approx(dock.cursor.value())
        press(dock, "Remove selected trace")
        owner.replay.present()
        assert len(dock.multi_plot.bindings) == 1
        assert dock.selected_reading.text().startswith("Selected:")
    finally:
        owner.disconnect()
        qtbot.waitUntil(lambda: owner.source is None and owner._preparing is None)
        window.close()
        window.deleteLater()
