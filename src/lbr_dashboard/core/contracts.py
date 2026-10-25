"""Typed source events and bounded, cancellable worker-to-owner handoff."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from math import isfinite
from threading import Condition
from time import monotonic
from typing import Any, Protocol, TypeAlias

from .lifecycle import CancellationToken, ServiceEvent
from .signals import (
    SignalDescriptor,
    SignalSample,
    SignalSchema,
    _freeze_mapping,
    _validate_identifier,
)

EventSink: TypeAlias = Callable[[ServiceEvent], None]


class SourceState(str, Enum):
    """Lifecycle states shared by replay, live, and simulated sources."""

    CREATED = "created"
    OPENING = "opening"
    OPEN = "open"
    RUNNING = "running"
    ENDED = "ended"
    STOPPING = "stopping"
    STOPPED = "stopped"
    CLOSED = "closed"
    FAILED = "failed"


class UnsupportedSourceOperation(RuntimeError):
    """Raised when a caller requests an operation a source does not support."""


class SourceEventKind(str, Enum):
    """Normalized events a source can deliver to downstream consumers."""

    DESCRIPTOR = "descriptor"
    RECORD = "record"
    HEALTH = "health"
    ERROR = "error"
    END_OF_STREAM = "end_of_stream"


@dataclass(frozen=True, slots=True)
class SourceCapabilities:
    """Truthful source capabilities; unsupported controls must not be implied."""

    is_live: bool
    supports_seek: bool
    supports_rate_control: bool
    buffer_capacity: int

    def __post_init__(self) -> None:
        if not isinstance(self.is_live, bool):
            raise TypeError("is_live must be a boolean")
        if not isinstance(self.supports_seek, bool):
            raise TypeError("supports_seek must be a boolean")
        if not isinstance(self.supports_rate_control, bool):
            raise TypeError("supports_rate_control must be a boolean")
        if (
            isinstance(self.buffer_capacity, bool)
            or not isinstance(self.buffer_capacity, int)
            or self.buffer_capacity <= 0
        ):
            raise ValueError("buffer_capacity must be a positive integer")


@dataclass(frozen=True, slots=True)
class SourceError:
    """Observable source failure context safe to cross a thread boundary."""

    code: str
    message: str
    recoverable: bool = False
    context: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not self.code.strip():
            raise ValueError("source error code must not be empty")
        if not isinstance(self.message, str) or not self.message.strip():
            raise ValueError("source error message must not be empty")
        if not isinstance(self.recoverable, bool):
            raise TypeError("recoverable must be a boolean")
        object.__setattr__(self, "context", _freeze_mapping(self.context, "error context"))


@dataclass(frozen=True, slots=True)
class SourceHealth:
    """Point-in-time health and loss metrics for a source."""

    state: SourceState
    buffered_records: int = 0
    dropped_records: int = 0
    last_error: SourceError | None = None
    rejected_controls: int = 0
    terminal_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, SourceState):
            raise TypeError("state must be SourceState")
        if self.last_error is not None and not isinstance(self.last_error, SourceError):
            raise TypeError("last_error must be SourceError")
        if self.terminal_reason is not None and (
            not isinstance(self.terminal_reason, str) or not self.terminal_reason.strip()
        ):
            raise ValueError("terminal_reason must be nonempty text")
        for name, value in (
            ("buffered_records", self.buffered_records),
            ("dropped_records", self.dropped_records),
            ("rejected_controls", self.rejected_controls),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class SourceEvent:
    """A typed source event; records always pair a stable ID with their sample."""

    kind: SourceEventKind
    source_id: str
    descriptor: SignalDescriptor | None = None
    signal_id: str | None = None
    sample: SignalSample | None = None
    health: SourceHealth | None = None
    error: SourceError | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, SourceEventKind):
            raise TypeError("kind must be a SourceEventKind")
        _validate_identifier(self.source_id, "event source_id")
        payloads = (self.descriptor, self.sample, self.health, self.error, self.reason)
        expected = {
            SourceEventKind.DESCRIPTOR: self.descriptor,
            SourceEventKind.RECORD: self.sample,
            SourceEventKind.HEALTH: self.health,
            SourceEventKind.ERROR: self.error,
            SourceEventKind.END_OF_STREAM: self.reason,
        }[self.kind]
        if expected is None or sum(value is not None for value in payloads) != 1:
            raise ValueError(f"{self.kind.value} events must contain exactly one matching payload")
        if self.kind is SourceEventKind.RECORD:
            if not isinstance(self.signal_id, str) or not self.signal_id:
                raise ValueError("record events require a signal_id")
            _validate_identifier(self.signal_id, "signal_id")
            if not isinstance(self.sample, SignalSample):
                raise TypeError("record requires SignalSample")
            if self.sample.source_id is not None and self.sample.source_id != self.source_id:
                raise ValueError("sample and event source_id differ")
        elif self.signal_id is not None:
            raise ValueError("signal_id is only valid for record events")
        if self.kind is SourceEventKind.DESCRIPTOR:
            if not isinstance(self.descriptor, SignalDescriptor):
                raise TypeError("descriptor requires SignalDescriptor")
            if self.descriptor.source_id != self.source_id:
                raise ValueError("descriptor and event source_id differ")
        if self.kind is SourceEventKind.HEALTH and not isinstance(self.health, SourceHealth):
            raise TypeError("health requires SourceHealth")
        if self.kind is SourceEventKind.ERROR and not isinstance(self.error, SourceError):
            raise TypeError("error requires SourceError")
        if self.kind is SourceEventKind.END_OF_STREAM and (
            not isinstance(self.reason, str) or not self.reason.strip()
        ):
            raise ValueError("end requires a nonempty reason")


def finite_timeout(value: float) -> float:
    """Validate finite, bounded waits before passing them to threading APIs."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("timeout must be a finite nonnegative number")
    try:
        valid = isfinite(value) and 0 <= value <= 86400
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("timeout must be finite and between zero and 86400 seconds")
    return float(value)


@dataclass(frozen=True, slots=True)
class SourceQueueStats:
    """Immutable loss metrics; rejected control events are separately visible."""

    capacity: int
    buffered: int
    published: int
    buffered_records: int
    dropped_records: int
    dropped_health: int
    rejected_controls: int
    closed: bool


_CONTROL = {SourceEventKind.DESCRIPTOR, SourceEventKind.ERROR, SourceEventKind.END_OF_STREAM}


class SourceEventQueue:
    """Bounded handoff; every publication honors timeout, cancellation and close.

    Controls may evict records/health, but never other controls. If controls fill
    the queue, publication returns False on timeout; callers must retain state.
    """

    def __init__(self, capacity: int) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity <= 0:
            raise ValueError("capacity must be a positive integer")
        self._queue: deque[SourceEvent] = deque()
        self._capacity = capacity
        self._published = self._records = self._health = self._controls = 0
        self._closed = False
        self._condition = Condition()

    def _reject(self, event: SourceEvent) -> None:
        if event.kind in _CONTROL:
            self._controls += 1
        elif event.kind is SourceEventKind.RECORD:
            self._records += 1
        else:
            self._health += 1

    def publish(
        self,
        event: SourceEvent,
        *,
        timeout: float = 0.0,
        cancellation: CancellationToken | None = None,
    ) -> bool:
        """Return False if delivery fails; never hide loss or wait indefinitely."""
        if not isinstance(event, SourceEvent):
            raise TypeError("event must be SourceEvent")
        deadline = monotonic() + finite_timeout(timeout)
        with self._condition:
            while True:
                if self._closed or (cancellation is not None and cancellation.is_cancelled):
                    self._reject(event)
                    return False
                if len(self._queue) < self._capacity:
                    break
                if event.kind in _CONTROL:
                    for index, buffered in enumerate(self._queue):
                        if buffered.kind not in _CONTROL:
                            del self._queue[index]
                            self._reject(buffered)
                            break
                    else:
                        buffered = None
                    if buffered is not None:
                        break
                remaining = deadline - monotonic()
                if remaining <= 0:
                    self._reject(event)
                    return False
                self._condition.wait(min(remaining, 0.02))
            self._queue.append(event)
            self._published += 1
            self._condition.notify_all()
            return True

    def snapshot(self) -> tuple[SourceEvent, ...]:
        with self._condition:
            return tuple(self._queue)

    def close(self) -> None:
        """Reject new publications, wake producers, retain queued events for drain."""
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def drain(self, limit: int | None = None) -> tuple[SourceEvent, ...]:
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit < 0
        ):
            raise ValueError("limit must be a nonnegative integer or None")
        with self._condition:
            count = len(self._queue) if limit is None else min(limit, len(self._queue))
            events = tuple(self._queue.popleft() for _ in range(count))
            self._condition.notify_all()
            return events

    @property
    def stats(self) -> SourceQueueStats:
        with self._condition:
            return SourceQueueStats(
                self._capacity,
                len(self._queue),
                self._published,
                sum(event.kind is SourceEventKind.RECORD for event in self._queue),
                self._records,
                self._health,
                self._controls,
                self._closed,
            )


class TelemetrySource(Protocol):
    """A nonblocking source lifecycle with owner-drained bounded events.

    Descriptors remain recoverable from schema; final state/error/loss remain
    recoverable from health even if the owner stops draining events.
    """

    @property
    def source_id(self) -> str: ...

    @property
    def schema(self) -> SignalSchema: ...

    @property
    def events(self) -> SourceEventQueue: ...

    @property
    def capabilities(self) -> SourceCapabilities: ...

    @property
    def state(self) -> SourceState: ...

    def open(self, cancellation: CancellationToken | None = None) -> None: ...

    def start(self, cancellation: CancellationToken | None = None) -> None: ...

    def stop(self) -> None: ...

    def join(self, timeout: float = 1.0) -> bool: ...

    def close(self, timeout: float = 1.0) -> bool: ...

    def seek(self, timestamp_ns: int) -> None: ...

    def set_rate(self, rate_hz: float) -> None: ...

    def health(self) -> SourceHealth: ...
