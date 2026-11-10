"""Production transport bindings driven by an injected monotonic clock."""

from dataclasses import replace
from fractions import Fraction

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QLineEdit, QPlainTextEdit, QSpinBox, QTextEdit

from lbr_dashboard.app import create_main_window
from lbr_dashboard.core import (
    PlaybackMode,
    SignalDescriptor,
    SignalSample,
    SignalSchema,
    TelemetrySession,
)
from lbr_dashboard.source_controller import SourceController
from lbr_dashboard.ui.replay_bar import SLIDER_STEPS


@pytest.fixture
def transport(qtbot):
    window = create_main_window()
    owner = window.source_owner
    assert isinstance(owner, SourceController)
    now = [0]
    owner.replay._now = lambda: now[0]
    window.show()
    yield window, owner, now
    owner.disconnect()
    qtbot.waitUntil(lambda: owner.source is None and owner._preparing is None)
    window.close()
    window.deleteLater()


def finite(transport, qtbot):
    window, owner, now = transport
    owner.choose("process")
    qtbot.waitUntil(lambda: owner._finished_rendered and window.replay_bar.play.isEnabled())
    return window, owner, now


def test_empty_and_loading_controls_are_disabled(transport):
    window, owner, _ = transport
    owner.poll()
    bar = window.replay_bar
    assert "No replay data" in bar.time.text()
    for control in (bar.play, bar.slider, bar.speed, bar.live):
        assert not control.isEnabled()
    owner.replay.toggle()
    owner.replay.seek_slider(5000)
    owner.replay.speed(2)
    owner.replay.follow()
    owner.replay.pause()
    assert owner.replay.clock.snapshot().selected_ns is None
    owner.choose("process")
    assert not bar.play.isEnabled()


def test_play_pause_speed_end_and_replay_match_shared_clock(transport, qtbot):
    window, owner, now = finite(transport, qtbot)
    replay, bar = owner.replay, window.replay_bar
    original = owner.session.snapshot()
    assert replay.clock.snapshot().selected_ns == 0
    qtbot.mouseClick(bar.play, Qt.MouseButton.LeftButton)
    now[0] = 500_000_000
    owner.poll()
    assert replay.clock.snapshot().selected_ns == 500_000_000
    assert "0.500000000 s" in bar.time.text()
    assert replay.cursor.value() == 0.5
    bar.speed.setCurrentIndex(bar.speed.findData(2))
    assert replay.clock.snapshot().speed == Fraction(2)
    now[0] += 250_000_000
    owner.poll()
    assert replay.clock.snapshot().selected_ns == 1_000_000_000
    qtbot.mouseClick(bar.play, Qt.MouseButton.LeftButton)
    now[0] += 10_000_000_000
    owner.poll()
    assert replay.clock.snapshot().selected_ns == 1_000_000_000
    assert replay.clock.snapshot().mode is PlaybackMode.PAUSED
    qtbot.mouseClick(bar.play, Qt.MouseButton.LeftButton)
    now[0] += 5_000_000_000
    owner.poll()
    assert replay.clock.snapshot().selected_ns == 3_000_000_000
    assert replay.clock.snapshot().mode is PlaybackMode.ENDED
    assert bar.play.text() == "Replay" and bar.slider.value() == SLIDER_STEPS
    qtbot.mouseClick(bar.play, Qt.MouseButton.LeftButton)
    assert replay.clock.snapshot().selected_ns == 0
    assert replay.clock.snapshot().mode is PlaybackMode.PLAYING
    assert owner.session.snapshot() == original


def test_slider_seeks_pause_without_calling_source_seek(transport, qtbot, monkeypatch):
    window, owner, _ = finite(transport, qtbot)

    def forbidden(*args):
        pytest.fail("cached replay must not call unsupported source.seek")

    monkeypatch.setattr(owner.source, "seek", forbidden)
    bar, clock = window.replay_bar, owner.replay.clock
    bar.play.click()
    bar.slider.sliderPressed.emit()
    assert clock.snapshot().mode is PlaybackMode.PAUSED
    bar.slider.setValue(2500)
    assert clock.snapshot().selected_ns == 750_000_000, "Seek slider did not update SessionClock"
    assert owner.replay.cursor.value() == 0.75
    assert clock.snapshot().mode is PlaybackMode.PAUSED
    bar.slider.setValue(SLIDER_STEPS)
    assert clock.snapshot().selected_ns == 3_000_000_000
    bar.slider.setValue(0)
    assert clock.snapshot().selected_ns == 0


def test_keyboard_shortcuts_and_clamping(transport, qtbot):
    window, owner, _ = finite(transport, qtbot)
    window.activateWindow()
    window.setFocus()
    qtbot.keyClick(window, Qt.Key.Key_Space)
    assert owner.replay.clock.snapshot().mode is PlaybackMode.PLAYING
    qtbot.keyClick(window, Qt.Key.Key_Space)
    assert owner.replay.clock.snapshot().mode is PlaybackMode.PAUSED
    qtbot.keyClick(window, Qt.Key.Key_Right)
    assert owner.replay.clock.snapshot().selected_ns == 1_000_000_000
    qtbot.keyClick(window, Qt.Key.Key_End)
    assert owner.replay.clock.snapshot().selected_ns == 3_000_000_000
    qtbot.keyClick(window, Qt.Key.Key_Right)
    assert owner.replay.clock.snapshot().selected_ns == 3_000_000_000
    qtbot.keyClick(window, Qt.Key.Key_Home)
    qtbot.keyClick(window, Qt.Key.Key_Left)
    assert owner.replay.clock.snapshot().selected_ns == 0
    qtbot.keyClick(window, Qt.Key.Key_Right, Qt.KeyboardModifier.ControlModifier)
    qtbot.keyClick(window, Qt.Key.Key_A)
    assert owner.replay.clock.snapshot().selected_ns == 0


@pytest.mark.parametrize(
    "kind", ["search", "line", "text", "plain", "spin", "combo", "button", "tree"]
)
def test_shortcuts_do_not_steal_editing_or_widget_navigation(transport, qtbot, kind):
    window, owner, _ = finite(transport, qtbot)
    if kind == "search":
        widget = window.left_panel.signal_browser.search
    elif kind == "tree":
        widget = window.left_panel.signal_browser.tree
    elif kind == "button":
        widget = window.source_picker.connect_button
    else:
        widget = {
            "line": QLineEdit,
            "text": QTextEdit,
            "plain": QPlainTextEdit,
            "spin": QSpinBox,
            "combo": QComboBox,
        }[kind](window)
        if isinstance(widget, QComboBox):
            widget.setEditable(True)
        widget.show()
    window.activateWindow()
    widget.setFocus()
    before = owner.replay.clock.snapshot()
    # Exercise the filter directly as well as actual text entry.
    if kind not in {"button", "tree", "spin", "combo"}:
        qtbot.keyClicks(widget, "hello world")
        assert isinstance(widget, (QLineEdit, QTextEdit, QPlainTextEdit))
        text = widget.text() if isinstance(widget, QLineEdit) else widget.toPlainText()
        assert "hello world" in text
    qtbot.keyClick(widget, Qt.Key.Key_Left)
    qtbot.keyClick(widget, Qt.Key.Key_Home)
    assert owner.replay.clock.snapshot() == before


def test_large_exact_timestamp_slider_and_single_sample(transport):
    window, owner, _ = transport
    descriptor = SignalDescriptor(
        "strange.signal", "Signal", ("custom",), "numeric", "Source-X", timestamp_domain="relative"
    )
    schema = SignalSchema(1, (descriptor,))
    session = TelemetrySession(schema)
    start = 2**60 + 13
    session.extend(
        descriptor.identifier,
        (
            SignalSample(start, 0, timestamp_domain="relative"),
            SignalSample(start + 10000, 7, timestamp_domain="relative"),
        ),
    )
    replay = owner.replay
    replay.update(
        session, snapshot=session.snapshot(), ready=True, connected=False, busy=False, live=False
    )
    replay.seek_slider(1234)
    assert replay.clock.snapshot().selected_ns == start + 1234
    assert str(start + 1234) in window.replay_bar.time.text()
    single = TelemetrySession(schema)
    single.append(descriptor.identifier, SignalSample(-7, 0, timestamp_domain="relative"))
    replay.update(
        single, snapshot=single.snapshot(), ready=True, connected=False, busy=False, live=False
    )
    assert replay.clock.snapshot().selected_ns == -7
    assert not window.replay_bar.play.isEnabled() and not window.replay_bar.slider.isEnabled()


def test_live_pause_follow_disconnect_and_finite_switch(transport, qtbot):
    window, owner, now = transport
    owner.choose("live")
    qtbot.waitUntil(lambda: owner.replay.clock.snapshot().selected_ns is not None)
    bar, replay = window.replay_bar, owner.replay
    assert replay.clock.snapshot().mode is PlaybackMode.LIVE
    assert bar.live.isEnabled()
    assert not bar.slider.isEnabled() and not bar.speed.isEnabled()
    bar.play.click()
    frozen = replay.clock.snapshot().selected_ns
    # Drive ingestion, not the playback clock, with deterministic samples in this live session.
    descriptor = owner.session.schema.signals[0]
    owner.session.append(
        descriptor.identifier, SignalSample(9_000_000_000, 7, timestamp_domain="relative")
    )
    view = owner.session.snapshot()
    replay.update(owner.session, snapshot=view, ready=True, live=True, connected=True, busy=False)
    assert replay.clock.snapshot().selected_ns == frozen
    bar.live.click()
    assert replay.clock.snapshot().selected_ns == 9_000_000_000
    owner.disconnect()
    qtbot.waitUntil(lambda: owner.source is None)
    assert not bar.live.isEnabled() and not bar.play.isEnabled()
    assert "Disconnected live input" in bar.time.text()
    owner.choose("motion")
    qtbot.waitUntil(lambda: owner._finished_rendered and bar.slider.isEnabled())
    assert replay.clock.snapshot().mode is PlaybackMode.PAUSED
    assert replay.clock.snapshot().selected_ns == 0
    assert not bar.live.isEnabled()


def test_selection_does_not_replace_clock_and_foreign_domains_do_not_share_cursor(transport, qtbot):
    window, owner, _ = finite(transport, qtbot)
    replay = owner.replay
    original = replay.clock
    replay.seek_slider(5000)
    owner.select_signal("process.temperature")
    assert replay.clock is original and replay.cursor.value() == 1.5
    # A reference timeline never implies synchronization of a different source/domain.
    snapshot = owner.session.snapshot()
    foreign = tuple(
        replace(d, timestamp_domain="device_monotonic")
        if d.identifier == "process.temperature"
        else d
        for d in snapshot.schema.signals
    )
    replay._snapshot = replace(
        snapshot,
        schema=SignalSchema(1, foreign),
        domains={**snapshot.domains, "process.temperature": "device_monotonic"},
    )
    replay.present()
    assert not replay.cursor.isVisible()


def test_controls_disabled_while_preparing_and_legacy_load_resets_clock(transport, qtbot, tmp_path):
    window, owner, _ = finite(transport, qtbot)
    window.replay_bar.play.click()
    path = tmp_path / "missing.csv"
    owner.open_csv(path)
    assert not window.replay_bar.play.isEnabled()
    owner.poll()
    qtbot.waitUntil(lambda: owner._preparing is None)
    path.write_text("timestamp_ms,a\n0,1\n", encoding="utf-8")
    window.load_csv(path)
    qtbot.waitUntil(lambda: owner.source is None)
    assert owner.replay.clock.snapshot().selected_ns is None
    assert not window.replay_bar.play.isEnabled()


def test_broken_seek_binding_is_caught_by_ordinary_workflow(transport, qtbot, monkeypatch):
    window, _, _ = finite(transport, qtbot)
    window.replay_bar.seek_requested.disconnect()
    with pytest.raises(AssertionError, match="Seek slider did not update SessionClock"):
        test_slider_seeks_pause_without_calling_source_seek(transport, qtbot, monkeypatch)


def test_play_after_seeking_to_end_restarts_immediately(transport, qtbot):
    window, owner, _ = finite(transport, qtbot)
    window.replay_bar.slider.setValue(SLIDER_STEPS)
    assert owner.replay.clock.snapshot().mode is PlaybackMode.PAUSED
    window.replay_bar.play.click()
    assert owner.replay.clock.snapshot().mode is PlaybackMode.PLAYING
    assert owner.replay.clock.snapshot().selected_ns == 0


def test_cursor_uses_resolved_domain_and_is_hidden_for_structured_signals(transport, qtbot):
    window, owner, _ = finite(transport, qtbot)
    replay = owner.replay
    snapshot = owner.session.snapshot()
    unspecified = tuple(replace(d, timestamp_domain="unspecified") for d in snapshot.schema.signals)
    replay._snapshot = replace(snapshot, schema=SignalSchema(1, unspecified))
    replay.present()
    assert replay.cursor.isVisible()  # Actual samples establish the relative domain.
    owner.select_signal("process.status")
    assert replay.cursor is None or not replay.cursor.isVisible()
