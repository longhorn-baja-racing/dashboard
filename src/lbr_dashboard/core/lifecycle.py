"""Cancellation, immutable event handoff, and worker lifecycle primitives."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from threading import Event, Lock, Thread
from types import MappingProxyType
from typing import Any, TypeAlias


class CancellationRequested(Exception):
    """Raised by cooperative work after its owner requests cancellation."""


class CancellationToken:
    """Thread-safe, cooperative cancellation signal."""

    def __init__(self) -> None:
        self._event = Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled:
            raise CancellationRequested("Background work was cancelled")


def _freeze(value: object) -> Any:
    if value is None or isinstance(value, (bool, int, float, str, bytes)):
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("Service event payload keys must be strings")
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    raise TypeError(f"Unsupported mutable event payload value: {type(value).__name__}")


@dataclass(frozen=True, slots=True)
class ServiceEvent:
    """Immutable event queued by worker code for consumption by its owner."""

    kind: str
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not self.kind:
            raise ValueError("Service event kind must be a non-empty string")
        frozen = _freeze(self.payload)
        if not isinstance(frozen, Mapping):
            raise TypeError("Service event payload must be a mapping")
        object.__setattr__(self, "payload", frozen)


@dataclass(frozen=True, slots=True)
class ServiceFailure:
    """Immutable failure details exposed to the owning application thread."""

    exception_type: str
    message: str


Work: TypeAlias = Callable[[CancellationToken, Callable[[ServiceEvent], None]], None]


class BackgroundService:
    """Own one worker thread, event queue, cancellation token, and cleanup."""

    def __init__(self, work: Work) -> None:
        self._work = work
        self._cancel = CancellationToken()
        self._events: list[ServiceEvent] = []
        self._lock = Lock()
        self._thread: Thread | None = None
        self._failure: ServiceFailure | None = None
        self._finished = Event()

    def start(self) -> None:
        """Start this service once."""

        with self._lock:
            if self._thread is not None:
                raise RuntimeError("Background service has already been started")
            self._thread = Thread(target=self._run, name="lbr-dashboard-worker", daemon=True)
            self._thread.start()

    def cancel(self) -> None:
        """Request cooperative cancellation."""

        self._cancel.cancel()

    def join(self, timeout: float | None = None) -> bool:
        """Wait for cleanup and report whether the worker finished."""

        if self._thread is None:
            return True
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def drain_events(self) -> tuple[ServiceEvent, ...]:
        """Return queued events; call from the owning/application thread."""

        with self._lock:
            events = tuple(self._events)
            self._events.clear()
            return events

    @property
    def failure(self) -> ServiceFailure | None:
        """Return worker failure details to the owner after or during join."""

        return self._failure

    @property
    def is_finished(self) -> bool:
        return self._finished.is_set()

    def _emit(self, event: ServiceEvent) -> None:
        with self._lock:
            self._events.append(event)

    def _run(self) -> None:
        try:
            self._work(self._cancel, self._emit)
        except CancellationRequested:
            pass
        except Exception as exc:
            self._failure = ServiceFailure(type(exc).__name__, str(exc))
        finally:
            self._finished.set()
