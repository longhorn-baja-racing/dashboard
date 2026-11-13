"""Freshness and observable loss for one telemetry source.

``SourceHealthTracker`` is fed plain observations by the code that owns a
source (packets, decode errors, connect/disconnect events, heartbeats and
firmware-reported status) and produces immutable ``SourceHealthSnapshot``
values that can be handed to the UI or to session consumers.

Rules that every value follows:

- Arrival times come from a monotonic clock in integer nanoseconds. They decide
  freshness, rate and heartbeat timeouts. Device timestamps are kept only as
  measurement metadata and never decide freshness.
- Counters for metadata the source does not provide (sequence numbers,
  heartbeats, decode error or overflow reports) are ``None`` ("unknown"),
  never ``0``.
- Firmware-reported status is stored as reported and kept apart from the
  dashboard's own inference.

The tracker is not thread-safe: feed it from the thread that owns the source.
Snapshots are frozen and safe to pass between threads.

See docs/source-health.md for thresholds and the sequence heuristics.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum

MS = 1_000_000
S = 1_000_000_000

DEFAULT_WARN_AFTER_NS = 500 * MS
DEFAULT_STALE_AFTER_NS = 2 * S
DEFAULT_RATE_WINDOW_NS = 1 * S
DEFAULT_REORDER_WINDOW = 32
MIN_SEQUENCE_BITS = 2
MAX_SEQUENCE_BITS = 64

ReportedValue = str | int | float | bool


def _require_int(value: object, name: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


# --- Sequence numbers -------------------------------------------------------


class SequenceStep(Enum):
    """How one sequence number relates to the ones seen before it."""

    BASELINE = "baseline"
    """First number of a run; nothing to compare with."""
    IN_ORDER = "in order"
    GAP = "gap"
    """Ahead by more than one; the skipped numbers are counted as lost."""
    DUPLICATE = "duplicate"
    OUT_OF_ORDER = "out of order"
    RESET = "reset"
    """The counter restarted; a new run begins and nothing is counted as lost."""


@dataclass(frozen=True, slots=True)
class SequenceCounts:
    """Loss and ordering counters derived from sequence numbers."""

    bits: int
    reorder_window: int
    packets: int
    """Packets that carried a sequence number, duplicates included."""
    lost: int
    """Numbers skipped while connected and not filled in by a late packet."""
    duplicates: int
    out_of_order: int
    rollovers: int
    resets: int

    @property
    def loss_fraction(self) -> float | None:
        """``lost / (lost + unique packets)``; ``None`` before any packet."""

        expected = self.lost + self.packets - self.duplicates
        return self.lost / expected if expected > 0 else None


class SequenceTracker:
    """Classify a stream of wrapping sequence numbers of a declared bit width.

    Uses serial-number arithmetic (RFC 1982) on the modulus ``2**bits``:

    - a step forward by less than half the modulus is in order (step 1) or a
      gap (larger steps; the skipped numbers are counted as lost). Wrapping
      past the top of the range is a rollover, not a reset.
    - a step backward by at most ``reorder_window`` is a late packet: out of
      order if its number had been counted as lost (the loss is taken back),
      or out of order if it predates the start of the run, otherwise a
      duplicate. Repeating the newest number is also a duplicate.
    - a step backward by more than ``reorder_window`` (or by exactly half the
      modulus) is a reset: a new run starts and nothing is counted as lost.
    - after ``restart()`` (a reconnect), the next number starts a new run. It
      is counted as a reset when it is not ahead of the newest number seen
      before the restart. Numbers skipped across a restart are not counted as
      lost, because the link was not there to observe them.
    """

    def __init__(self, bits: int, reorder_window: int | None = None) -> None:
        _require_int(bits, "sequence_bits", minimum=MIN_SEQUENCE_BITS)
        if bits > MAX_SEQUENCE_BITS:
            raise ValueError(f"sequence_bits must be at most {MAX_SEQUENCE_BITS}")
        modulus = 1 << bits
        if reorder_window is None:
            reorder_window = min(DEFAULT_REORDER_WINDOW, modulus // 4)
        _require_int(reorder_window, "reorder_window", minimum=0)
        if reorder_window >= modulus // 2:
            raise ValueError("reorder_window must be smaller than half the sequence range")
        self._bits = bits
        self._modulus = modulus
        self._window = reorder_window
        self._newest: int | None = None
        self._span = 0  # Numbers from the start of the run to the newest, capped.
        self._missing: set[int] = set()
        self._restarted = False
        self._packets = self._lost = self._duplicates = 0
        self._out_of_order = self._rollovers = self._resets = 0

    @property
    def modulus(self) -> int:
        return self._modulus

    def restart(self) -> None:
        """Start a new run at the next number (for example after a reconnect)."""

        self._restarted = True

    def observe(self, value: int) -> SequenceStep:
        _require_int(value, "sequence", minimum=0)
        if value >= self._modulus:
            raise ValueError(f"sequence {value} does not fit in {self._bits} bits")
        self._packets += 1
        newest = self._newest
        if newest is None:
            return self._begin_run(value, SequenceStep.BASELINE)
        ahead = (value - newest) % self._modulus
        moved_forward = 0 < ahead < self._modulus // 2
        if self._restarted:
            if moved_forward:
                return self._begin_run(value, SequenceStep.BASELINE)
            self._resets += 1
            return self._begin_run(value, SequenceStep.RESET)
        if ahead == 0:
            self._duplicates += 1
            return SequenceStep.DUPLICATE
        if moved_forward:
            return self._advance(value, ahead)
        behind = self._modulus - ahead
        if behind > self._window:
            self._resets += 1
            return self._begin_run(value, SequenceStep.RESET)
        if behind >= self._span:
            self._out_of_order += 1  # Older than the run; it was never counted.
            return SequenceStep.OUT_OF_ORDER
        if value in self._missing:
            self._missing.discard(value)
            self._lost -= 1
            self._out_of_order += 1
            return SequenceStep.OUT_OF_ORDER
        self._duplicates += 1
        return SequenceStep.DUPLICATE

    def counts(self) -> SequenceCounts:
        return SequenceCounts(
            bits=self._bits,
            reorder_window=self._window,
            packets=self._packets,
            lost=self._lost,
            duplicates=self._duplicates,
            out_of_order=self._out_of_order,
            rollovers=self._rollovers,
            resets=self._resets,
        )

    def _begin_run(self, value: int, step: SequenceStep) -> SequenceStep:
        self._newest = value
        self._span = 1
        self._missing.clear()
        self._restarted = False
        return step

    def _advance(self, value: int, ahead: int) -> SequenceStep:
        newest = self._newest
        assert newest is not None
        if value < newest:
            self._rollovers += 1
        skipped = ahead - 1
        self._lost += skipped
        # Only numbers that can still arrive late (inside the window) are remembered.
        for offset in range(max(1, ahead - self._window), ahead):
            self._missing.add((newest + offset) % self._modulus)
        self._newest = value
        self._span = min(self._span + ahead, self._window + 1)
        self._missing = {
            number for number in self._missing if (value - number) % self._modulus <= self._window
        }
        return SequenceStep.GAP if skipped else SequenceStep.IN_ORDER


# --- Configuration ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourceHealthConfig:
    """Thresholds and the health metadata a source declares it provides.

    ``sequence_bits`` and ``heartbeat_timeout_ns`` are ``None`` when the source
    has no sequence numbers or no heartbeat. ``reports_decode_errors`` and
    ``reports_overflow`` say whether the owner feeds those counters; when they
    are False the counters are unknown rather than zero.
    """

    warn_after_ns: int = DEFAULT_WARN_AFTER_NS
    stale_after_ns: int = DEFAULT_STALE_AFTER_NS
    rate_window_ns: int = DEFAULT_RATE_WINDOW_NS
    sequence_bits: int | None = None
    reorder_window: int | None = None
    heartbeat_timeout_ns: int | None = None
    reports_decode_errors: bool = True
    reports_overflow: bool = False

    def __post_init__(self) -> None:
        _require_int(self.warn_after_ns, "warn_after_ns", minimum=1)
        _require_int(self.stale_after_ns, "stale_after_ns", minimum=1)
        if self.stale_after_ns <= self.warn_after_ns:
            raise ValueError("stale_after_ns must be greater than warn_after_ns")
        _require_int(self.rate_window_ns, "rate_window_ns", minimum=1)
        if self.sequence_bits is None:
            if self.reorder_window is not None:
                raise ValueError("reorder_window requires sequence_bits")
        else:
            SequenceTracker(self.sequence_bits, self.reorder_window)  # Validates both.
        if self.heartbeat_timeout_ns is not None:
            _require_int(self.heartbeat_timeout_ns, "heartbeat_timeout_ns", minimum=1)
        for name in ("reports_decode_errors", "reports_overflow"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be a boolean")


# --- Snapshot ---------------------------------------------------------------


class ConnectionState(Enum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    UNKNOWN = "unknown"
    """The source has not reported any connection event."""


class Freshness(Enum):
    """Arrival age of the newest valid packet since the latest connect."""

    NO_DATA = "no data"
    FRESH = "fresh"
    WARN = "late"
    STALE = "stale"


class HeartbeatState(Enum):
    UNKNOWN = "unknown"
    """The source declares no heartbeat."""
    WAITING = "waiting"
    """No heartbeat and no connect event to time a timeout from yet."""
    ALIVE = "alive"
    TIMED_OUT = "timed out"


class LinkStatus(Enum):
    """One headline state combining connection and freshness."""

    LIVE = "live"
    LATE = "late"
    STALE = "stale"
    WAITING = "waiting for data"
    DISCONNECTED = "disconnected"


@dataclass(frozen=True, slots=True)
class ReportedStatus:
    """Status fields exactly as the source reported them, with arrival time."""

    received_ns: int
    fields: tuple[tuple[str, ReportedValue], ...]


@dataclass(frozen=True, slots=True)
class SourceHealthSnapshot:
    """Health of one source at ``at_ns`` (monotonic arrival clock).

    ``None`` always means unknown: the source does not provide that metadata
    or nothing has been observed yet. It never stands in for zero.
    """

    source_id: str
    at_ns: int
    connection: ConnectionState
    connected_since_ns: int | None
    disconnect_reason: str | None
    connects: int
    disconnects: int
    freshness: Freshness
    age_ns: int | None
    """Arrival age of the newest valid packet since the latest connect."""
    warn_after_ns: int
    stale_after_ns: int
    packets: int
    """Valid packets since the tracker was created, across reconnects."""
    rate_hz: float | None
    """Valid packets per second over the last full rate window."""
    rate_window_ns: int
    sequence: SequenceCounts | None
    decode_errors: int | None
    last_decode_error: str | None
    overflow_dropped: int | None
    """Records the dashboard dropped itself (for example a full queue)."""
    heartbeat: HeartbeatState
    heartbeat_age_ns: int | None
    heartbeat_timeout_ns: int | None
    heartbeat_timeouts: int | None
    """Heartbeat gaps longer than the timeout that ended with a heartbeat."""
    device_time_ns: int | None
    """Device timestamp of the newest packet that carried one."""
    device_time_regressions: int | None
    reported: ReportedStatus | None

    @property
    def status(self) -> LinkStatus:
        if self.connection is ConnectionState.DISCONNECTED:
            return LinkStatus.DISCONNECTED
        return {
            Freshness.NO_DATA: LinkStatus.WAITING,
            Freshness.FRESH: LinkStatus.LIVE,
            Freshness.WARN: LinkStatus.LATE,
            Freshness.STALE: LinkStatus.STALE,
        }[self.freshness]


# --- Tracker ----------------------------------------------------------------


class SourceHealthTracker:
    """Accumulate observations for one source and produce snapshots.

    Every ``observe_*`` method takes the arrival time from the monotonic
    ``clock`` unless ``arrival_ns`` is given. Arrival times must never go
    backwards. Feeding metadata the config does not declare (a sequence number
    without ``sequence_bits``, a heartbeat without ``heartbeat_timeout_ns``)
    raises ``ValueError`` so counters always match the declared capabilities.
    """

    def __init__(
        self,
        source_id: str,
        config: SourceHealthConfig | None = None,
        *,
        clock: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if not isinstance(source_id, str) or not source_id.strip():
            raise ValueError("source_id must be nonempty text")
        self._source_id = source_id
        self._config = config if config is not None else SourceHealthConfig()
        self._clock = clock
        bits = self._config.sequence_bits
        self._sequence = (
            SequenceTracker(bits, self._config.reorder_window) if bits is not None else None
        )
        self._last_arrival_ns: int | None = None
        self._connection = ConnectionState.UNKNOWN
        self._connected_since_ns: int | None = None
        self._disconnect_reason: str | None = None
        self._connects = self._disconnects = 0
        self._packets = 0
        self._last_packet_ns: int | None = None
        self._rate_epoch_ns: int | None = None
        self._recent: deque[int] = deque()
        self._decode_errors = 0
        self._last_decode_error: str | None = None
        self._overflow_dropped = 0
        self._heartbeat_ref_ns: int | None = None
        self._last_heartbeat_ns: int | None = None
        self._heartbeat_timeouts = 0
        self._device_time_ns: int | None = None
        self._device_time_previous: int | None = None  # Comparison baseline, per connection.
        self._device_time_regressions = 0
        self._reported: ReportedStatus | None = None

    @property
    def source_id(self) -> str:
        return self._source_id

    @property
    def config(self) -> SourceHealthConfig:
        return self._config

    def observe_connect(self, arrival_ns: int | None = None) -> None:
        """The link is (re)established; a new sequence run and rate window start."""

        now = self._arrival(arrival_ns)
        self._connection = ConnectionState.CONNECTED
        self._connected_since_ns = now
        self._disconnect_reason = None
        self._connects += 1
        self._last_packet_ns = None
        self._rate_epoch_ns = now
        self._recent.clear()
        self._heartbeat_ref_ns = now
        self._last_heartbeat_ns = None
        self._device_time_previous = None  # The device may have rebooted.
        if self._sequence is not None:
            self._sequence.restart()

    def observe_disconnect(
        self, arrival_ns: int | None = None, *, reason: str | None = None
    ) -> None:
        self._arrival(arrival_ns)
        self._connection = ConnectionState.DISCONNECTED
        self._connected_since_ns = None
        self._disconnect_reason = reason
        self._disconnects += 1

    def observe_packet(
        self,
        arrival_ns: int | None = None,
        *,
        sequence: int | None = None,
        device_time_ns: int | None = None,
    ) -> SequenceStep | None:
        """Record one valid (decoded) packet.

        Returns the sequence classification, or ``None`` without a sequence.
        A source that declares ``sequence_bits`` may still send some packets
        without a number; those count for freshness and rate only.
        """

        if sequence is not None and self._sequence is None:
            raise ValueError("sequence numbers require SourceHealthConfig.sequence_bits")
        if device_time_ns is not None and (
            isinstance(device_time_ns, bool) or not isinstance(device_time_ns, int)
        ):
            raise TypeError("device_time_ns must be an integer")
        now = self._arrival(arrival_ns)
        step = self._sequence.observe(sequence) if self._sequence and sequence is not None else None
        self._packets += 1
        self._last_packet_ns = now
        if self._rate_epoch_ns is None:
            self._rate_epoch_ns = now
        self._recent.append(now)
        self._prune_recent(now)
        if device_time_ns is not None:
            previous = self._device_time_previous
            if previous is not None and device_time_ns < previous:
                self._device_time_regressions += 1
            self._device_time_previous = self._device_time_ns = device_time_ns
        return step

    def observe_decode_error(
        self, arrival_ns: int | None = None, *, message: str | None = None
    ) -> None:
        """Bytes arrived but could not be decoded; they do not refresh freshness."""

        if not self._config.reports_decode_errors:
            raise ValueError("decode errors are not declared for this source")
        self._arrival(arrival_ns)
        self._decode_errors += 1
        self._last_decode_error = message

    def observe_overflow(self, dropped: int, arrival_ns: int | None = None) -> None:
        """The dashboard dropped ``dropped`` records itself (not transport loss)."""

        if not self._config.reports_overflow:
            raise ValueError("overflow reports are not declared for this source")
        _require_int(dropped, "dropped", minimum=1)
        self._arrival(arrival_ns)
        self._overflow_dropped += dropped

    def observe_heartbeat(self, arrival_ns: int | None = None) -> None:
        timeout = self._config.heartbeat_timeout_ns
        if timeout is None:
            raise ValueError("heartbeats require SourceHealthConfig.heartbeat_timeout_ns")
        now = self._arrival(arrival_ns)
        if self._heartbeat_ref_ns is not None and now - self._heartbeat_ref_ns > timeout:
            self._heartbeat_timeouts += 1
        self._heartbeat_ref_ns = now
        self._last_heartbeat_ns = now

    def observe_reported_status(
        self, fields: Mapping[str, ReportedValue], arrival_ns: int | None = None
    ) -> None:
        """Store a complete status message from the source, replacing the last one."""

        items: list[tuple[str, ReportedValue]] = []
        for name, value in fields.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("reported field names must be nonempty text")
            if not isinstance(value, (str, int, float, bool)):
                raise TypeError(f"reported field {name!r} must be text, number or boolean")
            items.append((name, value))
        now = self._arrival(arrival_ns)
        self._reported = ReportedStatus(now, tuple(sorted(items)))

    def snapshot(self, now_ns: int | None = None) -> SourceHealthSnapshot:
        now = self._clock() if now_ns is None else now_ns
        _require_int(now, "now_ns", minimum=0)
        if self._last_arrival_ns is not None and now < self._last_arrival_ns:
            raise ValueError("snapshot time is earlier than the last observation")
        config = self._config
        heartbeat, heartbeat_age = self._heartbeat_state(now)
        age = None if self._last_packet_ns is None else now - self._last_packet_ns
        return SourceHealthSnapshot(
            source_id=self._source_id,
            at_ns=now,
            connection=self._connection,
            connected_since_ns=self._connected_since_ns,
            disconnect_reason=self._disconnect_reason,
            connects=self._connects,
            disconnects=self._disconnects,
            freshness=self._freshness(now, age),
            age_ns=age,
            warn_after_ns=config.warn_after_ns,
            stale_after_ns=config.stale_after_ns,
            packets=self._packets,
            rate_hz=self._rate(now),
            rate_window_ns=config.rate_window_ns,
            sequence=self._sequence.counts() if self._sequence is not None else None,
            decode_errors=self._decode_errors if config.reports_decode_errors else None,
            last_decode_error=self._last_decode_error,
            overflow_dropped=self._overflow_dropped if config.reports_overflow else None,
            heartbeat=heartbeat,
            heartbeat_age_ns=heartbeat_age,
            heartbeat_timeout_ns=config.heartbeat_timeout_ns,
            heartbeat_timeouts=(
                self._heartbeat_timeouts if config.heartbeat_timeout_ns is not None else None
            ),
            device_time_ns=self._device_time_ns,
            device_time_regressions=(
                self._device_time_regressions if self._device_time_ns is not None else None
            ),
            reported=self._reported,
        )

    def _arrival(self, arrival_ns: int | None) -> int:
        now = self._clock() if arrival_ns is None else arrival_ns
        _require_int(now, "arrival_ns", minimum=0)
        if self._last_arrival_ns is not None and now < self._last_arrival_ns:
            raise ValueError(
                f"arrival times must not go backwards ({now} < {self._last_arrival_ns})"
            )
        self._last_arrival_ns = now
        return now

    def _freshness(self, now: int, age: int | None) -> Freshness:
        config = self._config
        if age is None:
            # Connected but silent for longer than the stale limit is stale, not waiting.
            since = self._connected_since_ns
            if since is not None and now - since > config.stale_after_ns:
                return Freshness.STALE
            return Freshness.NO_DATA
        if age > config.stale_after_ns:
            return Freshness.STALE
        if age > config.warn_after_ns:
            return Freshness.WARN
        return Freshness.FRESH

    def _rate(self, now: int) -> float | None:
        window = self._config.rate_window_ns
        self._prune_recent(now)
        if self._rate_epoch_ns is None or now - self._rate_epoch_ns < window:
            return None  # The window is not full yet, so a rate would be misleading.
        return len(self._recent) * S / window

    def _prune_recent(self, now: int) -> None:
        # The window is (now - rate_window_ns, now]; older arrivals are dropped.
        cutoff = now - self._config.rate_window_ns
        recent = self._recent
        while recent and recent[0] <= cutoff:
            recent.popleft()

    def _heartbeat_state(self, now: int) -> tuple[HeartbeatState, int | None]:
        timeout = self._config.heartbeat_timeout_ns
        if timeout is None:
            return HeartbeatState.UNKNOWN, None
        last = self._last_heartbeat_ns
        age = None if last is None else now - last
        ref = self._heartbeat_ref_ns
        if ref is None:
            return HeartbeatState.WAITING, age
        if now - ref > timeout:
            return HeartbeatState.TIMED_OUT, age
        return (HeartbeatState.ALIVE if last is not None else HeartbeatState.WAITING), age


# --- Text -------------------------------------------------------------------


def format_duration(ns: int) -> str:
    """Seconds with millisecond resolution, for example ``1.250 s``."""

    return f"{ns / S:.3f} s"


def format_rate(rate_hz: float) -> str:
    return f"{rate_hz:.1f} packets/s"


def status_text(snapshot: SourceHealthSnapshot) -> str:
    """Headline state in words; never relies on color."""

    status = snapshot.status
    connected = snapshot.connection is ConnectionState.CONNECTED
    if status is LinkStatus.DISCONNECTED:
        reason = snapshot.disconnect_reason
        return f"Disconnected ({reason})" if reason else "Disconnected"
    if status is LinkStatus.STALE:
        return "Connected but stale" if connected else "Stale (connection unknown)"
    if status is LinkStatus.LATE:
        return "Connected, data late" if connected else "Data late (connection unknown)"
    if status is LinkStatus.WAITING:
        return "Connected, waiting for data" if connected else "Waiting for data"
    return "Connected, live" if connected else "Live (connection unknown)"


def _connection_text(snapshot: SourceHealthSnapshot) -> str:
    counts = f"{snapshot.connects} connects, {snapshot.disconnects} disconnects"
    if snapshot.connection is ConnectionState.UNKNOWN:
        return "unknown (source reports no connection events)"
    if snapshot.connection is ConnectionState.CONNECTED:
        since = snapshot.connected_since_ns
        assert since is not None
        return f"connected for {format_duration(snapshot.at_ns - since)} ({counts})"
    return f"disconnected ({counts})"


def _age_text(snapshot: SourceHealthSnapshot) -> str:
    limits = (
        f"late after {format_duration(snapshot.warn_after_ns)}, "
        f"stale after {format_duration(snapshot.stale_after_ns)}"
    )
    if snapshot.age_ns is None:
        if snapshot.packets and snapshot.connects:
            return f"none since connect ({snapshot.freshness.value}; {limits})"
        return f"none received ({snapshot.freshness.value}; {limits})"
    age = format_duration(snapshot.age_ns)
    return f"{age} ago ({snapshot.freshness.value}; {limits})"


def _rate_text(snapshot: SourceHealthSnapshot) -> str:
    window = format_duration(snapshot.rate_window_ns)
    if snapshot.rate_hz is None:
        return f"measuring (needs a full {window} window)"
    return f"{format_rate(snapshot.rate_hz)} over {window}"


def _sequence_rows(counts: SequenceCounts | None) -> list[tuple[str, str]]:
    if counts is None:
        text = "unknown (no sequence numbers)"
        return [("Lost packets", text), ("Duplicates / out of order", text)]
    fraction = counts.loss_fraction
    lost = str(counts.lost)
    if fraction is not None:
        lost += f" ({fraction * 100:.2f}% of expected)"
    order = f"{counts.duplicates} duplicates, {counts.out_of_order} out of order"
    runs = (
        f"{counts.rollovers} rollovers, {counts.resets} resets "
        f"({counts.bits}-bit counter, {counts.packets} numbered packets)"
    )
    return [
        ("Lost packets", lost),
        ("Duplicates / out of order", order),
        ("Sequence", runs),
    ]


def _heartbeat_text(snapshot: SourceHealthSnapshot) -> str:
    if snapshot.heartbeat is HeartbeatState.UNKNOWN:
        return "unknown (source has no heartbeat)"
    timeout = snapshot.heartbeat_timeout_ns
    assert timeout is not None
    age = snapshot.heartbeat_age_ns
    last = "none yet" if age is None else f"last {format_duration(age)} ago"
    return (
        f"{snapshot.heartbeat.value}: {last} (timeout {format_duration(timeout)}, "
        f"{snapshot.heartbeat_timeouts} recovered timeouts)"
    )


def _count_text(value: int | None, missing: str) -> str:
    return f"unknown ({missing})" if value is None else str(value)


def format_source_health(snapshot: SourceHealthSnapshot) -> list[tuple[str, str]]:
    """Return ASCII ``(label, value)`` rows; every state is spelled out."""

    decode = _count_text(snapshot.decode_errors, "not reported")
    if snapshot.decode_errors and snapshot.last_decode_error:
        decode += f" (last: {snapshot.last_decode_error})"
    device_time = (
        "unknown (no device timestamps)"
        if snapshot.device_time_ns is None
        else f"{format_duration(snapshot.device_time_ns)} "
        f"({snapshot.device_time_regressions} regressions)"
    )
    rows = [
        ("Status", status_text(snapshot)),
        ("Connection", _connection_text(snapshot)),
        ("Last packet", _age_text(snapshot)),
        ("Packet rate", _rate_text(snapshot)),
        ("Packets received", str(snapshot.packets)),
        *_sequence_rows(snapshot.sequence),
        ("Decode errors", decode),
        ("Dashboard drops", _count_text(snapshot.overflow_dropped, "not reported")),
        ("Heartbeat", _heartbeat_text(snapshot)),
        ("Device time", device_time),
    ]
    reported = snapshot.reported
    if reported is None:
        rows.append(("Reported status", "unknown (nothing reported by source)"))
    else:
        age = format_duration(snapshot.at_ns - reported.received_ns)
        rows.append(("Reported status", f"received {age} ago"))
        rows.extend((f"Reported: {name}", str(value)) for name, value in reported.fields)
    return rows
