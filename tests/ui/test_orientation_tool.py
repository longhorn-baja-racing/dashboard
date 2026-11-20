"""The Orientation tool in the real app and over synthetic quaternion sessions."""

from __future__ import annotations

import math
import time
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFileDialog

from lbr_dashboard.app import create_main_window
from lbr_dashboard.core import SignalDescriptor, SignalSample, SignalSchema, TelemetrySession
from lbr_dashboard.core.frames import Rotation
from lbr_dashboard.source_controller import SourceController
from lbr_dashboard.ui.analysis import AnalysisContext
from lbr_dashboard.ui.attitude_view import AttitudeView
from lbr_dashboard.ui.signal_browser import ID_ROLE
from lbr_dashboard.ui.tools.orientation import OrientationTool

DEMO = Path(__file__).resolve().parents[2] / "sample_data" / "timestamped_demo.csv"
QUATERNION = "attitude.quaternion"
MS = 1_000_000
HALF = math.sqrt(0.5)


def start(qtbot, key: str = "attitude"):
    window = create_main_window()
    window.show()
    owner = window.source_owner
    assert isinstance(owner, SourceController)
    now = [0]
    owner.replay._now = lambda: now[0]
    picker = window.source_picker
    picker.choices.setCurrentIndex(picker.choices.findData(key))
    picker.connect_button.click()
    qtbot.waitUntil(lambda: owner._finished_rendered and window.replay_bar.slider.isEnabled())
    return window, owner


def stop(qtbot, window) -> None:
    window.close()
    qtbot.waitUntil(lambda: not window.isVisible())
    window.deleteLater()


@pytest.fixture
def attitude(qtbot):
    window, owner = start(qtbot)
    yield window, owner
    stop(qtbot, window)


def open_orientation(window) -> OrientationTool:
    dock = window.analysis_docks.open("orientation")
    tool = dock.widget()
    assert isinstance(tool, OrientationTool)
    tool.refresh()
    return tool


def seek(owner, tool: OrientationTool, time_ns: int) -> None:
    owner.replay.clock.seek(time_ns)
    owner.replay.present()
    assert tool.context.cursor_ns == time_ns
    tool.refresh()


def select(window, signal_id: str) -> None:
    browser = window.left_panel.signal_browser
    browser.search.setText(signal_id)
    model = browser.tree.model()
    assert model is not None
    matches = model.match(
        model.index(0, 0),
        ID_ROLE,
        signal_id,
        2,
        Qt.MatchFlag.MatchExactly | Qt.MatchFlag.MatchRecursive,
    )
    assert len(matches) == 1
    browser.tree.setCurrentIndex(matches[0])


def same_point(a, b) -> bool:
    return math.isclose(a[0], b[0], abs_tol=1e-9) and math.isclose(a[1], b[1], abs_tol=1e-9)


def test_simulated_attitude_follows_the_replay_cursor(attitude, qtbot) -> None:
    window, owner = attitude
    tool = open_orientation(window)
    assert tool.signal_id == QUATERNION
    metadata = tool.metadata.text()
    assert "Body frame: sensor" in metadata and "Reference frame: level" in metadata
    assert "wxyz, hamilton convention" in metadata
    assert "Yaw reference: relative to start (fictional, drifts)" in metadata
    assert "stale after 150 ms" in metadata

    # Sample 10 (500 ms) is a quarter turn of yaw with 10 deg roll.
    seek(owner, tool, 500 * MS)
    assert tool.reading is not None and tool.reading.is_current
    assert "Roll 10.0 deg, pitch 0.0 deg, yaw 90.0 deg" in tool.angles.text()
    view = tool.view
    assert view.pose_style == "valid" and view.caption == ""
    body = view.body_axes()
    assert body is not None
    # Body x points along reference y after a 90 deg yaw.
    assert same_point(body[0], AttitudeView.reference_axes()[1])

    # 1.55 s is inside the unavailable window: missing, never the previous pose.
    seek(owner, tool, 1_550 * MS)
    assert "Missing" in tool.angles.text() and "simulated_unavailable" in tool.angles.text()
    assert view.rotation is None and view.pose_style == "none"
    assert "Missing" in view.caption

    # 2.4 s is 200 ms after the last reading before the stale window.
    seek(owner, tool, 2_400 * MS)
    assert tool.angles.text().startswith("Stale: no reading for 200 ms (limit 150 ms)")
    assert view.rotation is None and "Stale" in view.caption
    assert "deg" not in tool.angles.text().split("\n")[0]

    # Seeking backwards shows the earlier pose again.
    seek(owner, tool, 500 * MS)
    assert "yaw 90.0 deg" in tool.angles.text() and view.pose_style == "valid"

    # Cursor moves are redrawn by the coalescing timer, not synchronously.
    owner.replay.clock.seek(0)
    owner.replay.present()
    qtbot.waitUntil(lambda: "yaw 0.0 deg" in tool.angles.text())
    window.repaint()
    tool.view.grab()


def test_non_quaternion_selection_is_refused_with_a_reason(attitude) -> None:
    window, _owner = attitude
    tool = open_orientation(window)
    # Selecting a 3-vector in the browser does not replace the quaternion shown.
    select(window, "attitude.acceleration")
    tool.refresh()
    assert tool.signal_id == QUATERNION
    tool.choice.setCurrentIndex(tool.choice.findData("attitude.acceleration"))
    assert tool.signal_id == "attitude.acceleration"
    assert "an orientation needs a four-component vector" in tool.hint.text()
    assert tool.view.rotation is None and tool.metadata.text() == ""
    tool.choice.setCurrentIndex(tool.choice.findData(QUATERNION))
    assert tool.reading is not None


def test_configuration_round_trip_and_validation(attitude) -> None:
    window, owner = attitude
    tool = open_orientation(window)
    tool.restore_configuration({"signal_id": QUATERNION, "max_age_ns": 500 * MS})
    assert tool.configuration() == {"signal_id": QUATERNION, "max_age_ns": 500 * MS}
    seek(owner, tool, 2_400 * MS)
    assert tool.reading is not None and tool.reading.is_current
    for bad in (
        [],
        {"signal_id": ""},
        {"max_age_ns": -1},
        {"max_age_ns": True},
        {"other": 1},
    ):
        with pytest.raises(ValueError):
            tool.restore_configuration(bad)
    assert tool.configuration() == {"signal_id": QUATERNION, "max_age_ns": 500 * MS}
    tool.max_age.setValue(0)
    assert tool.configuration()["max_age_ns"] is None
    tool.restore_configuration({"signal_id": "gone.signal", "max_age_ns": None})
    assert "gone.signal is not in this session" in tool.hint.text()
    assert tool.choice.currentText() == "gone.signal (not in this session)"


def test_sessions_without_a_declared_quaternion_show_why(qtbot, monkeypatch) -> None:
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(DEMO), ""))
    window, _owner = start(qtbot, "csv")
    try:
        tool = open_orientation(window)
        assert tool.signal_id is None
        assert "No signal in this session declares a quaternion" in tool.hint.text()
        assert tool.view.rotation is None
    finally:
        stop(qtbot, window)


def quaternion_descriptor(order: str = "wxyz", **extra) -> SignalDescriptor:
    return SignalDescriptor(
        "bench.attitude",
        "Attitude",
        ("bench",),
        "vector",
        "bench",
        shape=(4,),
        nominal_rate_hz=100,
        timestamp_domain="relative",
        extra={"quaternion_order": order, "quaternion_convention": "hamilton", **extra},
    )


def synthetic(qtbot, samples, order: str = "wxyz", **extra):
    descriptor = quaternion_descriptor(order, **extra)
    session = TelemetrySession(SignalSchema(1, (descriptor,)))
    for start in range(0, len(samples), 5000):
        session.extend(descriptor.identifier, samples[start : start + 5000])
    context = AnalysisContext()
    context.set_snapshot(session, session.snapshot())
    tool = OrientationTool(context)
    qtbot.addWidget(tool)
    tool.resize(480, 480)
    tool.show()
    return context, tool


def reading(t_ms: int, value, quality: str | None = None) -> SignalSample:
    return SignalSample(
        t_ms * MS,
        value,
        available=value is not None,
        quality=quality,
        timestamp_domain="relative",
    )


def test_order_signs_invalid_norm_and_calibration_loss(qtbot) -> None:
    context, tool = synthetic(
        qtbot,
        [
            reading(0, (HALF, 0.0, 0.0, HALF)),  # x y z w: roll 90
            reading(10, (-HALF, 0.0, 0.0, -HALF)),  # the same rotation, negated
            reading(20, (1.0, 1.0, 0.0, 0.0)),  # norm 1.41
            reading(30, (0.0, 0.0, 0.0, 1.0), "uncalibrated"),
        ],
        order="xyzw",
        yaw_reference="magnetic north",
    )
    assert tool.signal_id == "bench.attitude"
    assert "Yaw reference: magnetic north" in tool.metadata.text()
    assert "Reference frame: not declared" in tool.metadata.text()
    texts = []
    for t in (0, 10):
        context.set_cursor(t * MS)
        tool.refresh()
        texts.append(tool.angles.text().split("\n")[1])
        assert tool.view.pose_style == "valid"
    assert texts[0] == texts[1] == "Roll 90.0 deg, pitch 0.0 deg, yaw 0.0 deg"
    context.set_cursor(20 * MS)
    tool.refresh()
    assert tool.angles.text().startswith("Invalid: quaternion norm is 1.414, not 1")
    assert tool.view.rotation is None and "Invalid" in tool.view.caption
    context.set_cursor(30 * MS)
    tool.refresh()
    assert tool.angles.text().startswith("Uncalibrated")
    assert "NOT a valid current pose" in tool.angles.text()
    assert tool.view.pose_style == "uncalibrated" and "Uncalibrated" in tool.view.caption
    tool.view.grab()


def test_known_rotations_draw_known_axes(qtbot) -> None:
    view = AttitudeView()
    qtbot.addWidget(view)
    reference = AttitudeView.reference_axes()
    view.show_attitude(Rotation.identity(), "valid", "")
    body = view.body_axes()
    assert body is not None and all(map(same_point, body, reference))
    view.show_attitude(Rotation.from_axis_angle((1, 0, 0), 90), "valid", "")
    body = view.body_axes()
    assert body is not None
    # Roll 90: body y lies on reference z, body z on reference -y.
    assert same_point(body[0], reference[0]) and same_point(body[1], reference[2])
    assert same_point(body[2], (-reference[1][0], -reference[1][1]))
    view.show_attitude(Rotation.identity(), "none", "Stale")
    assert view.body_axes() is None and view.pose_style == "none"
    with pytest.raises(ValueError):
        view.show_attitude(None, "bright", "")


def test_cursor_updates_stay_within_budget(qtbot) -> None:
    samples = [
        reading(t, Rotation.from_axis_angle((0, 0, 1), t % 360).quaternion)
        for t in range(0, 50_000)
    ]
    context, tool = synthetic(qtbot, samples)
    started = time.perf_counter()
    for step in range(300):
        context.set_cursor(step * 157 * MS)
        tool.refresh()
        tool.view.repaint()
    elapsed = time.perf_counter() - started
    # 300 redraws over a 50 000 sample session: well under 10 ms each.
    assert elapsed < 3.0, elapsed
