"""Framework-neutral contracts and lifecycle primitives."""

from .contracts import (
    EventSink,
    SourceCapabilities,
    SourceError,
    SourceEvent,
    SourceEventKind,
    SourceEventQueue,
    SourceHealth,
    SourceQueueStats,
    SourceState,
    TelemetrySource,
    UnsupportedSourceOperation,
)
from .lifecycle import (
    BackgroundService,
    CancellationRequested,
    CancellationToken,
    ServiceEvent,
    ServiceFailure,
)
from .log import LogImporter, LogSession, LogSessionStore
from .registry import FactoryError, Registry, RegistryError
from .session import RejectedSourceBatch, SessionRecord, SessionSnapshot, TelemetrySession
from .session_clock import ClockSnapshot, ClockSubscriberError, PlaybackMode, SessionClock
from .signals import (
    KNOWN_SIGNAL_TYPES,
    SignalAvailability,
    SignalDescriptor,
    SignalRegistry,
    SignalSample,
    SignalSchema,
    SignalSchemaError,
    SignalSeries,
)
from .sources import IteratorSource

__all__ = [
    "ClockSnapshot",
    "ClockSubscriberError",
    "PlaybackMode",
    "SessionClock",
    "RejectedSourceBatch",
    "SessionRecord",
    "SessionSnapshot",
    "TelemetrySession",
    "IteratorSource",
    "SourceCapabilities",
    "SourceError",
    "SourceEvent",
    "SourceEventKind",
    "SourceEventQueue",
    "SourceHealth",
    "SourceQueueStats",
    "SourceState",
    "UnsupportedSourceOperation",
    "BackgroundService",
    "CancellationRequested",
    "CancellationToken",
    "EventSink",
    "FactoryError",
    "LogImporter",
    "LogSession",
    "LogSessionStore",
    "Registry",
    "RegistryError",
    "ServiceEvent",
    "ServiceFailure",
    "TelemetrySource",
    "KNOWN_SIGNAL_TYPES",
    "SignalAvailability",
    "SignalDescriptor",
    "SignalRegistry",
    "SignalSample",
    "SignalSchema",
    "SignalSchemaError",
    "SignalSeries",
]
