"""The Coordinate frames tool in the real app with the fictional attitude simulator."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFileDialog

from lbr_dashboard.app import create_main_window
from lbr_dashboard.core.export import signal_frame
from lbr_dashboard.core.frames import is_transformed
from lbr_dashboard.source_controller import SourceController
from lbr_dashboard.ui.signal_browser import ID_ROLE
from lbr_dashboard.ui.tools.frames import FramesTool

DEMO = Path(__file__).resolve().parents[2] / "sample_data" / "timestamped_demo.csv"
ACCEL = "attitude.acceleration"
OUTPUT = "attitude.acceleration.vehicle"
UNFRAMED = "attitude.rate"
MS = 1_000_000


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


def open_frames(window) -> FramesTool:
    dock = window.analysis_docks.open("frames")
    tool = dock.widget()
    assert isinstance(tool, FramesTool)
    tool.refresh()
    return tool


def browser_ids(window) -> set[str]:
    return set(window.left_panel.signal_browser._descriptors)


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


def seek(owner, time_ns: int) -> None:
    owner.replay.clock.seek(time_ns)
    owner.replay.present()


def test_menu_lists_the_tool_and_preview_explains_each_vector(attitude) -> None:
    window, owner = attitude
    labels = [action.text() for action in window.analysis_docks.menu.actions()]
    assert "Coordinate frames" in labels and "Orientation" in labels
    tool = open_frames(window)
    assert not tool.apply_button.isEnabled()
    assert "Enter a name" in tool.preview.toPlainText()
    assert [tool.source.itemText(i) for i in range(tool.source.count())] == ["sensor"]

    seek(owner, 50 * MS)
    tool.set_draft("vehicle", "sensor", "vehicle", "euler", "0, 0, 90")
    assert tool.apply_button.isEnabled()
    text = tool.preview.toPlainText()
    assert f"{ACCEL} -> {OUTPUT}" in text
    # Sample 1 at 50 ms is (1, 0, 9.81) m/s^2 in the sensor frame.
    assert "at cursor: (1, 0, 9.81) m/s^2 -> (0, 1, 9.81) m/s^2" in text
    assert f"{UNFRAMED} declares no coordinate frame" in text
    assert "attitude.quaternion is vector (4); only three-component vectors" in text
    assert "[ 0.0000, -1.0000,  0.0000]" in text


def test_applied_transform_adds_a_rotated_signal_and_keeps_raw_values(attitude) -> None:
    window, owner = attitude
    raw_before = owner.session.snapshot().series[ACCEL]
    tool = open_frames(window)
    tool.set_draft("vehicle", "sensor", "vehicle", "euler", "0, 0, 90")
    tool.apply_button.click()

    assert OUTPUT in browser_ids(window)
    assert f"{UNFRAMED}.vehicle" not in browser_ids(window)
    assert "attitude.quaternion.vehicle" not in browser_ids(window)
    context = window.analysis
    descriptor = context.descriptor(OUTPUT)
    assert descriptor is not None and is_transformed(descriptor)
    assert signal_frame(descriptor) == "vehicle"
    assert descriptor.path[0] == "transformed"
    snapshot = context.snapshot
    assert snapshot is not None
    raw = snapshot.series[ACCEL]
    assert raw == raw_before
    assert [record.sample.value for record in raw[:3]] == [
        (0.0, 0.0, 9.81),
        (1.0, 0.0, 9.81),
        (0.0, 1.0, 9.81),
    ]
    rotated = snapshot.series[OUTPUT]
    assert [record.sample.timestamp_ns for record in rotated] == [
        record.sample.timestamp_ns for record in raw
    ]
    assert np.allclose(rotated[1].sample.value, (0.0, 1.0, 9.81), atol=1e-12)
    assert np.allclose(rotated[2].sample.value, (-1.0, 0.0, 9.81), atol=1e-12)
    assert "1 transformed signal(s)" in tool.hint.text()
    assert tool.applied.count() == 1 and "sensor -> vehicle" in tool.applied.item(0).text()
    assert tool.apply_button.text() == "Replace transform"

    # Selecting the result shows its provenance and rotated readings.
    select(window, OUTPUT)
    details = window.left_panel.signal_browser.details.text()
    assert f"Transformed: {ACCEL} rotated from frame 'sensor' to 'vehicle'" in details
    table = window.right_panel.table
    assert table.rowCount() > 0
    column = [table.horizontalHeaderItem(i).text() for i in range(table.columnCount())]
    value = table.item(table.rowCount() - 1, column.index("Value"))
    assert value is not None and value.text().startswith("(")

    # Removing the transform removes its signal again; raw signals stay.
    tool.applied.setCurrentRow(0)
    tool.remove_button.click()
    assert OUTPUT not in browser_ids(window)
    assert ACCEL in browser_ids(window)
    assert context.snapshot is not None and context.snapshot.series[ACCEL] == raw_before


def test_mismatched_frame_and_invalid_rotation_are_refused(attitude) -> None:
    window, _owner = attitude
    tool = open_frames(window)
    tool.set_draft("level", "chassis", "vehicle", "euler", "0, 0, 90")
    text = tool.preview.toPlainText()
    assert "No vector signal in this session declares frame 'chassis'" in text
    assert f"{ACCEL} is in frame 'sensor', not 'chassis'" in text
    before = browser_ids(window)
    tool.apply_button.click()
    assert browser_ids(window) == before

    tool.set_draft("bad", "sensor", "vehicle", "quaternion", "1, 1, 0, 0")
    assert not tool.apply_button.isEnabled()
    assert "norm" in tool.preview.toPlainText()
    tool.set_draft("bad", "sensor", "vehicle", "matrix", "1,0,0; 0,1,0; 0,0,-1")
    assert not tool.apply_button.isEnabled()
    assert "reflection" in tool.preview.toPlainText()
    tool.set_draft("bad", "sensor", "sensor", "euler", "0, 0, 0")
    assert "must differ" in tool.preview.toPlainText()


def test_configuration_survives_a_restart(qtbot) -> None:
    window, _owner = start(qtbot)
    tool = open_frames(window)
    tool.set_draft("vehicle", "sensor", "vehicle", "matrix", "0,-1,0; 1,0,0; 0,0,1")
    tool.apply_button.click()
    saved = json.dumps(tool.configuration())
    stop(qtbot, window)

    window, owner = start(qtbot)
    try:
        tool = open_frames(window)
        assert OUTPUT not in browser_ids(window)
        tool.restore_configuration(json.loads(saved))
        assert OUTPUT in browser_ids(window)
        snapshot = window.analysis.snapshot
        assert snapshot is not None
        assert np.allclose(snapshot.series[OUTPUT][1].sample.value, (0.0, 1.0, 9.81))
        with pytest.raises(ValueError):
            tool.restore_configuration({"version": 1, "transforms": [{"name": "x"}]})
        assert [item.name for item in window.analysis.frames.transforms] == ["vehicle"]
        # Selecting the applied transform loads it back into the editor.
        tool.applied.setCurrentRow(0)
        assert tool.name.text() == "vehicle" and tool.form.currentData() == "quaternion"
        assert tool.draft is not None
        assert tool.draft.rotation.is_close(window.analysis.frames.transforms[0].rotation)
        assert owner.session is not None
    finally:
        stop(qtbot, window)


def test_sessions_without_framed_vectors_are_left_alone(qtbot, monkeypatch) -> None:
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(DEMO), ""))
    window, _owner = start(qtbot, "csv")
    try:
        tool = open_frames(window)
        before = browser_ids(window)
        tool.set_draft("vehicle", "sensor", "vehicle", "euler", "0, 0, 90")
        tool.apply_button.click()
        assert browser_ids(window) == before
        text = tool.preview.toPlainText()
        assert "No vector signal in this session declares frame 'sensor'" in text
        assert "are not vectors and are never transformed" in text
        assert "0 transformed signal(s)" in tool.hint.text()
    finally:
        stop(qtbot, window)
