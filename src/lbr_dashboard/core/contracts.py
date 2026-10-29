"""Framework-neutral contracts for telemetry sources."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from .lifecycle import CancellationToken, ServiceEvent

EventSink = Callable[[ServiceEvent], None]


class TelemetrySource(Protocol):
    """Live or replayed telemetry source controlled by the application owner."""

    @property
    def source_id(self) -> str: ...

    def start(self, events: EventSink, cancellation: CancellationToken) -> None: ...

    def stop(self) -> None: ...
