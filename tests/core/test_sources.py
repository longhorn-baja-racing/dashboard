"""Exercise the production source with finite, live and failure-injected adapters."""

from dataclasses import replace
from threading import Event, Thread
from time import monotonic
from typing import Any, cast

import pytest

from lbr_dashboard.core import (
    CancellationToken,
    IteratorSource,
    SignalDescriptor,
    SignalSample,
    SignalSchema,
    SourceCapabilities,
    SourceError,
    SourceEvent,
    SourceEventKind,
    SourceEventQueue,
    SourceHealth,
    SourceState,
    TelemetrySource,
    UnsupportedSourceOperation,
)


def descriptor(identifier="lab.pressure", kind="numeric", source="fixture"):
    return SignalDescriptor(
        identifier,
        "Reading",
        ("lab",),
        kind,
        source,
        canonical_unit="Pa",
        timestamp_domain="device_monotonic",
    )


def record(time=17, value: Any = 0, identifier="lab.pressure", source="fixture", **kwargs):
    return SourceEvent(
        SourceEventKind.RECORD,
        source,
        signal_id=identifier,
        sample=SignalSample(
            time, value, timestamp_domain="device_monotonic", source_id=source, unit="Pa", **kwargs
        ),
    )


def end(reason="complete"):
    return SourceEvent(SourceEventKind.END_OF_STREAM, "fixture", reason=reason)


def make_source(factory=None, **kwargs) -> IteratorSource:
    return IteratorSource(
        "fixture",
        SignalSchema(1, (descriptor(),)),
        factory or (lambda token: iter((record(),))),
        **kwargs,
    )


def run(source: TelemetrySource):
    source.open()
    source.start()
    assert source.join(1), "source did not finish"
    return source.events.drain()


def test_concrete_source_preserves_identified_samples_and_reopens() -> None:
    expected = [
        record(-7, 0),
        record(7, None, available=False, quality="missing"),
        record(7, 2),
        record(2**70 + 1, 3),
    ]
    source = make_source(lambda token: iter(expected))
    for _ in range(3):
        events = run(source)
        assert events[0].descriptor == descriptor()
        assert events[1:-1] == tuple(expected)
        assert events[-1] == end()
        assert source.health().terminal_reason == "complete"
        assert source.state is SourceState.ENDED
        assert source.health().dropped_records == 0
        assert source.close() and source.close()
        assert source.state is SourceState.CLOSED
    assert source.source_id == "fixture"
    assert source.capabilities == SourceCapabilities(False, False, False, 128)
    assert source.schema.signals == (descriptor(),)


@pytest.mark.parametrize("live", [False, True])
def test_unrelated_numeric_boolean_and_status_sources(live) -> None:
    schema = SignalSchema(
        1,
        (
            descriptor(),
            descriptor("custom.enabled", "boolean"),
            descriptor("other.status", "status"),
        ),
    )
    values = [
        record(0, 1),
        record(0, False, "custom.enabled"),
        record(1_000_000_000, {"state": "ready"}, "other.status"),
        record(10_000_000, 2),
    ]
    source = IteratorSource("fixture", schema, lambda token: iter(values), is_live=live)
    events = run(source)
    assert [event for event in events if event.kind is SourceEventKind.RECORD] == values
    assert source.capabilities.is_live is live
    assert source.close()


def test_cancel_live_source_releases_generator_and_never_needs_consumer() -> None:
    entered, cleaned = Event(), Event()

    def generate(token):
        try:
            entered.set()
            while not token.wait(0.001):
                yield record()
        finally:
            cleaned.set()

    source = make_source(generate, is_live=True, capacity=1, publish_timeout=10)
    source.open()
    source.start()
    try:
        assert entered.wait(1)
        assert not source.join(0)
        source.stop()
        assert source.close(1)
        assert cleaned.is_set()
        assert source.health().terminal_reason == "cancelled"
        assert source.health().rejected_controls >= 1
        assert source.events.stats.closed
        assert source.events.stats.buffered <= 1
    finally:
        source.close(1)


@pytest.mark.parametrize("stage", ["open", "start"])
def test_external_cancellation_reaches_adapter_factory_and_wait(stage) -> None:
    entered, cleaned = Event(), Event()
    token = CancellationToken()

    def factory(cancel):
        entered.set()
        try:
            assert cancel.wait(2)
            cancel.raise_if_cancelled()
        finally:
            cleaned.set()
        return iter(())

    source = make_source(factory)
    source.open(token if stage == "open" else None)
    source.start(token if stage == "start" else None)
    try:
        assert entered.wait(1)
        token.cancel()
        assert source.join(1)
        assert source.state is SourceState.STOPPED
        assert cleaned.is_set() and source.health().last_error is None
        assert source.health().terminal_reason == "cancelled"
    finally:
        source.close()


def test_cancelled_open_does_not_allocate_resources() -> None:
    token = CancellationToken()
    token.cancel()
    source = make_source(lambda token: pytest.fail("cancelled source allocated resources"))
    source.open(token)
    assert source.state is SourceState.STOPPED
    assert source.health().terminal_reason == "cancelled"
    with pytest.raises(RuntimeError):
        source.start()
    assert source.close()


def test_stop_cancels_factory_open_and_close_is_bounded_for_noncooperative_code() -> None:
    entered, release = Event(), Event()

    def factory(token):
        entered.set()
        assert release.wait(2)
        return iter(())

    source = make_source(factory)
    source.open()
    source.start()
    try:
        assert entered.wait(1)
        before = monotonic()
        assert not source.close(0.02)
        assert monotonic() - before < 0.5
        assert source.state is SourceState.STOPPING
        with pytest.raises(RuntimeError):
            source.open()
    finally:
        release.set()
        assert source.close(1)
    assert source.health().terminal_reason == "cancelled"


@pytest.mark.parametrize("when", ["open", "read", "cleanup"])
def test_real_source_failures_are_retained_with_full_queue(when) -> None:
    def broken(token):
        if when == "open":
            raise OSError("opening failed")

        def generate():
            try:
                yield record()
                if when == "read":
                    raise OSError("reading failed")
            finally:
                if when == "cleanup":
                    raise OSError("cleanup failed")

        return generate()

    source = make_source(broken, capacity=1, publish_timeout=0)
    run(source)
    health = source.health()
    assert health.state is SourceState.FAILED
    assert health.last_error is not None
    assert health.last_error.code == ("open_failed" if when == "open" else "read_failed")
    assert health.terminal_reason == "failed"
    assert health.rejected_controls >= 1
    assert source.close()


@pytest.mark.parametrize(
    "value",
    [
        record(source="other"),
        record(identifier="unknown.id"),
        record(value="bad"),
        SourceEvent(SourceEventKind.DESCRIPTOR, "fixture", descriptor=descriptor()),
        end(),
        object(),
    ],
)
def test_bad_adapter_data_becomes_observable_failure(value) -> None:
    source = make_source(lambda token: iter((value,)))
    events = run(source)
    assert source.state is SourceState.FAILED
    assert source.health().last_error is not None
    assert any(event.kind is SourceEventKind.ERROR for event in events)
    assert events[-1].reason == "failed"
    assert not any(event.kind is SourceEventKind.RECORD for event in events)
    assert source.close()


def test_recoverable_errors_and_health_preserve_context() -> None:
    context: Any = {"detail": [1, 2]}
    error = SourceError("gap", "Input record skipped", True, context)
    context["detail"].append(3)
    assert error.context["detail"] == (1, 2)
    warning = SourceEvent(SourceEventKind.ERROR, "fixture", error=error)
    health = SourceEvent(
        SourceEventKind.HEALTH, "fixture", health=SourceHealth(SourceState.RUNNING)
    )
    source = make_source(lambda token: iter((warning, health, record())))
    events = run(source)
    assert events[1:4] == (warning, health, record())
    assert source.state is SourceState.ENDED
    assert source.health().last_error == error
    assert source.close()


def test_fatal_adapter_error_retains_original_code() -> None:
    failure = SourceError("disconnected", "Device disconnected")
    source = make_source(
        lambda token: iter((SourceEvent(SourceEventKind.ERROR, "fixture", error=failure), record()))
    )
    events = run(source)
    assert source.state is SourceState.FAILED
    assert source.health().last_error == failure
    assert [event.error for event in events if event.kind is SourceEventKind.ERROR] == [failure]
    assert not any(event.kind is SourceEventKind.RECORD for event in events)
    assert source.close()


def test_no_consumer_cannot_hide_descriptors_errors_or_terminal_state() -> None:
    source = make_source(
        lambda token: (record(i) for i in range(1000)), capacity=1, publish_timeout=0
    )
    run(source)
    assert source.health().dropped_records == 1000
    assert source.schema.signals == (descriptor(),)
    assert source.health().terminal_reason == "complete"
    assert source.health().rejected_controls == 1
    assert source.close()


def test_lifecycle_misuse_unsupported_operations_and_reopen_without_drain() -> None:
    source = make_source()
    assert source.join(0)
    with pytest.raises(RuntimeError):
        source.start()
    source.open()
    source.open()
    for action in (lambda: source.seek(1), lambda: source.set_rate(2)):
        with pytest.raises(UnsupportedSourceOperation):
            action()
    source.start()
    assert source.join(1)
    with pytest.raises(RuntimeError, match="drain"):
        source.open()
    source.events.drain()
    source.open()
    source.stop()
    assert source.state is SourceState.STOPPED
    assert source.close()


def test_close_called_on_worker_returns_false_without_self_join() -> None:
    closed = []

    def factory(token):
        closed.append(source.close(0))
        return iter(())

    source = make_source(factory)
    run(source)
    assert closed == [False]
    assert source.close()


@pytest.mark.parametrize("capacity", [0, -1, True, 1.5, None])
def test_invalid_queue_capacity(capacity) -> None:
    with pytest.raises(ValueError):
        SourceEventQueue(capacity)


@pytest.mark.parametrize("timeout", [-1, True, float("nan"), float("inf"), None, "1", 86401])
def test_all_waits_reject_invalid_timeout(timeout) -> None:
    queue = SourceEventQueue(1)
    with pytest.raises(ValueError):
        queue.publish(end(), timeout=timeout)
    source = make_source()
    with pytest.raises(ValueError):
        source.join(timeout)
    with pytest.raises(ValueError):
        CancellationToken().wait(timeout)


@pytest.mark.parametrize("limit", [-1, True, 1.5, "1"])
def test_invalid_drain_limit(limit) -> None:
    with pytest.raises(ValueError):
        SourceEventQueue(1).drain(limit)


def test_queue_loss_control_eviction_and_drain_order() -> None:
    queue = SourceEventQueue(2)
    assert queue.publish(record(1)) and queue.publish(record(2))
    assert queue.stats.buffered_records == 2
    assert not queue.publish(record(3))
    assert queue.publish(end())
    assert queue.stats.dropped_records == 2
    assert queue.snapshot() == (record(2), end())
    assert queue.drain(0) == ()
    assert queue.drain(1) == (record(2),)
    queue.close()
    queue.close()
    assert not queue.publish(record())
    assert queue.drain() == (end(),)
    assert queue.stats.buffered == 0 and queue.stats.closed


@pytest.mark.parametrize("action", ["close", "cancel", "drain"])
def test_waiting_producer_is_released_without_indefinite_wait(action) -> None:
    queue = SourceEventQueue(1)
    queue.publish(end())
    entered = Event()
    token = CancellationToken()
    result = []

    def publish():
        entered.set()
        result.append(queue.publish(end("next"), timeout=10, cancellation=token))

    worker = Thread(target=publish)
    worker.start()
    try:
        assert entered.wait(1)
        if action == "close":
            queue.close()
        elif action == "cancel":
            token.cancel()
        else:
            queue.drain()
        worker.join(1)
        assert not worker.is_alive()
        assert result == [action == "drain"]
    finally:
        queue.close()
        worker.join(1)


def test_invalid_capabilities_errors_and_events_are_rejected() -> None:
    for field in ("is_live", "supports_seek", "supports_rate_control"):
        values: dict[str, Any] = dict(
            is_live=False, supports_seek=False, supports_rate_control=False, buffer_capacity=1
        )
        values[field] = 1
        with pytest.raises(TypeError):
            SourceCapabilities(**values)
    for values in (
        {"code": ""},
        {"code": None},
        {"message": ""},
        {"recoverable": 1},
        {"context": {"x": float("nan")}},
    ):
        args: dict[str, Any] = {"code": "bad", "message": "Bad input", **values}
        with pytest.raises((TypeError, ValueError)):
            SourceError(**args)
    for values in (
        {"kind": "record"},
        {"signal_id": "BAD"},
        {"sample": None},
        {"sample": 1},
        {"source_id": "BAD"},
        {"reason": "extra"},
        {"source_id": "other"},
    ):
        with pytest.raises((TypeError, ValueError)):
            replace(record(), **values)
    for event in (
        lambda: SourceEvent(SourceEventKind.DESCRIPTOR, "fixture", descriptor=cast(Any, 1)),
        lambda: SourceEvent(SourceEventKind.DESCRIPTOR, "other", descriptor=descriptor()),
        lambda: SourceEvent(SourceEventKind.HEALTH, "fixture", health=cast(Any, 1)),
        lambda: SourceEvent(SourceEventKind.ERROR, "fixture", error=cast(Any, 1)),
        lambda: end(" "),
        lambda: replace(end(), signal_id="lab.pressure"),
    ):
        with pytest.raises((TypeError, ValueError)):
            event()
    with pytest.raises(TypeError):
        SourceEventQueue(1).publish(cast(Any, 1))


def test_bad_factory_and_constructor_are_observable() -> None:
    source = make_source(lambda token: [])
    run(source)
    assert source.health().last_error is not None
    assert source.close()
    with pytest.raises(ValueError):
        IteratorSource("wrong", SignalSchema(1, (descriptor(),)), lambda token: iter(()))
    with pytest.raises(TypeError):
        IteratorSource("fixture", cast(Any, None), lambda token: iter(()))
    with pytest.raises(TypeError):
        IteratorSource("fixture", SignalSchema(1, ()), cast(Any, None))


def test_cancellation_wait_is_cooperative_and_nonblocking_at_zero() -> None:
    token = CancellationToken()
    assert not token.wait(0)
    token.cancel()
    assert token.wait(0)


@pytest.mark.parametrize("fail_read", [False, True])
def test_iterator_close_failure_is_retained_and_worker_finishes(fail_read) -> None:
    class BrokenCleanup:
        def __iter__(self):
            return self

        def __next__(self):
            if fail_read:
                raise OSError("read failed first")
            raise StopIteration

        def close(self):
            raise OSError("close failed")

    source = make_source(lambda token: BrokenCleanup())
    run(source)
    error = source.health().last_error
    assert error is not None
    assert error.code == ("read_failed" if fail_read else "close_failed")
    assert source.health().terminal_reason == "failed"
    assert source.close()


def test_thread_start_failure_reaches_caller_and_health(monkeypatch) -> None:
    def fail_start(self):
        raise RuntimeError("cannot start worker")

    source = make_source()
    source.open()
    monkeypatch.setattr("lbr_dashboard.core.sources.Thread.start", fail_start)
    with pytest.raises(RuntimeError, match="cannot start"):
        source.start()
    failure = source.health().last_error
    assert failure is not None
    assert failure.code == "start_failed"
    assert source.state is SourceState.FAILED
    assert source.health().terminal_reason == "failed"
    assert [event.kind for event in source.events.drain()] == [
        SourceEventKind.ERROR,
        SourceEventKind.END_OF_STREAM,
    ]
    assert source.close()


@pytest.mark.parametrize(
    "values",
    [
        {"state": "running"},
        {"last_error": 1},
        {"buffered_records": -1},
        {"dropped_records": True},
        {"rejected_controls": 1.5},
        {"terminal_reason": " "},
    ],
)
def test_health_validates_its_immutable_snapshot(values) -> None:
    args: dict[str, Any] = {"state": SourceState.OPEN, **values}
    with pytest.raises((TypeError, ValueError)):
        SourceHealth(**args)


def test_health_events_are_counted_separately_from_record_loss() -> None:
    queue = SourceEventQueue(1)
    health = SourceEvent(SourceEventKind.HEALTH, "fixture", health=SourceHealth(SourceState.OPEN))
    assert queue.publish(health)
    assert not queue.publish(health)
    assert queue.publish(end())
    assert queue.stats.dropped_health == 2
    assert queue.stats.dropped_records == 0
    assert queue.stats.buffered_records == 0
    with pytest.raises(ValueError):
        SourceCapabilities(False, False, False, 0)
    with pytest.raises(ValueError):
        replace(record(), signal_id="")
    with pytest.raises(ValueError):
        queue.publish(end(), timeout=cast(Any, 10**1000))


def test_queue_concurrent_producers_never_exceed_capacity() -> None:
    queue = SourceEventQueue(3)
    workers = [
        Thread(target=lambda: [queue.publish(record(i)) for i in range(200)]) for _ in range(4)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(1)
        assert not worker.is_alive()
    stats = queue.stats
    assert stats.buffered == stats.buffered_records == 3
    assert stats.published + stats.dropped_records == 800
    assert len(queue.drain()) == 3


def test_stop_before_start_and_invalid_close_timeout_do_not_lose_state() -> None:
    source = make_source()
    source.stop()
    assert source.close()
    source.open()
    with pytest.raises(ValueError):
        source.close(float("nan"))
    assert source.state is SourceState.OPEN
    assert source.close()
