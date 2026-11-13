"""Offscreen checks for the single-signal value readout."""

import gc
import weakref

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QWidget

from lbr_dashboard.core.value_display import (
    StaticDescriptor,
    StaticSample,
    ValueState,
    describe_value,
)
from lbr_dashboard.ui.value_widget import NO_UNIT_TEXT, SignalValueWidget

S = 1_000_000_000
PRESSURE = StaticDescriptor("front.pressure", "Front pressure", "csv-replay", "numeric", "kPa")
BRAKE = StaticDescriptor("brake.switch", "Brake switch", "live-adapter", "boolean")


def make_widget(qtbot, signal_id: str = "front.pressure") -> SignalValueWidget:
    widget = SignalValueWidget(signal_id)
    qtbot.addWidget(widget)
    return widget


def test_starts_with_no_data_for_the_assigned_signal(qtbot) -> None:
    widget = make_widget(qtbot)
    assert widget.signal_id == "front.pressure"
    assert widget.display is None
    assert widget.name_label.text() == "front.pressure"
    assert widget.state_label.text() == "[NO DATA]"
    assert widget.value_label.text() == "--"
    assert widget.property("valueState") == "no_data"


def test_update_shows_every_field(qtbot) -> None:
    widget = make_widget(qtbot)
    display = describe_value(PRESSURE, StaticSample(S, 101.5), now_ns=2 * S, max_age_ns=5 * S)
    assert widget.show_display(display)
    assert widget.display is display
    assert widget.name_label.text() == "Front pressure"
    assert widget.identity_label.text() == "front.pressure from csv-replay"
    assert widget.type_label.text() == "numeric"
    assert widget.value_label.text() == "101.5 kPa"
    assert widget.unit_label.text() == "kPa"
    assert widget.time_label.text() == "1.000 s (age 1.000 s)"
    assert widget.quality_label.text() == "ok"
    assert widget.state_label.text() == "[OK]"
    assert not widget.value_label.font().italic()
    assert widget.property("valueState") == "ok"
    assert "101.5 kPa" in widget.accessibleName()


@pytest.mark.parametrize(
    ("sample", "state_text", "value_text"),
    [
        pytest.param(None, "[NO DATA]", "--", id="no-data"),
        pytest.param(StaticSample(S, None, available=False), "[UNAVAILABLE]", "--", id="unavail"),
        pytest.param(StaticSample(S, float("nan")), "[INVALID]", "--", id="invalid"),
        pytest.param(StaticSample(0, 4.0), "[STALE]", "4 kPa", id="stale"),
    ],
)
def test_states_are_spelled_out_not_only_colored(qtbot, sample, state_text, value_text) -> None:
    widget = make_widget(qtbot)
    widget.show_display(describe_value(PRESSURE, sample, now_ns=10 * S, max_age_ns=S))
    assert widget.state_label.text() == state_text
    assert widget.value_label.text() == value_text
    assert widget.value_label.font().italic()


def test_false_and_missing_unit_are_explicit(qtbot) -> None:
    widget = make_widget(qtbot, "brake.switch")
    widget.show_display(describe_value(BRAKE, StaticSample(S, False)))
    assert widget.value_label.text() == "false"
    assert widget.unit_label.text() == NO_UNIT_TEXT
    assert widget.state_label.text() == "[OK]"


def test_records_for_other_signals_are_ignored(qtbot) -> None:
    widget = make_widget(qtbot)
    other = describe_value(BRAKE, StaticSample(S, True))
    assert not widget.show_display(other)
    assert widget.display is None
    assert widget.value_label.text() == "--"


def test_reassigning_the_signal_id_clears_the_readout(qtbot) -> None:
    widget = make_widget(qtbot)
    widget.show_display(describe_value(PRESSURE, StaticSample(S, 3.0)))
    widget.set_signal_id("brake.switch")
    assert widget.signal_id == "brake.switch"
    assert widget.display is None
    assert widget.name_label.text() == "brake.switch"
    assert widget.value_label.text() == "--"
    assert not widget.show_display(describe_value(PRESSURE, StaticSample(S, 3.0)))
    assert widget.show_display(describe_value(BRAKE, StaticSample(S, True)))
    assert widget.display is not None
    assert widget.display.state is ValueState.OK


def test_repeated_updates_and_disposal(qtbot) -> None:
    parent = QWidget()
    qtbot.addWidget(parent)
    widget = SignalValueWidget("front.pressure", parent)
    for step in range(50):
        widget.show_display(describe_value(PRESSURE, StaticSample(step * S, float(step))))
    assert widget.value_label.text() == "49 kPa"
    assert widget.time_label.text() == "49.000 s"

    ref = weakref.ref(widget)
    with qtbot.waitSignal(widget.destroyed, timeout=1000):
        widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    del widget
    gc.collect()
    assert ref() is None
    assert parent.findChildren(SignalValueWidget) == []
