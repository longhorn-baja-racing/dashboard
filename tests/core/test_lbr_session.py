"""Qt-free lbr session model helpers."""

from __future__ import annotations

import numpy as np
import pytest

from lbr_dashboard.core.lbr_session import (
    NODE_CLOCK,
    Channel,
    ChannelKey,
    LbrSession,
    SessionEvent,
    display_unit,
)


def channel(
    values: list[float],
    *,
    available: list[bool] | None = None,
    kind: str = "sample",
    statuses: tuple[str, ...] = ("ok",),
    status: list[int] | None = None,
    clock: str = "shared",
) -> Channel:
    count = len(values)
    return Channel(
        key=ChannelKey(kind, "imu", "X", clock),
        unit="m/s^2",
        times_us=np.arange(count, dtype=np.int64) * 1000,
        values=np.array(values, dtype=np.float64),
        available=np.array(available if available is not None else [True] * count),
        status=np.array(status if status is not None else [0] * count, dtype=np.uint16),
        statuses=statuses,
        seq=np.arange(count, dtype=np.int64),
    )


@pytest.mark.parametrize(
    ("unit", "expected"),
    [
        ("deg*1e-7", ("deg", 1e-7)),
        ("m * 1e3", ("m", 1e3)),
        ("count*1e+2", ("count", 100.0)),
        ("m/s^2", ("m/s^2", 1.0)),
        ("", ("", 1.0)),
        ("*1e-7", ("*1e-7", 1.0)),
    ],
)
def test_display_unit(unit: str, expected: tuple[str, float]) -> None:
    base, factor = display_unit(unit)
    assert base == expected[0] and factor == pytest.approx(expected[1])


def test_channel_key_ids_and_labels() -> None:
    assert ChannelKey("sample", "driver_input", "BRAKE_PEDAL").id == "driver_input.brake_pedal"
    assert ChannelKey("health", "central", "uptime_ms").id == "central.health.uptime_ms"
    node = ChannelKey("sample", "stress", "STRESS_3", NODE_CLOCK)
    assert node.id == "stress.stress_3.node-clock" and node.label == "STRESS_3 (node clock)"
    assert ChannelKey("sample", "", "X").id == "unknown.x"


def test_channel_statistics_and_lookup() -> None:
    c = channel(
        [1.0, float("nan"), 3.0, float("nan")],
        available=[True, False, True, True],
        statuses=("ok", "NOT_FITTED", ""),
        status=[0, 1, 0, 0],
    )
    assert c.count == 4 and c.available_count == 3
    assert c.unavailable_reasons() == {"NOT_FITTED": 1}
    assert c.status_labels() == {"ok": 3, "NOT_FITTED": 1}
    assert c.valid_values().tolist() == [1.0, 3.0]
    assert c.index_at(-1) == -1 and c.index_at(0) == 0 and c.index_at(2500) == 2
    with pytest.raises(ValueError):
        c.values[0] = 5.0
    blank = channel([float("nan")], available=[False], statuses=("ok", ""), status=[1])
    assert blank.unavailable_reasons() == {"unavailable": 1}


def test_discrete_detection() -> None:
    assert channel([0, 1, 1, 0]).is_discrete
    assert not channel([0.5, 1.0]).is_discrete
    assert not channel(list(range(40))).is_discrete
    assert not channel([float("nan")], available=[False]).is_discrete
    assert channel([12.5], kind="health").is_discrete


def test_event_labels() -> None:
    assert SessionEvent(1, "fault", "central", "NODE_LOST", "5").label == "FAULT NODE_LOST(5)"
    assert SessionEvent(1, "event", "central", "BOOT", "").label == "EVENT BOOT"
    assert SessionEvent(1, "loss", "central", "x", "3 records").label == "LOSS 3 records"
    assert SessionEvent(1, "text", "central", "text", "hello").label == "hello"
    assert SessionEvent(1, "text", "central", "text", "z" * 100).label.endswith("...")
    assert SessionEvent(1, "can_raw", "central", "0x7FF", "00").label == "can_raw 0x7FF 00"


def test_session_lookup_and_time_range() -> None:
    shared = channel([1.0, 2.0])
    node = channel([5.0], clock=NODE_CLOCK)
    events = (
        SessionEvent(-5000, "fault", "central", "X", ""),
        SessionEvent(10**9, "event", "stress", "Y", "", synced=False),
        SessionEvent(10**10, "text", "central", "text", "late"),
    )
    session = LbrSession("f.csv", 1, "central", {}, (), {}, (shared, node), events, (), 3)
    assert session.channel("imu.x") is shared and session.channel("missing") is None
    assert session.nodes() == ("imu",)
    assert session.time_range_us() == (-5000, 1000)
    empty = LbrSession("f.csv", 1, "", {}, (), {}, (), (), (), 1)
    assert empty.time_range_us() is None
    with pytest.raises(TypeError):
        session.session["x"] = "y"  # type: ignore[index]
