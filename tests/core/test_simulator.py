"""Real simulator acceptance with seeded values, fake pacing and injected faults."""

from dataclasses import FrozenInstanceError, replace
from threading import Event
from typing import Any, cast

import pytest

from lbr_dashboard.core import (
    CancellationToken,
    SignalDescriptor,
    SimulatedSignal,
    SimulatorSource,
    SourceEventKind,
    TelemetrySession,
    example_signals,
)


def signal(identifier="custom.reading", kind="numeric", rate: Any = 10, values=(0, 1, 2), **kwargs):
    descriptor = SignalDescriptor(
        identifier,
        "Reading",
        ("custom",),
        kind,
        "demo",
        nominal_rate_hz=rate,
        canonical_unit="Pa",
        timestamp_domain="relative",
    )
    return SimulatedSignal(descriptor, values, **kwargs)


def run(source):
    source.open()
    source.start()
    try:
        assert source.join(2), "simulator did not finish"
        return source.events.drain()
    finally:
        assert source.close(1)


def records(events, identifier=None) -> list[Any]:
    return [
        event
        for event in events
        if event.kind is SourceEventKind.RECORD
        and (identifier is None or event.signal_id == identifier)
    ]


@pytest.mark.parametrize("family", ["process", "motion"])
def test_unrelated_example_families_repeat_exactly_without_hardware_or_clock_waits(family) -> None:
    def forbidden(*args):
        pytest.fail("accelerated finite generation must not read or wait on real time")

    source = SimulatorSource(
        example_signals(family),
        duration_ns=1_000_000_000,
        seed=17,
        capacity=512,
        now_ns=forbidden,
        wait=forbidden,
    )
    first = run(source)
    first_stats = source.simulator_stats
    second = run(source)
    assert first == second and first_stats == source.simulator_stats
    assert first[-1].reason == "complete"
    if family == "process":
        assert source.health().last_error is not None

    expected = (
        ("numeric", "status") if family == "process" else ("numeric", "vector", "boolean", "event")
    )
    assert {item.data_type for item in source.schema.signals} == set(expected)
    assert source.schema.metadata == {"simulated": True, "seed": 17}
    assert not source.capabilities.is_live and not source.capabilities.supports_seek
    assert source.health().terminal_reason == "complete"


def test_100hz_1hz_and_status_keep_independent_counts_and_exact_provenance() -> None:
    source = SimulatorSource(
        example_signals(), start_ns=-17, duration_ns=1_000_000_000, capacity=256
    )
    events = run(source)
    fast = records(events, "process.pressure")
    slow = records(events, "process.temperature")
    assert len(fast) == 91 and len(slow) == 2
    assert [event.sample.timestamp_ns for event in slow] == [-17, 999_999_983]
    missing = [event for event in fast if not event.sample.available]
    assert len(missing) == 5 and all(event.sample.value is None for event in missing)
    assert any(event.sample.value == 0 and event.sample.available for event in fast)
    assert all(event.source_id == event.sample.source_id == "simulator" for event in fast)
    assert all(
        event.sample.unit == "Pa" and event.sample.timestamp_domain == "relative" for event in fast
    )
    stats = source.simulator_stats
    assert stats.lost == 5 and stats.stale == 5 and stats.unavailable == 5
    assert stats.attempted == 106 and stats.emitted == 96
    assert source.health().dropped_records == 5
    errors = [cast(Any, event.error) for event in events if event.kind is SourceEventKind.ERROR]
    assert {error.code for error in errors} == {"simulated_loss", "simulated_stale"}
    store = TelemetrySession(source.schema)
    store.ingest(events, source_health={source.source_id: source.health()})
    view = store.snapshot()
    assert len(view.series["process.temperature"]) == 2
    assert view.nearest("process.pressure", 220_000_000 - 17, max_distance_ns=0) is None
    point = view.nearest("process.pressure", 500_000_000 - 17, max_distance_ns=0)
    assert point is not None and not point.sample.available


def test_seed_affects_noise_only_and_adding_another_signal_does_not_change_values() -> None:
    noisy = signal(noise_amplitude=0.2)
    other = signal("another.signal", rate=1)

    def samples(signals, seed):
        return [
            event.sample
            for event in records(
                run(SimulatorSource(signals, seed=seed, duration_ns=1_000_000_000)),
                "custom.reading",
            )
        ]

    a = samples((noisy,), 7)
    assert samples((other, noisy), 7) == a
    b = samples((noisy,), 8)
    assert [sample.timestamp_ns for sample in a] == [sample.timestamp_ns for sample in b]
    assert [sample.value for sample in a] != [sample.value for sample in b]
    for index, sample in enumerate(a):
        assert abs(sample.value - noisy.values[index % 3]) <= 0.2


def test_nonintegral_period_does_not_accumulate_timestamp_drift_and_ties_are_stable() -> None:
    signals = (signal("z.reading", rate=3), signal("a.reading", rate=1))
    events = run(SimulatorSource(signals, start_ns=2**80 + 1, duration_ns=1_000_000_000))
    assert [event.sample.timestamp_ns - (2**80 + 1) for event in records(events, "z.reading")] == [
        0,
        333_333_333,
        666_666_666,
        1_000_000_000,
    ]
    assert [event.signal_id for event in records(events)][:2] == ["a.reading", "z.reading"]
    reverse = run(
        SimulatorSource(tuple(reversed(signals)), start_ns=2**80 + 1, duration_ns=1_000_000_000)
    )
    assert events == reverse


def test_stale_and_loss_have_no_records_unavailable_has_explicit_null_and_heartbeat_continues() -> (
    None
):
    model = signal(rate=10, loss=((1, 3),), stale=((3, 5),), unavailable=((5, 7),))
    events = run(SimulatorSource((model,), duration_ns=700_000_000, heartbeat_ns=100_000_000))
    data = records(events)
    assert [event.sample.timestamp_ns for event in data] == [
        0,
        500_000_000,
        600_000_000,
        700_000_000,
    ]
    assert [event.sample.value for event in data] == [0, None, None, 1]
    heartbeats = [event for event in events if event.kind is SourceEventKind.HEALTH]
    assert len(heartbeats) == 7
    assert cast(Any, heartbeats[-1].health).dropped_records == 2


@pytest.mark.parametrize("offset", [0, 150_000_000, 1_000_000_000])
def test_disconnect_is_fatal_at_exact_scheduled_time_and_never_fakes_terminal_success(
    offset,
) -> None:
    source = SimulatorSource((signal(),), duration_ns=1_000_000_000, disconnect_after_ns=offset)
    events = run(source)
    data = records(events)
    assert all(event.sample.timestamp_ns < offset for event in data)
    failure = source.health().last_error
    assert failure is not None and failure.code == "simulated_disconnect"
    assert failure.context["timestamp_ns"] == offset
    assert source.health().terminal_reason == "failed"
    assert events[-1].reason == "failed"


def test_disconnect_after_duration_does_not_change_natural_end_and_zero_duration_is_valid() -> None:
    source = SimulatorSource((signal(),), duration_ns=0, disconnect_after_ns=1)
    events = run(source)
    assert len(records(events)) == 1 and events[-1].reason == "complete"


def test_realtime_pacing_uses_injected_clock_wait_and_matches_accelerated_records() -> None:
    now = [1234]
    waits = []

    def wait(token, delay):
        token.raise_if_cancelled()
        waits.append(delay)
        now[0] += delay

    args: dict[str, Any] = dict(
        signals=(signal(rate=3),), duration_ns=1_100_000_000, heartbeat_ns=300_000_000
    )
    accelerated = run(SimulatorSource(**args))
    paced = run(SimulatorSource(**args, realtime=True, now_ns=lambda: now[0], wait=wait))
    assert paced == accelerated
    assert sum(waits) == 1_100_000_000 and max(waits) <= 1_000_000_000


def test_live_generator_can_be_accelerated_and_cancelled_without_a_replay_clock() -> None:
    token = CancellationToken()
    now = [0]
    waits = []

    def wait(cancel, delay):
        waits.append(delay)
        now[0] += delay
        if now[0] >= 500_000_000:
            token.cancel()

    source = SimulatorSource(
        (signal(),), duration_ns=None, realtime=True, now_ns=lambda: now[0], wait=wait
    )
    source.open(token)
    source.start()
    try:
        assert source.join(1)
        assert source.capabilities.is_live
        assert source.health().terminal_reason == "cancelled"
        assert source.simulator_stats.emitted == 5
    finally:
        assert source.close()


def test_default_live_wait_can_be_cancelled_with_stalled_consumer() -> None:
    entered = Event()

    def now():
        entered.set()
        return 0

    source = SimulatorSource(
        (signal(rate=1),),
        duration_ns=None,
        realtime=True,
        now_ns=now,
        capacity=1,
        publish_timeout=0,
    )
    source.open()
    source.start()
    try:
        assert entered.wait(1)
        assert source.close(1)
        assert source.health().terminal_reason == "cancelled"
    finally:
        assert source.close()


def test_monotonic_regression_and_invalid_return_are_observable_failures() -> None:
    times = iter((100, 100, 99))
    source = SimulatorSource((signal(),), realtime=True, now_ns=lambda: next(times))
    run(source)
    assert source.health().last_error is not None
    assert source.health().terminal_reason == "failed"
    invalid = SimulatorSource((signal(),), realtime=True, now_ns=lambda: cast(Any, "bad"))
    run(invalid)
    assert invalid.health().terminal_reason == "failed"


def test_frozen_configuration_keeps_nested_status_and_vector_values_detached() -> None:
    raw: Any = {"mode": [1, 2]}
    status = signal(kind="status", values=(raw,))
    raw["mode"].append(3)
    assert status.values == ({"mode": (1, 2)},)
    with pytest.raises(TypeError):
        status.values[0]["mode"][0] = 9
    with pytest.raises(FrozenInstanceError):
        cast(Any, status).values = ()


@pytest.mark.parametrize("values", [(), "bad", (None,), ("wrong",)])
def test_invalid_value_cycles(values) -> None:
    with pytest.raises((ValueError, TypeError)):
        signal(values=values)


@pytest.mark.parametrize(
    "intervals",
    [
        None,
        ((0,),),
        ((-1, 2),),
        ((2, 1),),
        ((0, 0),),
        ((0, 3), (2, 4)),
        ((True, 2),),
        ((0, 1.5),),
        "bad",
    ],
)
def test_invalid_fault_intervals(intervals) -> None:
    with pytest.raises((ValueError, TypeError)):
        signal(loss=intervals)


@pytest.mark.parametrize("amplitude", [-1, True, float("nan"), float("inf"), "1", 10**1000])
def test_invalid_noise(amplitude) -> None:
    with pytest.raises(ValueError):
        signal(noise_amplitude=amplitude)


def test_noise_only_numeric_explicit_domains_rates_and_disjoint_windows() -> None:
    for action in (
        lambda: signal(kind="boolean", values=(False,), noise_amplitude=1),
        lambda: signal(rate=None),
        lambda: signal(rate=1_000_000_001),
        lambda: SimulatedSignal(replace(signal().descriptor, timestamp_domain="unspecified"), (0,)),
        lambda: signal(loss=((0, 2),), unavailable=((1, 3),)),
        lambda: SimulatedSignal(cast(Any, None), (1,)),
    ):
        with pytest.raises((ValueError, TypeError)):
            action()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"seed": True},
        {"start_ns": 1.5},
        {"duration_ns": -1},
        {"heartbeat_ns": 0},
        {"disconnect_after_ns": -1},
        {"realtime": 1},
        {"now_ns": None},
        {"wait": 1},
    ],
)
def test_invalid_simulator_configuration(kwargs) -> None:
    with pytest.raises((ValueError, TypeError)):
        SimulatorSource((signal(),), **kwargs)


def test_invalid_schema_family_source_and_duplicate_signal_ids() -> None:
    for signals in (
        (),
        (1,),
        (signal(), signal()),
        (
            signal(),
            replace(
                signal("other.value"),
                descriptor=replace(signal("other.value").descriptor, source_id="other"),
            ),
        ),
    ):
        with pytest.raises((ValueError, TypeError)):
            SimulatorSource(cast(Any, signals))
    with pytest.raises(ValueError):
        example_signals("unknown")


def test_queue_loss_and_injected_loss_remain_distinguishable_and_bounded() -> None:
    source = SimulatorSource(example_signals(), capacity=1, publish_timeout=0)
    run(source)
    stats = source.simulator_stats
    assert stats.lost == 5
    assert source.events.stats.dropped_records == stats.emitted
    assert source.health().dropped_records == stats.emitted + 5
    assert source.events.stats.buffered <= 1


@pytest.mark.parametrize("values", [{1, 2}, {"key": 1}, None])
def test_unordered_or_non_sequence_values_are_not_silent_cycles(values) -> None:
    with pytest.raises(ValueError):
        signal(values=values)


def test_signal_names_rates_source_units_and_types_are_configuration_not_hardcoded() -> None:
    model = SignalDescriptor(
        "unrelated.meter",
        "Custom",
        ("elsewhere",),
        "string",
        "adapter-x",
        nominal_rate_hz=2,
        canonical_unit=None,
        timestamp_domain="device_monotonic",
    )
    source = SimulatorSource(
        (SimulatedSignal(model, ("ready", "waiting")),), start_ns=-5, duration_ns=1_000_000_000
    )
    data = records(run(source))
    assert [item.sample.timestamp_ns for item in data] == [-5, 499999995, 999999995]
    assert [item.sample.value for item in data] == ["ready", "waiting", "ready"]
    assert all(
        item.signal_id == model.identifier and item.source_id == "adapter-x" for item in data
    )
    assert all(
        item.sample.timestamp_domain == "device_monotonic" and item.sample.unit is None
        for item in data
    )
