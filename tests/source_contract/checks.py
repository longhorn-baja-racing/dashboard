"""Reusable assertions over public #18/#20 contracts; no widget dependencies."""

from collections.abc import Callable
from dataclasses import dataclass
from threading import Event
from time import monotonic

from lbr_dashboard.core import (
    SignalSchema,
    SourceEvent,
    SourceEventKind,
    SourceState,
    TelemetrySource,
    UnsupportedSourceOperation,
)

DEADLINE = 2.0


@dataclass
class AdapterRun:
    source: TelemetrySource
    resources_idle: Callable[[], bool] = lambda: True


def until(predicate: Callable[[], bool], message: str) -> None:
    end = monotonic() + DEADLINE
    pause = Event()
    while not predicate():
        assert monotonic() < end, message
        pause.wait(0.001)


def assert_closed(run: AdapterRun) -> None:
    source = run.source
    assert source.close(DEADLINE), "close did not release the source within its deadline"
    assert source.join(0), "worker remains alive after successful close"
    assert source.state is SourceState.CLOSED, "successful close must report CLOSED"
    assert source.events.stats.closed, "closed source still accepts publication"
    assert run.resources_idle(), "adapter leaked an input/resource after close"
    assert source.close(0), "repeated close must succeed"


def collect(source: TelemetrySource, *, event_limit: int = 256) -> tuple[SourceEvent, ...]:
    """Drain a finite run with bounded wall time and output, even for broken adapters."""
    end = monotonic() + DEADLINE
    events: list[SourceEvent] = []
    pause = Event()
    while True:
        stats = source.events.stats
        assert stats.capacity == source.capabilities.buffer_capacity, "queue capacity misreported"
        assert stats.buffered <= stats.capacity, "source handoff grew beyond its capacity"
        batch = source.events.drain(min(event_limit + 1 - len(events), stats.capacity))
        events.extend(batch)
        assert len(events) <= event_limit, "finite source produced unbounded/unexpected output"
        if source.join(0):
            events.extend(source.events.drain(event_limit + 1 - len(events)))
            assert len(events) <= event_limit, "finite source produced unbounded/unexpected output"
            return tuple(events)
        assert monotonic() < end, "finite source did not finish while its consumer was draining"
        if not batch:
            pause.wait(0.001)


def records(events: tuple[SourceEvent, ...]) -> tuple[SourceEvent, ...]:
    return tuple(event for event in events if event.kind is SourceEventKind.RECORD)


def assert_finite(
    run: AdapterRun,
    schema: SignalSchema,
    expected: tuple[SourceEvent, ...],
    *,
    already_started: bool = False,
) -> tuple[SourceEvent, ...]:
    """Compare with an independent oracle, including missingness and all provenance."""
    source = run.source
    try:
        if not already_started:
            source.open()
            source.start()
        events = collect(source)
        assert source.schema.signals == schema.signals, "original descriptor fields changed"
        assert all(e.source_id == source.source_id for e in events), "source identity changed"
        descriptors = tuple(e.descriptor for e in events if e.kind is SourceEventKind.DESCRIPTOR)
        assert descriptors == schema.signals, "descriptor identity/metadata lost in handoff"
        delivered = records(events)
        assert len(delivered) == len(expected), "records were lost, duplicated or fabricated"
        for index, (actual, wanted) in enumerate(zip(delivered, expected)):
            assert actual.signal_id == wanted.signal_id, f"record {index}: signal identity changed"
            assert actual.source_id == wanted.source_id, f"record {index}: source identity changed"
            assert actual.sample is not None and wanted.sample is not None
            assert actual.sample.timestamp_ns == wanted.sample.timestamp_ns, (
                f"record {index}: original timestamp changed"
            )
            assert actual.sample.available == wanted.sample.available, (
                f"record {index}: missingness changed"
            )
            actual_type = type(actual.sample.value)
            wanted_type = type(wanted.sample.value)
            assert actual_type is wanted_type, (
                f"record {index}: value type changed from "
                f"{wanted_type.__name__} to {actual_type.__name__}"
            )
            assert actual.sample == wanted.sample, (
                f"record {index}: value, missingness, quality, unit or provenance changed"
            )
        ends = tuple(e.reason for e in events if e.kind is SourceEventKind.END_OF_STREAM)
        assert ends == ("complete",), "finite source must publish one successful terminal event"
        assert events[-1].kind is SourceEventKind.END_OF_STREAM, "events published after completion"
        assert all(e.kind is not SourceEventKind.ERROR for e in events), (
            "successful run emitted ERROR"
        )
        assert source.health().terminal_reason == "complete", "terminal health disagrees with EOS"
        assert source.state is SourceState.ENDED, "completed finite source must report ENDED"
        assert source.health().last_error is None, "successful source retained an unexpected error"
        assert source.health().dropped_records == 0, "finite replay lost records under backpressure"
        return events
    finally:
        assert_closed(run)


def assert_controls(
    source: TelemetrySource,
    *,
    seek_probe: Callable[[TelemetrySource], None] | None = None,
    rate_probe: Callable[[TelemetrySource], None] | None = None,
) -> None:
    """True flags require an adapter-specific effect probe, never a silent skip."""
    for name, supported, operation, probe in (
        ("seek", source.capabilities.supports_seek, lambda: source.seek(0), seek_probe),
        ("rate", source.capabilities.supports_rate_control, lambda: source.set_rate(2), rate_probe),
    ):
        if supported:
            assert probe is not None, f"{name} advertised without a control-effect probe"
            probe(source)
        else:
            try:
                operation()
            except UnsupportedSourceOperation:
                pass
            else:
                raise AssertionError(f"unsupported {name} silently succeeded")


def assert_failed(run: AdapterRun, *, has_records: bool) -> tuple[SourceEvent, ...]:
    source = run.source
    try:
        source.open()
        source.start()
        events = collect(source)
        assert bool(records(events)) == has_records, "unexpected records before failure"
        assert source.state is SourceState.FAILED, "source concealed its failure state"
        health = source.health()
        assert health.terminal_reason == "failed", "failure was reported as successful completion"
        assert health.last_error is not None and health.last_error.message, "failure has no context"
        assert any(
            e.error == health.last_error for e in events if e.kind is SourceEventKind.ERROR
        ), "failed run did not deliver the error reported in health"
        assert events[-1].kind is SourceEventKind.END_OF_STREAM and events[-1].reason == "failed", (
            "failed run did not end with a failed EOS"
        )
        return events
    finally:
        assert_closed(run)


def assert_cancelled(run: AdapterRun) -> None:
    source = run.source
    source.stop()
    assert source.join(DEADLINE), "stop did not release the worker within its deadline"
    assert source.state is SourceState.STOPPED, "cancelled source must report STOPPED"
    assert source.health().terminal_reason == "cancelled", (
        "stopped stream claimed natural completion"
    )
    assert_closed(run)
