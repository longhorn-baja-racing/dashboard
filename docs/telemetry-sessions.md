# Mixed-rate telemetry sessions

Issue #21 adds the Qt-free `TelemetrySession`, `SessionRecord` and
`SessionSnapshot` types in `lbr_dashboard.core`. The store uses the #18 signal
schema/sample types and accepts #20 source events. It is one in-memory storage
implementation, independent of widgets, file formats and the future replay clock.
The existing CSV table/controller continues to work as before; CSV-to-signal
mapping and production source-selection/replay UI remain separate integrations.

## Append and provenance

Construct a session with an immutable `SignalSchema` and optional JSON metadata.
`append(signal_id, sample)` and `extend(signal_id, samples)` retain the original
sample objects, values, units, quality, availability and integer nanoseconds.
`ingest(events, source_health=...)` accepts a bounded batch of typed source events.
Any invalid descriptor, ID, source, value or clock domain rejects the entire batch
without changing records, health, domain bindings, revision or retention counts.

Each stored `SessionRecord` also contains the resolved signal/source ID and a
monotonically increasing session arrival `sequence`. A sample without an explicit
source ID resolves through its event/descriptor without modifying the raw sample.
Signals keep independent counts and times: a 100 Hz pressure signal does not pad
a 1 Hz temperature or irregular boolean/event signal into synchronized rows.
Event/status payloads use the same sparse record storage as other signal types.

Queries order measurements by timestamp, then arrival. Late/out-of-order records
are accepted; equal timestamps are retained independently. `arrival_order` on a
snapshot recovers source delivery order. A failed batch does not advance arrival
sequence. Late records evicted by the retention policy still count as arrivals
and are included in the eviction counter.

## Clock and gap policy

Queries use each signal's recorded timestamp domain. No origin is subtracted and
no device clock is silently converted to wall time or reception time. Signed and
large integer timestamps retain exact precision. A descriptor with an explicit
domain rejects mismatches through #18 validation. An `unspecified` descriptor
binds to its first sample's domain; later samples for that signal must use the
same domain. A different clock epoch/domain requires a separate session or signal
identity, rather than silently mixing incomparable timestamps.

Per-signal bounds do not claim synchronization between devices. An adapter must
provide a documented mapping before its values can be compared on one normalized
clock; this store does not invent that mapping. Raw timestamps remain available
in every record even when a presentation layer displays milliseconds.

- `range(signal_id, start_ns, end_ns)` returns original records in the inclusive
  interval `[start_ns, end_ns]`. A reversed interval is an error; an empty range
  returns an empty tuple. An unknown signal raises `KeyError`.
- `nearest(signal_id, timestamp_ns, max_distance_ns=None)` returns the closest
  original record, or `None` for an empty series. Equal distances choose the
  earlier timestamp; duplicates at that timestamp choose the last arrival.
- A supplied nonnegative `max_distance_ns` rejects results farther away than the
  bound, including when the closest point lies outside the requested gap.
  Without that parameter, nearest means literal nearest, with no inferred
  stale-time threshold.
- Missing/unavailable records are returned as missing, never skipped in favor of
  an available neighbor or converted to zero. A recorded zero or false remains
  an available value. Unobserved gaps have no synthetic records.
- There is no interpolation, resampling, forward fill or categorical state
  inference. Consumers inspect availability/quality and choose an explicit
  distance/display policy. `bounds(signal_id)` returns retained minimum/maximum
  timestamps or `None` for an empty signal.

## Consistent snapshots

`snapshot()` captures all signals, metadata, clock domains, source health, recent
control events and per-signal eviction counters at one revision under a lock.
Snapshots and their nested containers are immutable and stay unchanged after
append, retention or further source ingestion. Use one snapshot for a group of
widget queries so they all see the same revision. Individual direct session
queries are locked and safe, but separate calls may observe different revisions.

Direct range/nearest queries use binary search on the selected signal, without
copying unrelated signals. A range copies only its result. Explicit snapshots
copy all retained record references; callers should release snapshots they no
longer need. Keeping many historical snapshots intentionally retains their data
outside the live store's configured bound.

## Live retention and source ingestion

Replay storage is unbounded by default. Live ingestion through `ingest_source`
requires a positive `max_samples_per_signal`; a time window alone cannot bound
duplicate/high-rate records. The same query API serves either mode.

Retention is applied atomically per signal on every batch:

1. Optional `retention_ns` drops timestamps older than that signal's latest
   timestamp minus the window. The cutoff is inclusive; zero retains records at
   the latest timestamp only.
2. The count cap drops the oldest remaining records, keeping the latest arrivals
   when duplicate timestamps must be trimmed.

A fast signal cannot advance a slow signal's retention clock. Late samples older
than the retained window are counted as evicted instead of changing its bounds.
The snapshot's `evicted` counters distinguish deliberate storage retention from
upstream dropped records in `source_health`.

`ingest_source(source, limit=None)` is an owner-thread convenience: it checks the
source/catalog compatibility before draining, drains at most `max_batch_events`,
and stores persistent source health even if error/end events could not fit in
the source queue. An explicit `limit=0` updates health without consuming events.
Only one owner should drain a source. If a drained batch fails session admission,
`RejectedSourceBatch.events` retains the original events and `__cause__` explains
the failure; the store remains unchanged. These events are not silently requeued
or discarded by the session.

The default `max_batch_events` is 10,000; oversized iterators are stopped after
that limit plus one and rejected atomically. Recent descriptor/health/error/end
events are bounded by `max_source_events` (default 64). Latest health, error and
terminal details remain separately queryable. Explicit source-health snapshots
take precedence over older health/end messages in the drained batch. Without
one, incoming health/error/end events update health in delivery order.

## Example

```python
from lbr_dashboard.core import SignalDescriptor, SignalSample, SignalSchema, TelemetrySession

descriptor = SignalDescriptor(
    "bench.pressure",
    "Pressure",
    ("bench",),
    "numeric",
    "example",
    canonical_unit="Pa",
    timestamp_domain="relative",
)
session = TelemetrySession(SignalSchema(1, (descriptor,)), max_samples_per_signal=100)
session.extend(
    "bench.pressure",
    [
        SignalSample(0, 0, timestamp_domain="relative", unit="Pa"),
        SignalSample(10_000_000, None, available=False, timestamp_domain="relative", unit="Pa"),
        SignalSample(20_000_000, 8, timestamp_domain="relative", unit="Pa"),
    ],
)
view = session.snapshot()
assert len(view.range("bench.pressure", 0, 20_000_000)) == 3
missing = view.nearest("bench.pressure", 10_000_000)
assert missing is not None and not missing.sample.available
assert view.nearest("bench.pressure", 1_000_000_000, max_distance_ns=1_000_000) is None
```

`tests/core/test_session.py` covers query/retention/provenance and synchronized
writer/reader snapshots. `tests/ui/test_session_workflow.py` runs a real source,
owner timer, bounded session and plot, including a frozen snapshot after append.
