"""Feed a ``SourceHealthTracker`` from a running ``TelemetrySource``.

The owner of a source (the application's source controller) creates one
``SourceHealthMonitor`` per started source, hands it every batch of events it
drains and asks it for a ``SourceMonitorReport`` whenever it presents state.
The monitor turns contract information into tracker observations:

- source state OPEN / RUNNING is a connect, any stopping or terminal state a
  disconnect (with a reason in words);
- every RECORD event is one packet arrival, timed on the monotonic clock when
  the batch is handed over; records of a source that declares
  ``sequence_bits`` carry their packet sequence number in
  ``sample.extra["sequence"]``, and consecutive records of different signals
  with the same number are one packet;
- HEALTH events are the source's own status reports, and heartbeats when the
  source declares a heartbeat timeout;
- ERROR events with the code ``decode_error`` are decode errors when the source
  declares that it reports them;
- records the bounded event queue dropped are dashboard drops (overflow).

A source declares the health metadata it provides with an optional
``health_config`` attribute holding a ``SourceHealthConfig``. Without one the
monitor assumes nothing: sequence numbers, heartbeat and decode errors stay
unknown. Queue drops are always known, because every source has a queue.

Like the tracker, the monitor is not thread-safe; use it on the owner thread.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace

from .contracts import SourceEvent, SourceEventKind, SourceHealth, SourceState, TelemetrySource
from .source_health import (
    ReportedValue,
    SourceHealthConfig,
    SourceHealthSnapshot,
    SourceHealthTracker,
    format_duration,
)

SEQUENCE_KEY = "sequence"
"""``SignalSample.extra`` key holding a packet sequence number."""
DECODE_ERROR_CODE = "decode_error"
"""``SourceError.code`` of a recoverable error for bytes that did not decode."""

_CONNECTED = {SourceState.OPEN, SourceState.RUNNING}
_PENDING = {SourceState.CREATED, SourceState.OPENING}
_TERMINAL_REASONS = {"complete": "end of data", "cancelled": "stopped"}


@dataclass(frozen=True, slots=True)
class SignalArrival:
    """Records of one signal that reached the dashboard, by arrival time."""

    signal_id: str
    records: int
    unavailable: int
    """Records that arrived but said the reading is unavailable."""
    last_arrival_ns: int | None


@dataclass(frozen=True, slots=True)
class SourceMonitorReport:
    """Source health plus per-signal arrivals, both at ``health.at_ns``."""

    health: SourceHealthSnapshot
    signals: tuple[SignalArrival, ...]


def declared_config(source: TelemetrySource) -> SourceHealthConfig:
    """Return the metadata ``source`` declares; queue drops are always reported."""

    declared = getattr(source, "health_config", None)
    if not isinstance(declared, SourceHealthConfig):
        declared = SourceHealthConfig(reports_decode_errors=False)
    return replace(declared, reports_overflow=True)


def disconnect_reason(state: SourceState, health: SourceHealth) -> str:
    """Describe why a source is no longer connected, in words."""

    if state is SourceState.FAILED:
        error = health.last_error
        return f"failed: {error.message}" if error is not None else "failed"
    if state in {SourceState.STOPPING, SourceState.STOPPED, SourceState.CLOSED}:
        return "stopped"  # Disconnect, a source switch or window close released it.
    reason = health.terminal_reason
    return _TERMINAL_REASONS.get(reason, reason) if reason else "ended"


def reported_fields(health: SourceHealth) -> dict[str, ReportedValue]:
    """Contract health as the source reported it, for the reported status rows."""

    fields: dict[str, ReportedValue] = {
        "state": health.state.value,
        "buffered_records": health.buffered_records,
        "dropped_records": health.dropped_records,
        "rejected_controls": health.rejected_controls,
    }
    if health.last_error is not None:
        fields["last_error"] = f"{health.last_error.code}: {health.last_error.message}"
    if health.terminal_reason is not None:
        fields["terminal_reason"] = health.terminal_reason
    return fields


class SourceHealthMonitor:
    """Track one source: connection from its state, arrivals from drained events."""

    def __init__(
        self,
        source: TelemetrySource,
        *,
        clock: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        self._source = source
        self._clock = clock
        self._tracker = SourceHealthTracker(source.source_id, declared_config(source), clock=clock)
        self._connected: bool | None = None
        self._queue_dropped = 0
        self._signals: dict[str, list[int]] = {
            item.identifier: [0, 0, -1] for item in source.schema.signals
        }

    @property
    def source(self) -> TelemetrySource:
        return self._source

    @property
    def tracker(self) -> SourceHealthTracker:
        return self._tracker

    def observe_events(self, events: Iterable[SourceEvent]) -> None:
        """Record one drained batch; every event in it arrived now."""

        now = self._clock()
        self._observe_state(now)
        config = self._tracker.config
        packet_sequence: int | None = None
        packet_signals: set[str] = set()
        for event in events:
            if event.source_id != self._source.source_id:
                continue
            if event.kind is SourceEventKind.RECORD:
                assert event.signal_id is not None and event.sample is not None
                self._count_record(event.signal_id, event.sample.available, now)
                sequence = _sequence(event, config.sequence_bits)
                if (
                    sequence is not None
                    and sequence == packet_sequence
                    and event.signal_id not in packet_signals
                ):
                    packet_signals.add(event.signal_id)  # Another signal of the same packet.
                    continue
                packet_sequence, packet_signals = sequence, {event.signal_id}
                self._tracker.observe_packet(now, sequence=sequence)
                continue
            packet_sequence, packet_signals = None, set()
            if event.kind is SourceEventKind.HEALTH:
                assert event.health is not None
                if config.heartbeat_timeout_ns is not None:
                    self._tracker.observe_heartbeat(now)
                self._tracker.observe_reported_status(reported_fields(event.health), now)
            elif (
                event.kind is SourceEventKind.ERROR
                and config.reports_decode_errors
                and event.error is not None
                and event.error.code == DECODE_ERROR_CODE
            ):
                self._tracker.observe_decode_error(now, message=event.error.message)
        self._observe_queue(now)

    def report(self) -> SourceMonitorReport:
        """Observe the current state and return health as of now."""

        now = self._clock()
        self._observe_state(now)
        self._observe_queue(now)
        signals = tuple(
            SignalArrival(identifier, count, unavailable, None if last < 0 else last)
            for identifier, (count, unavailable, last) in self._signals.items()
        )
        return SourceMonitorReport(self._tracker.snapshot(now), signals)

    def _observe_state(self, now: int) -> None:
        state = self._source.state
        if state in _PENDING:
            return
        connected = state in _CONNECTED
        if connected == self._connected:
            return
        self._connected = connected
        if connected:
            self._tracker.observe_connect(now)
        else:
            reason = disconnect_reason(state, self._source.health())
            self._tracker.observe_disconnect(now, reason=reason)

    def _observe_queue(self, now: int) -> None:
        dropped = self._source.events.stats.dropped_records
        # A stopped source's queue rejects what the worker still sends; that is not loss.
        if dropped > self._queue_dropped and self._connected is not False:
            self._tracker.observe_overflow(dropped - self._queue_dropped, now)
        self._queue_dropped = dropped

    def _count_record(self, signal_id: str, available: bool, now: int) -> None:
        entry = self._signals.setdefault(signal_id, [0, 0, -1])
        entry[0] += 1
        entry[1] += 0 if available else 1
        entry[2] = now


def _sequence(event: SourceEvent, bits: int | None) -> int | None:
    """Return the record's packet number, or None when absent or out of range."""

    assert event.sample is not None
    if bits is None:
        return None
    value = event.sample.extra.get(SEQUENCE_KEY)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 1 << bits:
        return None
    return value


def signal_arrival_rows(report: SourceMonitorReport) -> list[tuple[str, str]]:
    """ASCII rows for per-signal arrivals, judged with the source thresholds."""

    health = report.health
    rows = []
    for item in report.signals:
        if item.last_arrival_ns is None:
            rows.append((f"Signal {item.signal_id}", "no records received"))
            continue
        age = health.at_ns - item.last_arrival_ns
        state = (
            "stale"
            if age > health.stale_after_ns
            else "late"
            if age > health.warn_after_ns
            else "fresh"
        )
        unavailable = f", {item.unavailable} unavailable" if item.unavailable else ""
        rows.append(
            (
                f"Signal {item.signal_id}",
                f"{item.records} records{unavailable}, last {format_duration(age)} ago ({state})",
            )
        )
    return rows
