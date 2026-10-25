# Telemetry source contract

Issue #20 defines a Qt-free `TelemetrySource` protocol and typed events in
`lbr_dashboard.core.contracts`, with a usable `IteratorSource` implementation in
`core.sources`. This replaces the earlier minimal callback-only protocol; no
production adapter used that earlier interface. The legacy CSV UI continues to
use its importer/controller. CSV mapping (#31), simulator scenarios (#23), session
ingestion (#21), replay controls (#22), and the reusable conformance toolkit (#111)
remain separate tasks. No binary protocol or selected hardware is required.

## Owner and worker

- `open(cancellation=None)` prepares a cycle without performing adapter I/O. It
  is idempotent while open. A pre-cancelled token produces a stopped cycle without
  calling the factory. Drain the old queue before reopening so unread events
  cannot disappear. Reopening creates a new queue and resets cycle health.
- `start(cancellation=None)` starts one owned worker and returns immediately.
  The worker calls the adapter factory, validates and publishes descriptors, then
  reads its iterator. Opening and start cancellation are both linked to the same
  token the factory receives. Concurrent/repeated starts are rejected.
- `stop()` requests cooperative cancellation and closes publication immediately;
  it does not block waiting for a consumer or adapter cleanup.
- `join(timeout=1)` waits for that worker without cancelling it. `close(timeout=1)`
  requests stop and joins. Both return a boolean; **false means the worker has not
  finished and resources must not be assumed released**. Calls from the worker
  never self-join. `close` is repeatable and retains error/loss/terminal details.

Timeouts are finite numbers between zero and 86400 seconds. No threading wait in
the queue or close path is indefinite. Adapter I/O and cleanup run on the worker,
including the iterator's `close()`/`finally`. Adapters must use bounded I/O, check
cancellation, and release every resource there. Python cannot safely kill an
arbitrary blocked adapter: a noncooperative factory, read or cleanup makes close
return false at its deadline. The worker is a daemon as a final process-exit
safeguard, not a substitute for successful cleanup.

The application owner drains `source.events` on its own thread (for example using
an application-owned Qt timer), updates the session model, then presents values
to widgets. The source never calls a widget or an owner-provided callback.
`EventSink` remains the separate generic `BackgroundService` event alias; sources
do not use that older unbounded service queue.

## Identity and typed data

Every `SourceEvent` carries an explicit source ID. Record events additionally
carry a stable signal ID and immutable `SignalSample`. Descriptors must belong to
the source, IDs must exist in its catalog, and samples must satisfy descriptor
type/shape, source and timestamp-domain validation. Original values, integer
nanoseconds, units, quality, unavailable nulls and record order are preserved;
neither source nor queue normalizes time or fabricates values. Missing sample
source identity resolves through its identified event and descriptor, as in #18.

The iterator yields record, health, or error events. Its catalog is fixed for an
open cycle; descriptor events are emitted automatically from `source.schema`.
Input descriptor/end events are rejected to avoid competing catalogs or premature
terminal state. Unknown signal IDs or mismatched source/type/domain records become
observable failures instead of entering widgets. Unrelated signal families use
the same implementation without source-specific widget code.

`SourceCapabilities` reports live/finite mode, seek, rate control and queue
capacity. `IteratorSource` truthfully reports seek/rate control as unsupported;
both methods raise `UnsupportedSourceOperation`. It does not own a playback
clock. A future adapter may support those operations through this same protocol,
but must advertise and implement its actual behavior.

## Backpressure, loss and terminal state

`SourceEventQueue` bounds the number of queued events. Payload byte-size limits
belong to each explicit adapter. Publication returns true on delivery and false
on timeout, cancellation or close. A default zero timeout is nonblocking; the
concrete source uses a configurable finite timeout (default 50 ms) per event.
Records/health may be dropped when consumers lag. Controls may evict an older
record/health event, but cannot evict another control. A queue full of controls
honors its timeout too. `close()` wakes waiting publishers and retains existing
events for draining.

A finite queue cannot promise unlimited lossless control events with no consumer.
Instead, rejected controls are counted; descriptors remain in `source.schema`,
and `source.health()` retains final state, terminal reason, latest immutable error,
and dropped-record/rejected-control counters. Read these even when no end/error
event was delivered. Queue stats separately count dropped health messages so they
cannot be confused with lost measurements. Published records are never replaced
with zero or synthetic unavailable samples after loss.

Finite exhaustion reports `complete`; cancellation reports `cancelled`; failures
report `failed`. Factory/open, thread-start, read/validation, fatal adapter and
cleanup errors remain available through health. Recoverable errors can be emitted
without terminating the stream. If reading and cleanup both fail, retain the
original read error. Queues close after terminal publication, even on failure.

## Small explicit adapter example

```python
from lbr_dashboard.core import (
    IteratorSource,
    SignalDescriptor,
    SignalSample,
    SignalSchema,
    SourceEvent,
    SourceEventKind,
)

descriptor = SignalDescriptor(
    "bench.pressure",
    "Pressure",
    ("bench",),
    "numeric",
    "example",
    canonical_unit="Pa",
    timestamp_domain="device_monotonic",
)


def records(cancel):
    # An actual adapter decodes its documented file/stream here and owns cleanup.
    for timestamp_ns, value in ((0, 0), (10_000_000, None), (20_000_000, 7)):
        cancel.raise_if_cancelled()
        yield SourceEvent(
            SourceEventKind.RECORD,
            "example",
            signal_id=descriptor.identifier,
            sample=SignalSample(
                timestamp_ns,
                value,
                available=value is not None,
                source_id="example",
                unit="Pa",
                timestamp_domain="device_monotonic",
            ),
        )


source = IteratorSource("example", SignalSchema(1, (descriptor,)), records)
source.open()
source.start()
assert source.join(1)
events = source.events.drain()
assert source.health().terminal_reason == "complete"
assert source.close()
```

Tests exercise the production class with finite replay-like inputs, mixed
numeric/boolean/status sources, cooperative live streams, stalled consumers,
cancelled opening, bad records, resource failures and deliberately noncooperative
adapters. `tests/ui/test_source_workflow.py` verifies owner-thread polling, real
trace values, missing versus zero, a responsive Qt event loop and worker cleanup.
