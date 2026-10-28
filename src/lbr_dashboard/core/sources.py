"""Concrete in-process source for explicit adapter-produced event iterators."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from threading import Event, RLock, Thread, current_thread

from .contracts import (
    SourceCapabilities,
    SourceError,
    SourceEvent,
    SourceEventKind,
    SourceEventQueue,
    SourceHealth,
    SourceState,
    UnsupportedSourceOperation,
    finite_timeout,
)
from .lifecycle import CancellationRequested, CancellationToken
from .signals import SignalSchema, _validate_identifier


class _RunCancellation(CancellationToken):
    def __init__(self, parent: CancellationToken | None = None) -> None:
        super().__init__()
        self.parent = parent
        self.additional: CancellationToken | None = None

    @property
    def is_cancelled(self) -> bool:
        return (
            super().is_cancelled
            or (self.parent is not None and self.parent.is_cancelled)
            or (self.additional is not None and self.additional.is_cancelled)
        )


class _ReportedFailure(Exception):
    def __init__(self, error: SourceError) -> None:
        super().__init__(error.message)
        self.error = error


class IteratorSource:
    """Finite or live source with explicit schema and cooperative iterator factory.

    Open/start are nonblocking; one owned worker runs the factory and iterator.
    The factory receives cancellation and owns format
    decoding; it must use bounded I/O/waits and release resources in its iterator's
    close/finally. This source owns one bounded queue per open cycle. No arbitrary
    callback runs on the owner. Close reports False if cleanup exceeds its deadline.
    """

    def __init__(
        self,
        source_id: str,
        schema: SignalSchema,
        factory: Callable[[CancellationToken], Iterator[SourceEvent]],
        *,
        is_live: bool = False,
        capacity: int = 128,
        publish_timeout: float = 0.05,
    ) -> None:
        self._source_id = _validate_identifier(source_id, "source_id")
        if not isinstance(schema, SignalSchema):
            raise TypeError("schema must be SignalSchema")
        if any(item.source_id != source_id for item in schema.signals):
            raise ValueError("all descriptors must belong to this source_id")
        if not callable(factory):
            raise TypeError("factory must be callable")
        self._schema = schema
        self._descriptors = {item.identifier: item for item in schema.signals}
        self._factory = factory
        self._capabilities = SourceCapabilities(is_live, False, False, capacity)
        self._timeout = finite_timeout(publish_timeout)
        self._events = SourceEventQueue(capacity)
        self._state = SourceState.CREATED
        self._lock = RLock()
        self._finished = Event()
        self._finished.set()
        self._cancel = _RunCancellation()
        self._iterator: Iterator[SourceEvent] | None = None
        self._thread: Thread | None = None
        self._error: SourceError | None = None
        self._terminal: str | None = None

    @property
    def source_id(self) -> str:
        return self._source_id

    @property
    def schema(self) -> SignalSchema:
        return self._schema

    @property
    def events(self) -> SourceEventQueue:
        return self._events

    @property
    def capabilities(self) -> SourceCapabilities:
        return self._capabilities

    @property
    def state(self) -> SourceState:
        with self._lock:
            return self._state

    def open(self, cancellation: CancellationToken | None = None) -> None:
        with self._lock:
            if self._state is SourceState.OPEN:
                return
            if not self._finished.is_set():
                raise RuntimeError("source is already opening or running")
            if self._events.stats.buffered:
                raise RuntimeError("drain previous events before reopening")
            self._events = SourceEventQueue(self.capabilities.buffer_capacity)
            self._cancel = _RunCancellation(cancellation)
            self._state = SourceState.OPENING
            self._error = None
            self._terminal = None
            if self._cancel.is_cancelled:
                self._terminal = "cancelled"
                self._state = SourceState.STOPPED
                self._events.close()
            else:
                self._state = SourceState.OPEN

    def start(self, cancellation: CancellationToken | None = None) -> None:
        with self._lock:
            if self._state is not SourceState.OPEN:
                raise RuntimeError("open the source before start")
            self._state = SourceState.OPENING
            self._finished.clear()
            self._cancel.additional = cancellation
            try:
                self._thread = Thread(
                    target=self._run, name=f"telemetry-{self.source_id}", daemon=True
                )
                self._thread.start()
            except Exception as error:
                self._thread = None
                self._error = SourceError("start_failed", str(error) or type(error).__name__)
                self._terminal = "failed"
                self._state = SourceState.FAILED
                self._events.publish(
                    SourceEvent(SourceEventKind.ERROR, self.source_id, error=self._error)
                )
                self._events.publish(
                    SourceEvent(SourceEventKind.END_OF_STREAM, self.source_id, reason="failed")
                )
                self._events.close()
                self._finished.set()
                raise

    def _run(self) -> None:
        failure = None
        reason = "complete"
        try:
            self._cancel.raise_if_cancelled()
            candidate = self._factory(self._cancel)
            if not isinstance(candidate, Iterator):
                raise TypeError("factory must return an iterator")
            self._iterator = candidate
            self._cancel.raise_if_cancelled()
            with self._lock:
                self._state = SourceState.RUNNING
            for descriptor in self.schema.signals:
                self._publish(
                    SourceEvent(SourceEventKind.DESCRIPTOR, self.source_id, descriptor=descriptor)
                )
            assert self._iterator is not None
            while True:
                self._cancel.raise_if_cancelled()
                try:
                    event = next(self._iterator)
                except StopIteration:
                    self._cancel.raise_if_cancelled()
                    break
                self._cancel.raise_if_cancelled()
                self._validate(event)
                self._publish(event)
        except CancellationRequested:
            reason = "cancelled"
        except _ReportedFailure as error:
            reason, failure = "failed", error.error
        except Exception as error:
            reason = "failed"
            failure = SourceError(
                "open_failed" if self._iterator is None else "read_failed",
                str(error) or type(error).__name__,
            )
        finally:
            self._finish(reason, failure)

    def _validate(self, event: SourceEvent) -> None:
        if not isinstance(event, SourceEvent) or event.source_id != self.source_id:
            raise ValueError("iterator must yield events with this source_id")
        if event.kind is SourceEventKind.RECORD:
            assert event.sample is not None
            if event.signal_id not in self._descriptors:
                raise ValueError(f"unknown signal_id: {event.signal_id}")
            self._descriptors[event.signal_id].validate_sample(event.sample)
        elif event.kind is SourceEventKind.ERROR:
            assert event.error is not None
            with self._lock:
                self._error = event.error
            if not event.error.recoverable:
                raise _ReportedFailure(event.error)
        elif event.kind is not SourceEventKind.HEALTH:
            raise ValueError("iterator may yield only record, health or error events")

    def _publish(self, event: SourceEvent) -> None:
        self.events.publish(event, timeout=self._timeout, cancellation=self._cancel)

    def _finish(self, reason: str, failure: SourceError | None = None) -> None:
        iterator, self._iterator = self._iterator, None
        try:
            closer = getattr(iterator, "close", None)
            if closer is not None:
                closer()
        except Exception as error:
            if failure is None:
                failure = SourceError("close_failed", str(error) or type(error).__name__)
            reason = "failed"
        with self._lock:
            if failure is not None:
                self._error = failure
            self._terminal = reason

        if failure is not None:
            self._publish(SourceEvent(SourceEventKind.ERROR, self.source_id, error=failure))
        self._publish(SourceEvent(SourceEventKind.END_OF_STREAM, self.source_id, reason=reason))
        self.events.close()
        with self._lock:
            self._state = (
                SourceState.FAILED
                if reason == "failed"
                else SourceState.ENDED
                if reason == "complete"
                else SourceState.STOPPED
            )
            self._finished.set()

    def stop(self) -> None:
        with self._lock:
            self._cancel.cancel()
            self.events.close()
            active = not self._finished.is_set()
            if active:
                self._state = SourceState.STOPPING
            elif self._state is SourceState.OPEN:
                self._terminal = "cancelled"
                self._state = SourceState.STOPPED

    def join(self, timeout: float = 1.0) -> bool:
        """Wait for the worker without requesting cancellation; never join self."""
        timeout = finite_timeout(timeout)
        with self._lock:
            thread = self._thread
        if thread is current_thread():
            return False
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def close(self, timeout: float = 1.0) -> bool:
        timeout = finite_timeout(timeout)
        self.stop()
        if not self.join(timeout):
            return False
        with self._lock:
            self._state = SourceState.CLOSED
        return True

    def seek(self, timestamp_ns: int) -> None:
        raise UnsupportedSourceOperation("iterator source does not support seek")

    def set_rate(self, rate_hz: float) -> None:
        raise UnsupportedSourceOperation("iterator source does not support rate control")

    def health(self) -> SourceHealth:
        with self._lock:
            stats = self.events.stats
            return SourceHealth(
                self._state,
                stats.buffered_records,
                stats.dropped_records,
                self._error,
                stats.rejected_controls,
                self._terminal,
            )
