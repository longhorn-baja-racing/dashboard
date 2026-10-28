"""Thread-safe sparse session storage with immutable, versioned query snapshots."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from itertools import islice
from threading import RLock
from types import MappingProxyType
from typing import Any

from .contracts import SourceEvent, SourceEventKind, SourceHealth, SourceState, TelemetrySource
from .signals import SignalSample, SignalSchema, _freeze_mapping


def _integer(value: object, name: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """An original sample plus resolved identity and session arrival sequence."""

    signal_id: str
    source_id: str
    sample: SignalSample
    sequence: int


def _time(record: SessionRecord) -> int:
    return record.sample.timestamp_ns


def _range(
    records: Sequence[SessionRecord], start_ns: int, end_ns: int
) -> tuple[SessionRecord, ...]:
    start_ns = _integer(start_ns, "start_ns")
    end_ns = _integer(end_ns, "end_ns")
    if start_ns > end_ns:
        raise ValueError("start_ns must not exceed end_ns")
    return tuple(
        records[
            bisect_left(records, start_ns, key=_time) : bisect_right(records, end_ns, key=_time)
        ]
    )


def _nearest(
    records: Sequence[SessionRecord], timestamp_ns: int, max_distance_ns: int | None
) -> SessionRecord | None:
    timestamp_ns = _integer(timestamp_ns, "timestamp_ns")
    if max_distance_ns is not None:
        max_distance_ns = _integer(max_distance_ns, "max_distance_ns", minimum=0)
    if not records:
        return None
    index = bisect_right(records, timestamp_ns, key=_time)
    candidates = []
    if index:
        candidates.append(records[index - 1])
    if index < len(records):
        # Pick the last arrival even when the nearest time lies in the future.
        last = bisect_right(records, _time(records[index]), key=_time) - 1
        candidates.append(records[last])
    result = min(candidates, key=lambda item: (abs(_time(item) - timestamp_ns), _time(item)))
    if max_distance_ns is not None and abs(_time(result) - timestamp_ns) > max_distance_ns:
        return None
    return result


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    """One consistent revision; subsequent ingestion cannot change these queries."""

    revision: int
    schema: SignalSchema
    metadata: Mapping[str, Any]
    series: Mapping[str, tuple[SessionRecord, ...]]
    domains: Mapping[str, str]
    source_health: Mapping[str, SourceHealth]
    source_events: tuple[SourceEvent, ...]
    evicted: Mapping[str, int]

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _freeze_mapping(self.metadata, "session metadata"))
        object.__setattr__(
            self,
            "series",
            MappingProxyType({key: tuple(records) for key, records in self.series.items()}),
        )
        for field in ("domains", "source_health", "evicted"):
            object.__setattr__(self, field, MappingProxyType(dict(getattr(self, field))))
        object.__setattr__(self, "source_events", tuple(self.source_events))

    def range(self, signal_id: str, start_ns: int, end_ns: int) -> tuple[SessionRecord, ...]:
        """Original records inside inclusive bounds, ordered by time then arrival."""
        return _range(self.series[signal_id], start_ns, end_ns)

    def nearest(
        self,
        signal_id: str,
        timestamp_ns: int,
        *,
        max_distance_ns: int | None = None,
    ) -> SessionRecord | None:
        """Nearest measured record, including unavailable; no interpolation or fill.

        Equal-distance ties choose the earlier time. Equal timestamps choose the
        last arrival. A supplied distance bound prevents looking across a gap.
        """
        return _nearest(self.series[signal_id], timestamp_ns, max_distance_ns)

    def bounds(self, signal_id: str) -> tuple[int, int] | None:
        records = self.series[signal_id]
        return (_time(records[0]), _time(records[-1])) if records else None

    def arrival_order(self, signal_id: str) -> tuple[SessionRecord, ...]:
        """Recover source delivery order without rewriting measurement timestamps."""
        return tuple(sorted(self.series[signal_id], key=lambda item: item.sequence))


class TelemetrySession:
    """One in-memory store for replay and bounded live source ingestion.

    Each signal has one clock domain per session. Cross-signal/device clock
    alignment requires an explicit adapter policy; no timestamps are rebased.
    """

    def __init__(
        self,
        schema: SignalSchema,
        *,
        metadata: Mapping[str, Any] | None = None,
        max_samples_per_signal: int | None = None,
        retention_ns: int | None = None,
        max_source_events: int = 64,
        max_batch_events: int = 10000,
    ) -> None:
        if not isinstance(schema, SignalSchema):
            raise TypeError("schema must be SignalSchema")
        if max_samples_per_signal is not None:
            _integer(max_samples_per_signal, "max_samples_per_signal", minimum=1)
        if retention_ns is not None:
            _integer(retention_ns, "retention_ns", minimum=0)
        self._event_capacity = _integer(max_source_events, "max_source_events", minimum=1)
        self._batch_capacity = _integer(max_batch_events, "max_batch_events", minimum=1)
        self._schema = schema
        self._descriptors = {item.identifier: item for item in schema.signals}
        self._sources = {item.source_id for item in schema.signals}
        self._metadata = _freeze_mapping({} if metadata is None else metadata, "session metadata")
        self._capacity = max_samples_per_signal
        self._retention_ns = retention_ns
        self._series: dict[str, list[SessionRecord]] = {key: [] for key in self._descriptors}
        self._domains: dict[str, str] = {}
        self._health: dict[str, SourceHealth] = {}
        self._events: deque[SourceEvent] = deque(maxlen=self._event_capacity)
        self._evicted = {key: 0 for key in self._descriptors}
        self._sequence = self._revision = 0
        self._lock = RLock()

    @property
    def schema(self) -> SignalSchema:
        return self._schema

    def _batch(self, values: Iterable[Any]) -> tuple[Any, ...]:
        batch = tuple(islice(values, self._batch_capacity + 1))
        if len(batch) > self._batch_capacity:
            raise ValueError(f"batch exceeds {self._batch_capacity} events")
        return batch

    def append(self, signal_id: str, sample: SignalSample) -> None:
        self.extend(signal_id, (sample,))

    def extend(self, signal_id: str, samples: Iterable[SignalSample]) -> None:
        descriptor = self._descriptors[signal_id]
        self.ingest(
            SourceEvent(
                SourceEventKind.RECORD, descriptor.source_id, signal_id=signal_id, sample=sample
            )
            for sample in samples
        )

    def ingest(
        self,
        events: Iterable[SourceEvent],
        *,
        source_health: Mapping[str, SourceHealth] | None = None,
    ) -> int:
        """Commit a bounded batch atomically; invalid input leaves the session intact."""
        batch = self._batch(events)
        health = dict(source_health) if source_health is not None else {}
        explicit_health = set(health)
        for source_id, value in health.items():
            if source_id not in self._sources or not isinstance(value, SourceHealth):
                raise ValueError("health must identify a known source and contain SourceHealth")
        with self._lock:
            pending: dict[str, list[SessionRecord]] = {}
            domains = dict(self._domains)
            pending_events: list[SourceEvent] = []
            sequence = self._sequence
            for event in batch:
                if not isinstance(event, SourceEvent) or event.source_id not in self._sources:
                    raise ValueError("event must identify a known source")
                if event.kind is SourceEventKind.DESCRIPTOR:
                    assert event.descriptor is not None
                    if self._descriptors.get(event.descriptor.identifier) != event.descriptor:
                        raise ValueError("source descriptor differs from the session schema")
                elif event.kind is SourceEventKind.RECORD:
                    assert event.signal_id is not None and event.sample is not None
                    descriptor = self._descriptors[event.signal_id]
                    if event.source_id != descriptor.source_id:
                        raise ValueError("event source differs from signal source")
                    descriptor.validate_sample(event.sample)
                    domain = domains.setdefault(event.signal_id, event.sample.timestamp_domain)
                    if domain != event.sample.timestamp_domain:
                        raise ValueError("one signal cannot mix timestamp domains in a session")
                    if event.signal_id not in pending:
                        pending[event.signal_id] = list(self._series[event.signal_id])
                    records = pending[event.signal_id]
                    sequence += 1
                    record = SessionRecord(event.signal_id, event.source_id, event.sample, sequence)
                    if not records or _time(record) >= _time(records[-1]):
                        records.append(record)
                    else:
                        records.insert(bisect_right(records, _time(record), key=_time), record)
                    continue
                elif event.kind is SourceEventKind.HEALTH:
                    assert event.health is not None
                    # An explicit post-drain health snapshot takes precedence.
                    if event.source_id not in explicit_health:
                        health[event.source_id] = event.health
                elif event.kind in (SourceEventKind.ERROR, SourceEventKind.END_OF_STREAM):
                    if event.source_id not in explicit_health:
                        previous = health.get(
                            event.source_id,
                            self._health.get(event.source_id, SourceHealth(SourceState.RUNNING)),
                        )
                        if event.kind is SourceEventKind.ERROR:
                            assert event.error is not None
                            health[event.source_id] = replace(
                                previous,
                                last_error=event.error,
                                state=previous.state
                                if event.error.recoverable
                                else SourceState.FAILED,
                            )
                        else:
                            state = (
                                SourceState.FAILED
                                if event.reason == "failed"
                                else SourceState.STOPPED
                                if event.reason == "cancelled"
                                else SourceState.ENDED
                            )
                            health[event.source_id] = replace(
                                previous, state=state, terminal_reason=event.reason
                            )
                pending_events.append(event)
            evicted = dict(self._evicted)
            for signal_id, records in pending.items():
                before = len(records)
                if self._retention_ns is not None and records:
                    cutoff = _time(records[-1]) - self._retention_ns
                    del records[: bisect_left(records, cutoff, key=_time)]
                if self._capacity is not None and len(records) > self._capacity:
                    del records[: -self._capacity]
                evicted[signal_id] += before - len(records)
            health_changed = any(self._health.get(key) != value for key, value in health.items())
            if batch or health_changed:
                self._series.update(pending)
                self._domains = domains
                self._sequence = sequence
                self._health.update(health)
                self._events.extend(pending_events)
                self._evicted = evicted
                self._revision += 1
            return sum(event.kind is SourceEventKind.RECORD for event in batch)

    def ingest_source(self, source: TelemetrySource, *, limit: int | None = None) -> int:
        """Drain one known source on its owner thread; also retain persistent health."""
        if source.capabilities.is_live and self._capacity is None:
            raise ValueError("live ingestion requires max_samples_per_signal")
        if source.source_id not in self._sources:
            raise ValueError("source is absent from the session schema")
        for descriptor in source.schema.signals:
            if self._descriptors.get(descriptor.identifier) != descriptor:
                raise ValueError("source descriptor differs from the session schema")
        if limit is not None:
            _integer(limit, "limit", minimum=0)
        count = self._batch_capacity if limit is None else min(limit, self._batch_capacity)
        batch = source.events.drain(count)
        try:
            return self.ingest(batch, source_health={source.source_id: source.health()})
        except Exception as error:
            raise RejectedSourceBatch(batch) from error

    def snapshot(self) -> SessionSnapshot:
        with self._lock:
            return SessionSnapshot(
                self._revision,
                self.schema,
                self._metadata,
                {key: tuple(records) for key, records in self._series.items()},
                dict(self._domains),
                dict(self._health),
                tuple(self._events),
                dict(self._evicted),
            )

    def range(self, signal_id: str, start_ns: int, end_ns: int) -> tuple[SessionRecord, ...]:
        with self._lock:
            return _range(self._series[signal_id], start_ns, end_ns)

    def nearest(
        self,
        signal_id: str,
        timestamp_ns: int,
        *,
        max_distance_ns: int | None = None,
    ) -> SessionRecord | None:
        with self._lock:
            return _nearest(self._series[signal_id], timestamp_ns, max_distance_ns)

    def bounds(self, signal_id: str) -> tuple[int, int] | None:
        with self._lock:
            records = self._series[signal_id]
            return (_time(records[0]), _time(records[-1])) if records else None


class RejectedSourceBatch(ValueError):
    """A source batch failed admission; original drained events remain recoverable."""

    def __init__(self, events: tuple[SourceEvent, ...]) -> None:
        super().__init__("source batch rejected; inspect events and the original cause")
        self.events = events
