"""Deterministic shared-time tests; no real-time sleeping or sensor dependencies."""

from dataclasses import FrozenInstanceError
from fractions import Fraction
from threading import Thread
from typing import Any, cast

import pytest

from lbr_dashboard.core import (
    ClockSubscriberError,
    PlaybackMode,
    SessionClock,
    SignalDescriptor,
    SignalSample,
    SignalSchema,
    TelemetrySession,
)


class FakeTime:
    def __init__(self, value=0):
        self.value = value
        self.reads = 0

    def __call__(self):
        self.reads += 1
        return self.value

    def advance(self, value):
        self.value += value


def clock(start: int | None = 0, end: int | None = 1_000_000_000):
    time = FakeTime()
    return SessionClock(start_ns=start, end_ns=end, now_ns=time), time


def session(domain="relative", source="fixture", identifier="custom.pressure"):
    model = SignalDescriptor(
        identifier,
        "Reading",
        ("custom",),
        "numeric",
        source,
        canonical_unit="Pa",
        timestamp_domain=domain,
    )
    return TelemetrySession(SignalSchema(1, (model,)), max_samples_per_signal=10)


def test_play_pause_resume_speed_and_end_have_exact_fake_time_results() -> None:
    shared, time = clock()
    shared.play()
    time.advance(100_000_000)
    assert shared.tick().selected_ns == 100_000_000
    time.advance(50_000_000)
    assert shared.pause().selected_ns == 150_000_000
    time.advance(200_000_000)
    assert shared.tick().selected_ns == 150_000_000
    shared.play()
    time.advance(50_000_000)
    assert shared.set_speed(2).selected_ns == 200_000_000
    time.advance(200_000_000)
    assert shared.tick().selected_ns == 600_000_000
    time.advance(1_000_000_000)
    assert shared.tick().selected_ns == 1_000_000_000
    assert shared.snapshot().mode is PlaybackMode.ENDED
    assert shared.play().mode is PlaybackMode.ENDED
    assert shared.seek(0).mode is PlaybackMode.PAUSED
    assert shared.play().mode is PlaybackMode.PLAYING


def test_fractional_speed_is_exact_without_tick_rounding_drift() -> None:
    shared, time = clock(end=10000)
    shared.set_speed(Fraction(1, 3))
    shared.play()
    for i in range(1, 3001):
        time.advance(1)
        assert shared.tick().selected_ns == i // 3
    assert shared.snapshot().speed == Fraction(1, 3)
    shared.pause()
    time.advance(100)
    shared.set_speed(0.1)
    shared.play()
    for _ in range(10):
        time.advance(1)
        shared.tick()
    assert shared.snapshot().selected_ns == 1001


def test_fractional_position_survives_pause_and_large_negative_original_times() -> None:
    start = -(2**80) + 1
    shared, time = clock(start=start, end=start + 100)
    shared.set_speed(Fraction(1, 3))
    shared.play()
    time.advance(1)
    shared.pause()
    time.advance(100)
    shared.play()
    time.advance(2)
    assert shared.tick().selected_ns == start + 1
    shared.seek(2**90)
    assert shared.snapshot().selected_ns == start + 100
    shared.seek(-(2**90))
    assert shared.snapshot().selected_ns == start


def test_backwards_seek_clamps_and_does_not_jump_when_resumed() -> None:
    shared, time = clock(10, 100)
    shared.play()
    time.advance(50)
    assert shared.tick().selected_ns == 60
    assert shared.seek(25).selected_ns == 25
    time.advance(100)
    assert shared.tick().selected_ns == 25
    shared.play()
    time.advance(5)
    assert shared.tick().selected_ns == 30
    assert shared.seek(-100).selected_ns == 10
    assert shared.seek(1000).selected_ns == 100


def test_empty_single_sample_clear_and_later_bounds_are_explicit() -> None:
    shared, time = clock(None, None)
    assert shared.snapshot().selected_ns is None
    assert shared.play().mode is PlaybackMode.PAUSED
    assert shared.seek(17).selected_ns is None
    shared.update_bounds(20, 20)
    assert shared.snapshot().selected_ns == 20
    assert shared.play().mode is PlaybackMode.ENDED
    assert shared.update_bounds(None, None).selected_ns is None
    shared.follow_live()
    assert shared.snapshot().mode is PlaybackMode.LIVE
    assert shared.update_bounds(10, 100).selected_ns == 100
    shared.update_bounds(None, None)
    assert shared.snapshot().mode is PlaybackMode.LIVE


def test_bounds_retention_clamps_selection_and_expansion_does_not_restart_end() -> None:
    shared, time = clock(0, 100)
    shared.seek(20)
    assert shared.update_bounds(50, 150).selected_ns == 50
    shared.play()
    time.advance(75)
    assert shared.update_bounds(50, 100).mode is PlaybackMode.ENDED
    assert shared.snapshot().selected_ns == 100
    shared.update_bounds(50, 200)
    time.advance(100)
    assert shared.tick().selected_ns == 100
    shared.play()
    time.advance(25)
    assert shared.tick().selected_ns == 125


def test_live_follow_disconnect_reconnect_seek_and_pause_are_deterministic() -> None:
    shared, time = clock(0, 100)
    assert shared.follow_live().selected_ns == 100
    time.advance(1000)
    assert shared.tick().selected_ns == 100  # no extrapolation beyond recorded data
    assert shared.update_bounds(0, 200).selected_ns == 200
    shared.set_connected(False)
    assert shared.update_bounds(0, 300).selected_ns == 200
    time.advance(1000)
    assert shared.tick().selected_ns == 200
    assert shared.set_connected(True).selected_ns == 300
    shared.seek(100)
    shared.update_bounds(0, 400)
    assert shared.snapshot().selected_ns == 100 and shared.snapshot().mode is PlaybackMode.PAUSED
    shared.follow_live()
    shared.pause()
    shared.set_connected(False)
    shared.update_bounds(0, 500)
    shared.set_connected(True)
    assert shared.snapshot().selected_ns == 400


def test_disconnected_follow_waits_for_reconnect_and_retained_bounds_still_apply() -> None:
    shared, time = clock(0, 100)
    shared.seek(20)
    shared.set_connected(False)
    shared.follow_live()
    assert shared.snapshot().selected_ns == 20
    assert shared.update_bounds(50, 200).selected_ns == 50
    assert shared.set_connected(True).selected_ns == 200
    shared.set_connected(False)
    shared.update_bounds(None, None)
    shared.update_bounds(300, 400)
    assert shared.snapshot().selected_ns == 300
    assert shared.set_connected(True).selected_ns == 400


def test_every_subscriber_gets_the_same_immutable_frame_without_extra_clock_reads() -> None:
    shared, time = clock()
    first, second = [], []
    off1 = shared.subscribe(first.append)
    off2 = shared.subscribe(second.append)
    initial = shared.snapshot()
    reads = time.reads
    time.advance(99)
    assert shared.snapshot() is initial and time.reads == reads
    shared.play()
    time.advance(200)
    frame = shared.tick()
    assert first == second
    assert first[-1] is second[-1] is frame is shared.snapshot()
    with pytest.raises(FrozenInstanceError):
        cast(Any, frame).selected_ns = 0
    revision = frame.revision
    assert shared.tick() is frame and frame.revision == revision
    off1()
    off1()
    time.advance(1)
    shared.tick()
    assert len(second) == len(first) + 1
    off2()


def test_failing_subscriber_does_not_starve_other_subscribers() -> None:
    shared, time = clock()
    good = []

    def broken(frame):
        if frame.revision:
            raise RuntimeError("failed presentation")

    off = shared.subscribe(broken)
    shared.subscribe(good.append)
    with pytest.raises(ClockSubscriberError) as failure:
        shared.play()
    assert len(failure.value.errors) == 1
    assert good[-1] is shared.snapshot() and shared.snapshot().mode is PlaybackMode.PLAYING
    off()
    time.advance(10)
    assert shared.tick().selected_ns == 10


def test_reentrant_commands_are_rejected_without_changing_other_subscriber_frames() -> None:
    shared, time = clock()
    good = []

    def mutate(frame):
        if frame.revision:
            shared.seek(999)

    off = shared.subscribe(mutate)
    shared.subscribe(good.append)
    with pytest.raises(ClockSubscriberError) as failure:
        shared.play()
    assert isinstance(failure.value.errors[0], RuntimeError)
    assert good[-1].selected_ns == 0
    off()


def test_initial_callback_failure_removes_registration_and_reentrant_subscribe_fails() -> None:
    shared, time = clock()
    calls = []

    def bad(frame):
        calls.append(frame)
        raise RuntimeError("initial failure")

    with pytest.raises(RuntimeError, match="initial failure"):
        shared.subscribe(bad)
    shared.play()
    assert len(calls) == 1

    def attach(frame):
        if frame.revision > 1:
            shared.subscribe(lambda frame: None)

    off = shared.subscribe(attach)
    time.advance(10)
    with pytest.raises(ClockSubscriberError):
        shared.tick()
    off()
    with pytest.raises(TypeError):
        shared.subscribe(cast(Any, None))


def test_unsubscribe_during_delivery_preserves_current_frame_for_all_initial_listeners() -> None:
    shared, time = clock()
    first, second = [], []

    off2 = None

    def callback(frame):
        first.append(frame)
        if frame.revision and off2 is not None:
            off2()

    shared.subscribe(callback)
    off2 = shared.subscribe(second.append)
    shared.play()
    assert first[-1] is second[-1]
    time.advance(1)
    shared.tick()
    assert len(first) == len(second) + 1


@pytest.mark.parametrize(
    "speed", [0, -1, True, None, "2", float("nan"), float("inf"), Fraction(-1, 2)]
)
def test_invalid_speed_is_atomic(speed) -> None:
    shared, time = clock()
    shared.play()
    time.advance(10)
    before = shared.snapshot()
    reads = time.reads
    with pytest.raises(ValueError):
        shared.set_speed(speed)
    assert shared.snapshot() is before and time.reads == reads


@pytest.mark.parametrize(
    "bounds", [(None, 1), (0, None), (2, 1), (True, 3), (0, float("nan")), ("0", 1)]
)
def test_invalid_bounds_are_atomic(bounds) -> None:
    shared, time = clock()
    before = shared.snapshot()
    with pytest.raises(ValueError):
        shared.update_bounds(*bounds)
    assert shared.snapshot() is before
    with pytest.raises(ValueError):
        SessionClock(start_ns=bounds[0], end_ns=bounds[1])


@pytest.mark.parametrize("value", [True, 1.5, None, "1", float("nan")])
def test_invalid_seek_and_clock_return_values_are_rejected(value) -> None:
    shared, time = clock()
    before = shared.snapshot()
    with pytest.raises(ValueError):
        shared.seek(value)
    time.value = value
    with pytest.raises(ValueError):
        shared.tick()
    assert shared.snapshot() is before


def test_monotonic_regression_and_invalid_connection_are_atomic() -> None:
    shared, time = clock()
    shared.play()
    time.advance(10)
    shared.tick()
    before = shared.snapshot()
    time.value = 9
    with pytest.raises(ValueError, match="backwards"):
        shared.tick()
    assert shared.snapshot() is before
    with pytest.raises(ValueError):
        shared.set_connected(cast(Any, 1))
    with pytest.raises(TypeError):
        SessionClock(now_ns=cast(Any, None))


def test_controls_are_owner_thread_only_but_immutable_snapshot_can_be_read_elsewhere() -> None:
    shared, time = clock()
    results = []

    def worker():
        results.append(shared.snapshot())
        try:
            shared.play()
        except RuntimeError as error:
            results.append(error)

    thread = Thread(target=worker)
    thread.start()
    thread.join(1)
    assert not thread.is_alive()
    assert results[0] is shared.snapshot() and isinstance(results[1], RuntimeError)


def test_session_binding_preserves_raw_gap_and_reference_identity() -> None:
    store = session()
    for stamp, value in ((-10, 0), (0, None), (100, 10)):
        store.append(
            "custom.pressure",
            SignalSample(
                stamp,
                value,
                available=value is not None,
                timestamp_domain="relative",
                source_id="fixture",
                unit="psi",
                quality="raw",
            ),
        )
    shared, time = clock(None, None)
    frame = shared.update_from_session(store.snapshot(), "custom.pressure")
    assert (frame.start_ns, frame.end_ns, frame.selected_ns) == (-10, 100, -10)
    assert frame.reference == ("custom.pressure", "fixture", "relative")
    shared.seek(50)
    assert store.nearest("custom.pressure", 50, max_distance_ns=10) is None
    original = store.snapshot()
    shared.play()
    time.advance(1)
    shared.tick()
    assert store.snapshot() == original
    missing = store.nearest("custom.pressure", 0)
    assert missing is not None and missing.sample.value is None and missing.sample.unit == "psi"
    for other in (
        session(source="another"),
        session(domain="unix_utc"),
        session(identifier="other.id"),
    ):
        identifier = other.schema.signals[0].identifier
        before = shared.snapshot()
        with pytest.raises(ValueError, match="reference"):
            shared.update_from_session(other.snapshot(), identifier)
        assert shared.snapshot() is before
    with pytest.raises(KeyError):
        shared.update_from_session(store.snapshot(), "unknown")
    with pytest.raises(TypeError):
        shared.update_from_session(cast(Any, None), "custom.pressure")


def test_empty_session_binds_first_real_domain_and_live_extends_retained_bounds() -> None:
    store = session(domain="unspecified")
    shared, time = clock(None, None)
    shared.update_from_session(store.snapshot(), "custom.pressure")
    assert shared.snapshot().reference is None
    shared.follow_live()
    store.append("custom.pressure", SignalSample(100, 0, timestamp_domain="device_monotonic"))
    assert shared.update_from_session(store.snapshot(), "custom.pressure").selected_ns == 100
    store.append("custom.pressure", SignalSample(200, 1, timestamp_domain="device_monotonic"))
    assert shared.update_from_session(store.snapshot(), "custom.pressure").selected_ns == 200


def test_reference_binding_survives_subscriber_failure_as_one_committed_frame() -> None:
    store = session()
    store.append("custom.pressure", SignalSample(100, 0, timestamp_domain="relative"))
    shared, time = clock(None, None)

    def fail(frame):
        if frame.reference:
            raise RuntimeError("bad widget")

    off = shared.subscribe(fail)
    with pytest.raises(ClockSubscriberError):
        shared.update_from_session(store.snapshot(), "custom.pressure")
    assert shared.snapshot().reference == ("custom.pressure", "fixture", "relative")
    off()


def test_empty_unspecified_session_keeps_known_reference_domain_while_waiting() -> None:
    store = session(domain="unspecified")
    store.append("custom.pressure", SignalSample(100, 1, timestamp_domain="device_monotonic"))
    shared, time = clock(None, None)
    shared.update_from_session(store.snapshot(), "custom.pressure")
    shared.follow_live()
    empty = session(domain="unspecified")
    frame = shared.update_from_session(empty.snapshot(), "custom.pressure")
    assert frame.selected_ns is None and frame.mode is PlaybackMode.LIVE
    assert frame.reference == ("custom.pressure", "fixture", "device_monotonic")


def test_reference_change_validation_and_clock_failure_leave_binding_unchanged() -> None:
    store = session()
    store.append("custom.pressure", SignalSample(1, 1, timestamp_domain="relative"))
    shared, time = clock(None, None)
    before = shared.snapshot()
    time.value = -1
    with pytest.raises(ValueError):
        shared.update_from_session(store.snapshot(), "custom.pressure")
    assert shared.snapshot() is before and shared.snapshot().reference is None
