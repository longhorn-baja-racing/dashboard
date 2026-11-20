"""Read-only live source for the firmware's USB serial link (protocol version 4).

The Central streams COBS-framed transport packets on its USB-UART at 921600
baud (``docs/protocol.md``, "Transport packets"); they carry the raw CAN frames
of the bus, firmware log text and car logic outputs. This adapter:

- reads the port on the source worker through a :class:`SerialPort`, an
  interface with ``open``, ``read`` and ``close`` and nothing that writes, so
  it never sends a byte (or a command) to the vehicle;
- splits frames at ``0x00``, checks COBS, packet header, CRC and record framing,
  and resynchronizes at the next delimiter after any corruption; every rejected
  frame is counted and reported as a recoverable ``decode_error`` event;
- tracks the 32-bit per-link packet sequence: gaps are packets lost on the
  link, counted and published on the ``usb_link.lost_packets`` channel;
- decodes the records with the ``.lbrlog`` reader's decoder and catalog, so a
  live reading has the same signal ID, device time, value and unavailable
  reason as the same record replayed from a log;
- keeps device measurement time (microseconds on the Central's clock, or a
  node's own clock before it synchronized) and uses arrival time only for
  link freshness.

The session schema is fixed when the source is created, so it is built from
the catalog (every frame field on both clocks, events, heartbeats, health
pages, text) plus the car logic outputs announced while preparing (outputs
re-send their ``KEY`` record every second). Readings of anything else are
counted and reported once, never shown under a guessed name.

A port error ends the run as failed (``serial_disconnected``) and the port is
released; Retry opens it again in a new session.
"""

from __future__ import annotations

import struct
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from threading import Lock
from time import monotonic
from typing import Any, Protocol

from ..core import (
    CancellationToken,
    IteratorSource,
    SignalDescriptor,
    SignalSample,
    SignalSchema,
    SourceError,
    SourceEvent,
    SourceEventKind,
)
from ..core.lbr_session import NODE_CLOCK, SHARED_CLOCK
from ..core.source_health import SourceHealthConfig
from .lbr_stream import (
    MAX_FRAME,
    FrameSplitter,
    LinkSequence,
    Packet,
    PacketError,
    SeqStep,
    cobs_decode,
    parse_packet,
)
from .lbrlog import (
    HEALTH_FIELDS,
    REC_CAN_FRAME,
    REC_KEY,
    Catalog,
    ChannelKey,
    LbrlogError,
    _Decoder,
)
from .lbrlog_source import SHARED_DOMAIN, _descriptor, _sample_maker, signal_identifier

DEFAULT_BAUD = 921_600
#: How long preparation listens for car logic output names (sent every second).
DISCOVERY_S = 1.2
READ_SIZE = 4096
READ_TIMEOUT_S = 0.05
#: Queue between the worker and the app: about two seconds of a busy bus.
QUEUE_CAPACITY = 16_384
#: Decode errors published per second; the rest are summarized once a second.
ERRORS_PER_SECOND = 10
#: A packet time this far behind the previous one means the device restarted.
RESTART_BACKSTEP_US = 1_000_000
LINK_PREFIX = "usb_link"
SEQUENCE_KEY = "sequence"

_CAN = struct.Struct("<QIBB")
_U16 = struct.Struct("<H")
_U32 = struct.Struct("<I")


class SerialPortError(Exception):
    """The port could not be opened or read; the message is shown to the user."""


class SerialDisconnected(SerialPortError):
    """The port went away while open (cable unplugged, device reset, driver error)."""


class SerialPort(Protocol):
    """Read-only byte stream from one serial port; implementations must not write."""

    @property
    def name(self) -> str: ...

    def open(self) -> None:
        """Open for reading; raise :class:`SerialPortError` with a readable reason."""

    def read(self, max_bytes: int, timeout_s: float) -> bytes:
        """Up to ``max_bytes`` received bytes, ``b""`` after ``timeout_s`` without data."""
        ...

    def close(self) -> None:
        """Release the port; safe to call more than once."""


PortOpener = Callable[[str], SerialPort]


@dataclass(frozen=True, slots=True)
class SerialPortInfo:
    """One port the operating system lists, for the port chooser."""

    name: str
    description: str = ""
    manufacturer: str = ""
    usb: bool = False

    @property
    def label(self) -> str:
        detail = " - ".join(part for part in (self.description, self.manufacturer) if part)
        return f"{self.name} ({detail})" if detail else self.name


# --------------------------------------------------------------------------
# Decoding


@dataclass(frozen=True, slots=True)
class OutputKey:
    """A car logic output announced by a ``KEY`` record (ids hold for one boot)."""

    name: str
    value_type: str
    unit: str
    key_id: int | None = None


def output_key(catalog: Catalog, payload: bytes) -> OutputKey | None:
    """Name, type and unit of a ``KEY`` record, as the log reader decodes them."""

    if len(payload) < 4:
        return None
    key_id, vtype, unit = struct.unpack_from("<HBB", payload)
    name = bytes(payload[4:]).decode("ascii", "replace")
    type_name = catalog.types[vtype] if vtype < len(catalog.types) else f"type{vtype}"
    return OutputKey(
        name,
        "bool" if type_name == "bool" else "f32",
        catalog.units[unit] if unit < len(catalog.units) else "",
        key_id,
    )


@dataclass
class PacketResult:
    """Everything one packet produced, in record order."""

    readings: list[tuple[str, SignalSample]] = field(default_factory=list)
    heartbeat: bool = False
    problems: list[str] = field(default_factory=list)
    hidden: list[str] = field(default_factory=list)
    catalog_mismatch: list[str] = field(default_factory=list)


def _link_descriptor(name: str, label: str, description: str, source_id: str) -> SignalDescriptor:
    return SignalDescriptor(
        f"{LINK_PREFIX}.{name}",
        label,
        (LINK_PREFIX,),
        "numeric",
        source_id,
        canonical_unit="count",
        description=description,
        display={"lbr_kind": "link"},
        timestamp_domain=SHARED_DOMAIN,
    )


class LiveDecoder:
    """Turn transport packets into readings with the ``.lbrlog`` decoder.

    Channels are declared up front from the catalog (and the given outputs),
    so the schema is complete before the first byte arrives; the decoder fills
    those same channel buffers and they are emptied after every packet, so
    memory stays bounded however long the link runs.
    """

    def __init__(
        self,
        catalog: Catalog,
        source_id: str,
        outputs: tuple[OutputKey, ...] = (),
    ) -> None:
        self.catalog = catalog
        self.source_id = source_id
        decoder = _Decoder((catalog,))
        decoder._choose_catalog(catalog.hash)
        decoder.logger = catalog.node(catalog.central)
        self._decoder = decoder
        self._declare(outputs)
        identifiers: dict[ChannelKey, str] = {}
        descriptors = []
        used: set[str] = set()
        for key, buffer in decoder.channels.items():
            channel = buffer.freeze()
            identifier = base = signal_identifier(channel)
            suffix = 2
            while identifier in used:
                identifier = f"{base}-{suffix}"
                suffix += 1
            used.add(identifier)
            identifiers[key] = identifier
            descriptors.append(_descriptor(channel, identifier, source_id))
        descriptors.extend(
            (
                _link_descriptor(
                    "packet_seq",
                    "USB link packet sequence",
                    "Per-link sequence number of every packet received, at the packet's "
                    "build time on the Central.",
                    source_id,
                ),
                _link_descriptor(
                    "lost_packets",
                    "USB link lost packets",
                    "Packets lost in transit so far (gaps in the packet sequence).",
                    source_id,
                ),
                _link_descriptor(
                    "rejected_frames",
                    "USB link rejected frames",
                    "Frames that were not valid packets so far (COBS, header, CRC or "
                    "record framing), including boot text on the port.",
                    source_id,
                ),
            )
        )
        self._identifiers = identifiers
        self.schema = SignalSchema(
            1,
            tuple(descriptors),
            metadata={
                "format": "lbr_usb_stream",
                "protocol_version": 4,
                "catalog_hash": catalog.hash,
                "catalog_origin": catalog.origin,
                "outputs": tuple(output.name for output in outputs),
            },
        )
        self._hidden: set[ChannelKey] = set()
        self.hidden_readings = 0
        self._rejected_seen = 0
        self._mismatched: set[int] = set()

    @property
    def t_us(self) -> int:
        """Latest shared device time the decoder has seen (0 before any)."""

        return self._decoder.t_us

    def _declare(self, outputs: tuple[OutputKey, ...]) -> None:
        decoder = self._decoder
        catalog = self.catalog
        central = catalog.central
        logger = decoder.logger
        can_nodes = [n for n in sorted(catalog.nodes) if catalog.is_can_node(n)]

        def clocks(node: int) -> tuple[str, ...]:
            return (SHARED_CLOCK,) if node == central else (SHARED_CLOCK, NODE_CLOCK)

        for frame in catalog.frames.values():
            node = catalog.node(frame.sender)
            for spec in frame.fields:
                if spec.signal in catalog.event_ids:
                    continue
                info = catalog.signals.get(spec.signal)
                type_name = str(info["type"]) if info else "raw"
                name = str(info["name"]) if info else f"signal_0x{spec.signal:04x}"
                unit = str(info["unit"]) if info else ""
                for clock in clocks(frame.sender):
                    decoder._channel(
                        ChannelKey(node, "sample", name, clock),
                        type_name,
                        unit,
                        node_id=frame.sender,
                        signal_id=spec.signal,
                    )
        event_signals = sorted(catalog.event_ids)
        for node_id in can_nodes:
            node = catalog.node(node_id)
            for signal in event_signals:
                name = str(catalog.signals[signal]["name"])
                for clock in clocks(node_id):
                    decoder._channel(
                        ChannelKey(node, "sample", name, clock),
                        "event",
                        "",
                        node_id=node_id,
                        signal_id=signal,
                    )
            decoder._channel(ChannelKey(node, "heartbeat", "state"), "status")
            decoder._channel(ChannelKey(node, "heartbeat", "err_flags"), "status", "bitmask")
            for _layout, names in HEALTH_FIELDS:
                for health_name in names:
                    if health_name is not None:
                        decoder._channel(
                            ChannelKey(node, "health", health_name), "count", node_id=node_id
                        )
        for node_id in (0, *can_nodes):
            # Frames with an unknown id or length, kept as text like the CSV export.
            decoder._channel(
                ChannelKey(catalog.node(node_id), "can_raw", ""), "text", node_id=node_id
            )
        decoder._channel(ChannelKey(logger, "text", ""), "text")
        decoder._channel(ChannelKey(catalog.node(central), "command", ""), "text", node_id=central)
        for name in ("dropped_records", "dropped_bytes"):
            decoder._channel(ChannelKey(logger, "loss", name), "count")
        for output in outputs:
            decoder._channel(
                ChannelKey(logger, "output", output.name), output.value_type, output.unit
            )
            if output.key_id is not None:
                # Values that arrive before the next KEY record still get their name.
                decoder.keys[output.key_id] = (output.name, output.value_type, output.unit)

    def _check_catalog(self, payload: bytes, result: PacketResult) -> None:
        """Health page 2 carries each node's catalog hash: a different one is surfaced."""

        if len(payload) != 22:
            return
        _t, can_id, flags, dlc = _CAN.unpack_from(payload)
        ids = self.catalog.ids
        if flags & 0x01 or dlc != 8 or can_id < ids["health_base"] + 16 or can_id > 0x7FF:
            return
        node, page = (can_id - ids["health_base"]) >> 4, can_id & 0xF
        if page != 2 or not self.catalog.is_can_node(node):
            return
        reported = _U32.unpack_from(payload, 14)[0]
        if reported != self.catalog.hash and node not in self._mismatched:
            self._mismatched.add(node)
            result.catalog_mismatch.append(
                f"{self.catalog.node(node)} reports signal catalog {reported}, but the "
                f"dashboard decodes with catalog {self.catalog.hash} ({self.catalog.origin}); "
                "names and values from that node may be wrong. Use the dashboard release "
                "that matches the firmware."
            )

    def packet(self, packet: Packet) -> PacketResult:
        """Decode every record of one packet and return its readings in record order."""

        decoder = self._decoder
        result = PacketResult()
        if decoder.t_us == 0:
            # Records without their own time (text) get the time of the latest
            # record before them; before any record that is the packet's time,
            # as it is the SESSION start time for a recording.
            decoder.t_us = packet.t_us
        rejected_before = decoder.rejected
        for rtype, payload in packet.records:
            if rtype == REC_CAN_FRAME:
                self._check_catalog(payload, result)
            try:
                decoder.record(0, rtype, memoryview(payload))
            except LbrlogError as error:
                result.problems.append(str(error))
            except (struct.error, ValueError, IndexError, KeyError) as error:
                result.problems.append(f"record type {rtype} could not be decoded ({error})")
        if decoder.rejected > rejected_before:
            new = decoder.rejected - rejected_before
            reasons = [r.reason for r in decoder.rejected_records[-new:]] if new <= 3 else []
            detail = f": {'; '.join(reasons)}" if reasons else ""
            result.problems.append(f"rejected {new} malformed record(s){detail}")
            # The log reader keeps a bounded list; a live run must not grow it.
            del decoder.rejected_records[:]
        self._drain(packet.seq, result)
        return result

    def _drain(self, seq: int, result: PacketResult) -> None:
        pending: list[tuple[int, str, SignalSample]] = []
        for key, buffer in self._decoder.channels.items():
            if not buffer.order:
                continue
            identifier = self._identifiers.get(key)
            if identifier is None:
                self.hidden_readings += len(buffer.order)
                if key not in self._hidden:
                    self._hidden.add(key)
                    result.hidden.append(_describe(key))
            else:
                if key.kind == "heartbeat":
                    result.heartbeat = True
                channel = buffer.freeze()
                make = _sample_maker(channel, self.source_id)
                for index in range(channel.count):
                    sample = make(index)
                    sample = replace(sample, extra={**sample.extra, SEQUENCE_KEY: seq})
                    pending.append((int(channel.order[index]), identifier, sample))
            for item in (
                buffer.order,
                buffer.times,
                buffer.values,
                buffer.available,
                buffer.status,
                buffer.seq,
            ):
                del item[:]
            buffer.texts.clear()
        pending.sort(key=lambda item: item[0])
        result.readings.extend((identifier, sample) for _, identifier, sample in pending)


def _describe(key: ChannelKey) -> str:
    if key.kind == "output":
        return f"car logic output {key.name}"
    clock = " (node clock)" if key.clock == NODE_CLOCK else ""
    name = f" {key.name}" if key.name else ""
    return f"{key.node} {key.kind}{name}{clock}"


# --------------------------------------------------------------------------
# Source


@dataclass(frozen=True, slots=True)
class LinkStatus:
    """Link counters for the status line; safe to read from any thread."""

    port: str
    connected: bool
    bytes_received: int
    packets: int
    lost_packets: int
    rejected_frames: int
    duplicate_packets: int
    last_seq: int | None
    last_packet_age_s: float | None
    hidden_readings: int

    def summary(self) -> str:
        if not self.packets:
            waiting = "connected, waiting for firmware packets" if self.connected else "closed"
            text = f"{self.port}: {waiting}"
            if self.rejected_frames:
                text += f" ({self.rejected_frames} rejected frames)"
            return text
        text = (
            f"{self.port}: {self.packets} packets, {self.lost_packets} lost, "
            f"{self.rejected_frames} rejected frames"
        )
        if self.duplicate_packets:
            text += f", {self.duplicate_packets} duplicates"
        if self.hidden_readings:
            text += f", {self.hidden_readings} readings not shown"
        age = self.last_packet_age_s
        if age is not None:
            if not self.connected:
                text += f"; port closed, last packet {age:.1f} s ago - data is stale"
            elif age >= 2.0:
                text += f"; no packets for {age:.1f} s - data is stale"
        return text


class _Recorder(Protocol):
    def begin(
        self, start_t_us: int, node_id: int, carry: tuple[tuple[int, bytes], ...]
    ) -> None: ...

    def offer(
        self,
        t_us: int,
        records: tuple[tuple[int, bytes], ...],
        *,
        decoder_t_us: int | None = None,
        lost_packets: int = 0,
        rejected_bytes: int = 0,
    ) -> bool: ...

    def note_time(self, t_us: int) -> None: ...


class SerialLiveSource(IteratorSource):
    """Live source reading one serial port on its worker; see the module docstring."""

    def __init__(
        self,
        port_name: str,
        opener: PortOpener,
        *,
        catalog: Catalog | None = None,
        outputs: tuple[OutputKey, ...] = (),
        capacity: int = QUEUE_CAPACITY,
        read_size: int = READ_SIZE,
        read_timeout_s: float = READ_TIMEOUT_S,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.port_name = port_name
        self.catalog = catalog if catalog is not None else Catalog.bundled()
        self.outputs = outputs
        self._opener = opener
        self._read_size = read_size
        self._read_timeout = read_timeout_s
        self._clock = clock
        source_id = f"serial:{port_name}"
        schema = LiveDecoder(self.catalog, source_id, outputs).schema
        #: What the source health monitor may assume (``core.source_monitor``).
        self.health_config = SourceHealthConfig(
            sequence_bits=32, heartbeat_timeout_ns=3_000_000_000, reports_decode_errors=True
        )
        self._link_lock = Lock()
        self._link = LinkStatus(port_name, False, 0, 0, 0, 0, 0, None, None, 0)
        self._last_arrival: float | None = None
        self._recorder: _Recorder | None = None
        self._recorder_begun = False
        super().__init__(
            source_id,
            schema,
            self._generate,
            is_live=True,
            capacity=capacity,
            publish_timeout=0.0,
        )

    # -- recording -----------------------------------------------------------

    def attach_recorder(self, recorder: _Recorder | None) -> None:
        """Copy every received record to ``recorder`` from the next packet on."""

        with self._link_lock:
            self._recorder = recorder
            self._recorder_begun = False

    def link_status(self) -> LinkStatus:
        with self._link_lock:
            status = self._link
            arrival = self._last_arrival
        if arrival is None:
            return status
        return replace(status, last_packet_age_s=max(0.0, self._clock() - arrival))

    def _update_link(self, **changes: Any) -> None:
        with self._link_lock:
            self._link = replace(self._link, **changes)

    # -- worker ----------------------------------------------------------------

    def _generate(self, token: CancellationToken) -> Iterator[SourceEvent]:
        source_id = self.source_id
        decoder = LiveDecoder(self.catalog, source_id, self.outputs)
        splitter = FrameSplitter(MAX_FRAME)
        sequence = LinkSequence()
        state = _RunState()
        with self._link_lock:
            self._link = LinkStatus(self.port_name, False, 0, 0, 0, 0, 0, None, None, 0)
            self._last_arrival = None
            self._recorder_begun = False
        port = self._opener(self.port_name)
        try:
            try:
                port.open()
            except SerialPortError as error:
                yield self._problem(
                    "serial_open_failed", f"Could not open {self.port_name}: {error}", False
                )
                return
            self._update_link(connected=True)
            while True:
                token.raise_if_cancelled()
                try:
                    data = port.read(self._read_size, self._read_timeout)
                except SerialPortError as error:
                    yield self._problem(
                        "serial_disconnected",
                        f"{self.port_name} disconnected: {error}. Readings are kept; "
                        "reconnect the cable and click Retry.",
                        False,
                    )
                    return
                if data:
                    yield from self._bytes(data, splitter, sequence, decoder, state)
                yield from self._flush_errors(state, force=False)
        finally:
            try:
                port.close()
            finally:
                self._update_link(connected=False)

    def _problem(self, code: str, message: str, recoverable: bool, **context: Any) -> SourceEvent:
        return SourceEvent(
            SourceEventKind.ERROR,
            self.source_id,
            error=SourceError(code, message, recoverable=recoverable, context=context),
        )

    def _decode_error(
        self, state: _RunState, message: str, **context: Any
    ) -> Iterator[SourceEvent]:
        now = self._clock()
        if now - state.window_start >= 1.0:
            yield from self._flush_errors(state, force=True)
            state.window_start, state.window_errors = now, 0
        if state.window_errors < ERRORS_PER_SECOND:
            state.window_errors += 1
            yield self._problem("decode_error", message, True, **context)
        else:
            state.suppressed += 1

    def _flush_errors(self, state: _RunState, *, force: bool) -> Iterator[SourceEvent]:
        if not state.suppressed:
            return
        now = self._clock()
        if force or now - state.window_start >= 1.0:
            count, state.suppressed = state.suppressed, 0
            state.window_start, state.window_errors = now, 1
            yield self._problem(
                "decode_error",
                f"{count} more frames on {self.port_name} could not be decoded.",
                True,
                frames=count,
            )

    def _bytes(
        self,
        data: bytes,
        splitter: FrameSplitter,
        sequence: LinkSequence,
        decoder: LiveDecoder,
        state: _RunState,
    ) -> Iterator[SourceEvent]:
        oversize = splitter.oversize
        discarded = splitter.discarded_bytes
        frames = splitter.feed(data)
        with self._link_lock:
            self._link = replace(self._link, bytes_received=self._link.bytes_received + len(data))
        if splitter.oversize > oversize:
            state.rejected_frames += splitter.oversize - oversize
            state.rejected_bytes += splitter.discarded_bytes - discarded
            self._update_link(rejected_frames=state.rejected_frames)
            yield from self._decode_error(
                state,
                f"Discarded {splitter.discarded_bytes - discarded} bytes on {self.port_name} "
                "without a frame delimiter (not a firmware packet stream).",
                reason="oversize",
            )
        for frame in frames:
            try:
                packet = parse_packet(cobs_decode(frame))
            except PacketError as error:
                state.rejected_frames += 1
                state.rejected_bytes += len(frame) + 1
                self._update_link(rejected_frames=state.rejected_frames)
                yield from self._decode_error(
                    state,
                    f"Rejected a {len(frame)}-byte frame on {self.port_name}: {error}.",
                    reason=error.reason,
                    bytes=len(frame),
                )
                continue
            yield from self._packet(packet, sequence, decoder, state)

    def _packet(
        self, packet: Packet, sequence: LinkSequence, decoder: LiveDecoder, state: _RunState
    ) -> Iterator[SourceEvent]:
        source_id = self.source_id
        previous_t = state.last_packet_t
        step = sequence.observe(packet.seq)
        lost = 0
        if step.step is SeqStep.GAP:
            lost = step.lost
        elif step.step is SeqStep.DUPLICATE:
            state.duplicates += 1
        elif step.step is SeqStep.BACKWARD and (
            previous_t is not None and packet.t_us + RESTART_BACKSTEP_US < previous_t
        ):
            yield self._problem(
                "device_restarted",
                f"The device on {self.port_name} restarted (its packet count and clock "
                "started over). Readings so far are kept; click Retry to start a new live "
                "session on the new clock.",
                False,
            )
            return
        state.lost_packets += lost
        state.last_packet_t = packet.t_us
        t_before = decoder.t_us or packet.t_us
        self._feed_recorder(packet, t_before, lost, state)
        self._track_carry(packet, state)
        result = decoder.packet(packet)
        with self._link_lock:
            self._last_arrival = self._clock()
            self._link = replace(
                self._link,
                packets=self._link.packets + 1,
                lost_packets=state.lost_packets,
                duplicate_packets=state.duplicates,
                last_seq=packet.seq,
                hidden_readings=decoder.hidden_readings,
            )
            recorder = self._recorder if self._recorder_begun else None
        if recorder is not None:
            recorder.note_time(decoder.t_us)
        for message in result.catalog_mismatch:
            yield self._problem("catalog_mismatch", message, True)
        for message in result.problems:
            yield from self._decode_error(
                state, f"Packet {packet.seq} on {self.port_name}: {message}.", reason="record"
            )
        for description in result.hidden:
            yield self._problem(
                "channel_not_shown",
                f"Readings of {description} are not shown: it was not announced when the "
                "port was opened. Click Retry to include it; recordings keep it.",
                True,
            )
        domain = SHARED_DOMAIN
        link = [("packet_seq", packet.seq)]
        if lost or not state.link_published:
            link.append(("lost_packets", state.lost_packets))
        if state.rejected_frames != state.published_rejected or not state.link_published:
            link.append(("rejected_frames", state.rejected_frames))
            state.published_rejected = state.rejected_frames
        state.link_published = True
        t_ns = packet.t_us * 1000
        for name, value in link:
            yield SourceEvent(
                SourceEventKind.RECORD,
                source_id,
                signal_id=f"{LINK_PREFIX}.{name}",
                sample=SignalSample(
                    t_ns,
                    value,
                    timestamp_domain=domain,
                    source_id=source_id,
                    unit="count",
                    extra={SEQUENCE_KEY: packet.seq},
                ),
            )
        for identifier, sample in result.readings:
            yield SourceEvent(
                SourceEventKind.RECORD, source_id, signal_id=identifier, sample=sample
            )
        if result.heartbeat:
            now = self._clock()
            if state.last_health is None or now - state.last_health >= 0.5:
                state.last_health = now
                yield SourceEvent(SourceEventKind.HEALTH, source_id, health=self.health())
        yield from self._flush_errors(state, force=False)

    def _track_carry(self, packet: Packet, state: _RunState) -> None:
        """Keep the records a recording started mid-stream needs to decode like the live view."""

        ids = self.catalog.ids
        for rtype, payload in packet.records:
            if rtype == REC_CAN_FRAME and len(payload) == 22:
                _t, can_id, flags, dlc = _CAN.unpack_from(payload)
                node = can_id - ids["heartbeat_base"]
                if not flags & 0x01 and dlc == 2 and self.catalog.is_can_node(node):
                    state.heartbeats[node] = payload
            elif rtype == REC_KEY and len(payload) >= 4 and len(state.keys) < 1024:
                state.keys[_U16.unpack_from(payload)[0]] = payload

    def _feed_recorder(self, packet: Packet, t_before: int, lost: int, state: _RunState) -> None:
        with self._link_lock:
            recorder = self._recorder
            begin = recorder is not None and not self._recorder_begun
            if begin:
                self._recorder_begun = True
        if recorder is None:
            state.recorded_rejected = state.rejected_bytes
            return
        if begin:
            # Records received before this packet that set decoder state: the
            # latest heartbeat of each node (whether its clock is synchronized)
            # and the names of car logic outputs.
            heartbeats = sorted(
                ((REC_CAN_FRAME, payload) for payload in state.heartbeats.values()),
                key=lambda item: _CAN.unpack_from(item[1])[0],
            )
            keys = [(REC_KEY, payload) for payload in state.keys.values()]
            recorder.begin(t_before, packet.source, tuple(keys + heartbeats))
            state.recorded_rejected = state.rejected_bytes
            lost = 0
        rejected = state.rejected_bytes - state.recorded_rejected
        state.recorded_rejected = state.rejected_bytes
        recorder.offer(
            packet.t_us,
            packet.records,
            decoder_t_us=t_before,
            lost_packets=lost,
            rejected_bytes=rejected,
        )


@dataclass
class _RunState:
    window_start: float = 0.0
    window_errors: int = 0
    suppressed: int = 0
    rejected_frames: int = 0
    rejected_bytes: int = 0
    recorded_rejected: int = 0
    published_rejected: int = 0
    lost_packets: int = 0
    duplicates: int = 0
    link_published: bool = False
    last_packet_t: int | None = None
    last_health: float | None = None
    heartbeats: dict[int, bytes] = field(default_factory=dict)
    keys: dict[int, bytes] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Preparation


def discover_outputs(
    port_name: str,
    opener: PortOpener,
    token: CancellationToken,
    *,
    seconds: float = DISCOVERY_S,
    catalog: Catalog | None = None,
    clock: Callable[[], float] = monotonic,
) -> tuple[OutputKey, ...]:
    """Open the port, listen for car logic output names, close it again.

    Raises :class:`SerialPortError` if the port cannot be opened, so a missing
    or busy port is reported before the session starts.
    """

    catalog = catalog if catalog is not None else Catalog.bundled()
    found: dict[str, OutputKey] = {}
    port = opener(port_name)
    try:
        port.open()
        splitter = FrameSplitter(MAX_FRAME)
        deadline = clock() + seconds
        while clock() < deadline:
            token.raise_if_cancelled()
            data = port.read(READ_SIZE, READ_TIMEOUT_S)
            for frame in splitter.feed(data) if data else ():
                try:
                    packet = parse_packet(cobs_decode(frame))
                except PacketError:
                    continue
                for rtype, payload in packet.records:
                    if rtype == REC_KEY:
                        key = output_key(catalog, payload)
                        if key is not None and len(found) < 256:
                            found.setdefault(key.name, key)
    finally:
        port.close()
    return tuple(sorted(found.values(), key=lambda key: key.name))


def prepare_serial(
    port_name: str,
    opener: PortOpener,
    token: CancellationToken,
    *,
    discovery_s: float = DISCOVERY_S,
    catalog: Catalog | None = None,
    options: Mapping[str, Any] | None = None,
) -> Callable[[], SerialLiveSource]:
    """Check the port opens and learn output names off the GUI thread; return a factory."""

    catalog = catalog if catalog is not None else Catalog.bundled()
    outputs: tuple[OutputKey, ...] = ()
    try:
        if discovery_s > 0:
            outputs = discover_outputs(
                port_name, opener, token, seconds=discovery_s, catalog=catalog
            )
    except SerialPortError as error:
        raise SerialPortError(f"{port_name}: {error}") from None
    token.raise_if_cancelled()
    extra = dict(options or {})
    return lambda: SerialLiveSource(port_name, opener, catalog=catalog, outputs=outputs, **extra)
