"""Type-aware readings through the dashboard's value dock and replay clock."""

import pytest

from lbr_dashboard.app import create_main_window
from lbr_dashboard.core import SignalDescriptor, SignalSample, SignalSchema, TelemetrySession
from lbr_dashboard.core.value_display import ValueState
from lbr_dashboard.source_controller import SourceController


@pytest.mark.parametrize(
    ("kind", "raw", "unit", "hints", "expected"),
    [
        ("numeric", 0, "kPa", {}, "0 kPa"),
        ("numeric", 0.33333, "V", {"decimal_places": 2}, "0.33 V"),
        ("numeric", 7, "V", {"decimal_places": True}, "7 V"),
        ("boolean", False, None, {}, "false"),
        ("string", "<b>ready</b>", None, {}, '"<b>ready</b>"'),
        ("status", 0, None, {}, "[code 0]"),
        ("vector", (0, 1), "m/s", {"component_labels": ["north", "east"]}, "(north=0, east=1) m/s"),
        ("vector", (0, 1), "m/s", {"component_labels": ["north", 7]}, "(0, 1) m/s"),
        ("opaque", "sample", None, {}, "--"),
    ],
)
def test_bind_renders_the_actual_descriptor_and_sample(qtbot, kind, raw, unit, hints, expected):
    window = create_main_window()
    qtbot.addWidget(window)
    descriptor = SignalDescriptor(
        "lab.value",
        "Δ reading",
        ("lab",),
        kind,
        "bench",
        canonical_unit=unit,
        shape=(2,) if kind == "vector" else (),
        display=hints,
    )
    session = TelemetrySession(SignalSchema(1, (descriptor,)))
    sample_unit = "Pa" if unit == "kPa" else None
    session.append(
        "lab.value", SignalSample(123_000_000, raw, unit=sample_unit, quality="recorded")
    )
    workspace = window.workspace_docks
    workspace.update_snapshot(session.snapshot())
    browser = window.left_panel.signal_browser
    browser.refresh(session.schema, {"lab.value": (True, "")})
    browser.select_id("lab.value")
    dock = workspace.add("value")
    dock.bind_selected()
    card = dock.value_widget
    assert card is not None and card.display is not None
    assert card.name_label.text() == "Δ reading"
    assert card.identity_label.text() == "lab.value from bench"
    assert card.value_label.text() == ("0 Pa" if sample_unit else expected)
    assert card.display.timestamp_ns == 123_000_000
    if kind == "opaque":
        assert card.display.state is ValueState.UNSUPPORTED_TYPE
    else:
        assert card.display.state is ValueState.OK
        assert "recorded" in card.quality_label.text()
        assert card.unit_label.text() == (sample_unit or unit or "(none)")


def test_replay_does_not_show_future_readings_or_hide_unavailability(qtbot):
    window = create_main_window()
    qtbot.addWidget(window)
    owner = window.source_owner
    assert isinstance(owner, SourceController)
    reference = SignalDescriptor("lab.clock", "Clock", ("lab",), "numeric", "bench")
    reading = SignalDescriptor(
        "lab.value", "Reading", ("lab",), "numeric", "bench", nominal_rate_hz=10
    )
    session = TelemetrySession(SignalSchema(1, (reference, reading)))
    session.extend("lab.clock", (SignalSample(0, 1), SignalSample(1_000_000_000, 2)))
    session.extend(
        "lab.value",
        (
            SignalSample(100_000_000, 0),
            SignalSample(400_000_000, None, available=False, quality="gap"),
            SignalSample(900_000_000, 8),
        ),
    )
    snapshot = session.snapshot()
    workspace = window.workspace_docks
    workspace.update_snapshot(snapshot)
    dock = workspace.add("value")
    assert dock.bind("lab.value", "bench")
    card = dock.value_widget
    assert card is not None
    replay = owner.replay
    replay.update(session, snapshot=snapshot, ready=True, connected=False, busy=False)
    for millis, state, value in (
        (50, ValueState.NO_DATA, "--"),
        (100, ValueState.OK, "0"),
        (200, ValueState.OK, "0"),
        (350, ValueState.STALE, "0"),
        (400, ValueState.UNAVAILABLE, "--"),
        (890, ValueState.UNAVAILABLE, "--"),
        (900, ValueState.OK, "8"),
    ):
        window.replay_bar.slider.setValue(millis * 10)
        assert card.display is not None
        assert (card.display.state, card.value_label.text()) == (state, value)
        assert dock.value_reference.text() == "Reading at replay time"
    replay.update(None, ready=False, connected=False, busy=False)
    assert dock.value_reference.text() == "Latest reading"
    workspace.update_snapshot(None)
    assert card.display is None and "Missing signal" in dock.feedback.text()
