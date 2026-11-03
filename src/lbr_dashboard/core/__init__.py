"""Framework-neutral contracts and lifecycle primitives."""

from .contracts import EventSink, TelemetrySource
from .lifecycle import (
    BackgroundService,
    CancellationRequested,
    CancellationToken,
    ServiceEvent,
    ServiceFailure,
)
from .log import LogImporter, LogSession, LogSessionStore
from .registry import FactoryError, Registry, RegistryError

__all__ = [
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
]
