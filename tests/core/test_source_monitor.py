"""Source health fed from the source contract: state, drained events and queue loss."""

from __future__ import annotations

from typing import cast

import pytest

from lbr_dashboard.core import (
    SignalDescriptor,
    SignalSample,
    SignalSchema,
    SimulatorSource,
    SourceCapabilities,
    SourceError,
    SourceEvent,
    SourceEventKind,
    SourceHealth,
    SourceState,
    TelemetrySession,
    TelemetrySource,
    example_signals,
)
from lbr_dashboard.core.contracts import SourceEventQueue
from lbr_dashboard.core.source_health import (
    MS,
    ConnectionState,
    Freshness,
    HeartbeatState,
    LinkStatus,
    S,
    SourceHealthConfig,
    status_text,
    summary_text,
)
from lbr_dashboard.core.source_monitor import (
    SEQUENCE_KEY,
    SourceHealthMonitor,
    declared_config,
    disconnect_reason,
    signal_arrival_rows,
)

SOURCE = "bench"
SPEED = "bench.speed"
TEMP = "bench.temp"


class Clock:
    def __init__(self) -> None:
        self.now = 100 * S

    def __call__(self) -> int:
        return self.now

    def advance(self, ns: int) -> None:
        self.now += ns


class FakeSource:
    """Just enough of a TelemetrySource for the monitor; state is set by the test."""

    def __init__(self, config: SourceHealthConfig | None = None, capacity: int = 64) -> None:
        self.source_id = SOURCE
        self.schema = SignalSchema(
            1,
            (
                SignalDescriptor(SPEED, "Speed", ("bench",), "numeric", SOURCE),
                SignalDescriptor(TEMP, "Temperature", ("bench",), "numeric", SOURCE),
            ),
        )
        self.events = SourceEventQueue(capacity)
        self.capabilities = SourceCapabilities(True, False, False, capacity)
        self.state = SourceState.CREATED
        self.terminal_reason: str | None = None
        self.last_error: SourceError | None = None
        if config is not None:
            self.health_config = config

    def health(self) -> SourceHealth:
        return SourceHealth(
            self.state,
            dropped_records=self.events.stats.dropped_records,
            last_error=self.last_error,
            terminal_reason=self.terminal_reason,
        )


def record(signal_id: str = SPEED, *, sequence: int | None = None, available: bool = True):
    extra = {} if sequence is None else {SEQUENCE_KEY: sequence}
    sample = SignalSample(0, 1.0 if available else None, available=available, extra=extra)
    return SourceEvent(SourceEventKind.RECORD, SOURCE, signal_id=signal_id, sample=sample)


def heartbeat(state: SourceState = SourceState.RUNNING, dropped: int = 0) -> SourceEvent:
    return SourceEvent(
        SourceEventKind.HEALTH, SOURCE, health=SourceHealth(state, dropped_records=dropped)
    )


def monitor(
    config: SourceHealthConfig | None = None, capacity: int = 64
) -> tuple[FakeSource, SourceHealthMonitor, Clock]:
    clock = Clock()
    source = FakeSource(config, capacity)
    return source, SourceHealthMonitor(cast(TelemetrySource, source), clock=clock), clock


def test_undeclared_metadata_is_unknown_but_queue_drops_are_counted():
    source, health, _ = monitor()
    source.state = SourceState.RUNNING
    health.observe_events([record()])
    snapshot = health.report().health
    assert snapshot.sequence is None
    assert snapshot.heartbeat is HeartbeatState.UNKNOWN
    assert snapshot.decode_errors is None
    assert snapshot.reported is None
    assert snapshot.overflow_dropped == 0
    assert "loss unknown (no sequence numbers)" in summary_text(snapshot)
    assert "lost 0" not in summary_text(snapshot)


def test_declared_config_keeps_the_source_declaration_and_adds_overflow():
    declared = SourceHealthConfig(sequence_bits=8, heartbeat_timeout_ns=S)
    config = declared_config(FakeSource(declared))  # type: ignore[arg-type]
    assert config.sequence_bits == 8 and config.heartbeat_timeout_ns == S
    assert config.reports_overflow
    assert not declared_config(FakeSource()).reports_decode_errors  # type: ignore[arg-type]


def test_connected_but_stale_differs_from_disconnected_at_the_thresholds():
    source, health, clock = monitor()
    assert health.report().health.connection is ConnectionState.UNKNOWN
    source.state = SourceState.OPENING
    assert health.report().health.connection is ConnectionState.UNKNOWN
    source.state = SourceState.RUNNING
    health.observe_events([record(), record(TEMP)])
    assert health.report().health.status is LinkStatus.LIVE
    clock.advance(500 * MS)
    assert health.report().health.freshness is Freshness.FRESH  # Boundary is inclusive.
    clock.advance(1)
    assert health.report().health.status is LinkStatus.LATE
    clock.advance(1500 * MS - 1)
    assert health.report().health.status is LinkStatus.LATE
    clock.advance(1)
    stale = health.report().health
    assert stale.status is LinkStatus.STALE
    assert status_text(stale) == "Connected but stale"
    source.state = SourceState.STOPPING
    gone = health.report().health
    assert gone.status is LinkStatus.DISCONNECTED
    assert status_text(gone) == "Disconnected (stopped)"
    assert summary_text(stale) != summary_text(gone)
    source.state = SourceState.CLOSED
    assert health.report().health.disconnects == 1  # Still the same disconnect.


def test_records_arriving_with_the_first_running_state_count_after_the_connect():
    source, health, _ = monitor()
    source.state = SourceState.RUNNING
    health.observe_events([record()])
    snapshot = health.report().health
    assert snapshot.connects == 1 and snapshot.packets == 1
    assert snapshot.freshness is Freshness.FRESH


def test_reconnect_and_terminal_reasons():
    source, health, clock = monitor()
    source.state = SourceState.RUNNING
    health.observe_events([record()])
    source.state = SourceState.FAILED
    source.last_error = SourceError("read_failed", "port vanished")
    assert status_text(health.report().health) == "Disconnected (failed: port vanished)"
    clock.advance(S)
    source.state = SourceState.RUNNING
    snapshot = health.report().health
    assert snapshot.connects == 2 and snapshot.freshness is Freshness.NO_DATA
    assert status_text(snapshot) == "Connected, waiting for data"
    ended = SourceHealth(SourceState.ENDED, terminal_reason="complete")
    assert disconnect_reason(SourceState.ENDED, ended) == "end of data"
    assert disconnect_reason(SourceState.ENDED, SourceHealth(SourceState.ENDED)) == "ended"


def test_sequence_loss_rollover_duplicates_and_reset_match_a_known_stream():
    source, health, clock = monitor(SourceHealthConfig(sequence_bits=8))
    source.state = SourceState.RUNNING
    # 253..255 then 0 is a rollover; 2 skips 1; 2 again is a duplicate.
    health.observe_events([record(sequence=n) for n in (253, 254, 255, 0, 2, 2)])
    clock.advance(10 * MS)
    # 1 arrives late and is no longer lost; 200 is 58 back (window 32): the counter reset.
    health.observe_events([record(sequence=1), record(sequence=200), record(sequence=201)])
    counts = health.report().health.sequence
    assert counts is not None
    assert (counts.lost, counts.duplicates, counts.out_of_order) == (0, 1, 1)
    assert (counts.rollovers, counts.resets) == (1, 1)
    health.observe_events([record(sequence=n) for n in (202, 204)])
    counts = health.report().health.sequence
    assert counts is not None
    assert counts.lost == 1 and counts.packets == 11
    assert "lost 1, 1 duplicates, 1 out of order" in summary_text(health.report().health)


def test_records_of_one_packet_share_a_sequence_number():
    source, health, _ = monitor(SourceHealthConfig(sequence_bits=16))
    source.state = SourceState.RUNNING
    batch = [
        record(SPEED, sequence=7),
        record(TEMP, sequence=7),
        record(SPEED, sequence=8),
        record(TEMP, sequence=8),
        record(SPEED, sequence=8),  # The same packet delivered twice.
        record(TEMP, sequence=8),
        record(SPEED),  # A record without a number counts for freshness only.
        record(SPEED, sequence=70_000),  # Out of range for 16 bits: no number.
    ]
    health.observe_events(batch)
    snapshot = health.report().health
    assert snapshot.packets == 5
    assert snapshot.sequence is not None
    assert snapshot.sequence.packets == 3 and snapshot.sequence.duplicates == 1


def test_sequence_numbers_are_ignored_when_the_source_does_not_declare_them():
    source, health, _ = monitor()
    source.state = SourceState.RUNNING
    health.observe_events([record(sequence=1), record(sequence=1)])
    snapshot = health.report().health
    assert snapshot.packets == 2 and snapshot.sequence is None


def test_heartbeat_timeout_reported_status_and_decode_errors():
    config = SourceHealthConfig(heartbeat_timeout_ns=2 * S, reports_decode_errors=True)
    source, health, clock = monitor(config)
    source.state = SourceState.RUNNING
    health.observe_events([heartbeat(dropped=3)])
    snapshot = health.report().health
    assert snapshot.heartbeat is HeartbeatState.ALIVE
    assert snapshot.reported is not None
    assert dict(snapshot.reported.fields)["dropped_records"] == 3
    assert snapshot.overflow_dropped == 0  # Reported loss is not mixed into inference.
    clock.advance(2 * S + 1)
    assert health.report().health.heartbeat is HeartbeatState.TIMED_OUT
    corrupt = SourceError("decode_error", "bad checksum", recoverable=True)
    other = SourceError("simulated_loss", "Scheduled measurements lost", recoverable=True)
    health.observe_events(
        [
            SourceEvent(SourceEventKind.ERROR, SOURCE, error=corrupt),
            SourceEvent(SourceEventKind.ERROR, SOURCE, error=other),
            heartbeat(),
        ]
    )
    snapshot = health.report().health
    assert snapshot.decode_errors == 1 and snapshot.last_decode_error == "bad checksum"
    assert snapshot.heartbeat is HeartbeatState.ALIVE and snapshot.heartbeat_timeouts == 1


def test_queue_overflow_counts_only_while_connected():
    source, health, _ = monitor(capacity=1)
    source.state = SourceState.RUNNING
    source.events.publish(record())
    source.events.publish(record())  # Full: dropped by the dashboard queue.
    assert health.report().health.overflow_dropped == 1
    source.state = SourceState.STOPPING
    source.events.close()
    source.events.publish(record())  # Rejected after stop: not loss.
    assert health.report().health.overflow_dropped == 1


def test_per_signal_arrivals_separate_unavailable_readings_from_silence():
    source, health, clock = monitor()
    source.state = SourceState.RUNNING
    health.observe_events([record(SPEED), record(SPEED, available=False)])
    clock.advance(3 * S)
    health.observe_events([record(SPEED)])
    rows = dict(signal_arrival_rows(health.report()))
    assert rows[f"Signal {SPEED}"] == "3 records, 1 unavailable, last 0.000 s ago (fresh)"
    assert rows[f"Signal {TEMP}"] == "no records received"
    clock.advance(600 * MS)
    rows = dict(signal_arrival_rows(health.report()))
    assert rows[f"Signal {SPEED}"].endswith("last 0.600 s ago (late)")


def test_session_observer_sees_committed_batches_only():
    source, health, _ = monitor()
    source.state = SourceState.RUNNING
    session = TelemetrySession(source.schema, max_samples_per_signal=10)
    seen: list[int] = []

    def observer(batch: tuple[SourceEvent, ...]) -> None:
        seen.append(len(batch))

    source.events.publish(record())
    source.events.publish(record(TEMP))
    assert session.ingest_source(source, observer=observer) == 2  # type: ignore[arg-type]
    assert seen == [2]
    bad = SourceEvent(
        SourceEventKind.RECORD,
        SOURCE,
        signal_id=SPEED,
        sample=SignalSample(0, 1.0, timestamp_domain="other"),
    )
    source.events.publish(bad)
    with pytest.raises(ValueError):
        session.ingest_source(source, observer=observer)  # type: ignore[arg-type]
    assert seen == [2]


def test_simulator_declares_a_heartbeat_only_in_real_time():
    live = SimulatorSource(example_signals(), duration_ns=None, realtime=True)
    finite = SimulatorSource(example_signals())
    assert live.health_config.heartbeat_timeout_ns == 2 * S
    assert finite.health_config.heartbeat_timeout_ns is None
    assert live.health_config.sequence_bits is None
    assert not live.health_config.reports_decode_errors
