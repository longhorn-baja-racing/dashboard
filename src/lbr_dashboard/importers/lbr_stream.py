"""Firmware live-link framing: COBS frames carrying CRC-checked transport packets.

The format is the firmware's ``docs/protocol.md`` ("Transport packets",
protocol version 4) and is pinned by its ``dev/tests/fixtures/stream_usb.bin``.
USB and WiFi are byte streams: every packet is COBS-encoded and followed by a
``0x00`` delimiter, so a receiver splits at ``0x00`` and resynchronizes after
any corruption (boot ROM text and panic output are simply rejected frames)::

    packet := version u8 (1)  link u8  source u8  record_count u8
              seq u32  t_us u64  records...  crc32 u32
    record := type u8  len u8  payload[len]
    crc32  := CRC-32/IEEE over everything before it

Record types are the ``.lbrlog`` record types (plus a few link-only ones), so
the payloads go to the same decoder as a session log.

Nothing here does I/O; :class:`FrameSplitter` takes bytes as they arrive, in
chunks of any size, and keeps at most one maximum-size frame buffered.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from enum import Enum

PACKET_VERSION = 1
PACKET_HEADER = 16
PACKET_CRC = 4
#: ``LBR_PKT_MAX_SIZE`` in the firmware: the largest packet a link sends.
MAX_PACKET = 1024
#: COBS adds one byte per 254 bytes plus one; anything longer is not a packet.
MAX_FRAME = MAX_PACKET + MAX_PACKET // 254 + 2

LINK_NAMES = {0: "LoRa", 1: "USB", 2: "WiFi", 3: "BLE", 4: "cockpit"}
#: Link-only record types (not ``.lbrlog`` records).
REC_HEALTH_LITE = 0x08
REC_FILE_INFO, REC_FILE_CHUNK, REC_FILE_END = 0x10, 0x11, 0x12

_HEADER = struct.Struct("<BBBBIQ")
_U32 = struct.Struct("<I")


class PacketError(ValueError):
    """A frame that is not a valid transport packet; ``reason`` names why."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def cobs_encode(data: bytes) -> bytes:
    """COBS-encode ``data`` (no trailing delimiter); used to build test streams."""

    out = bytearray()
    block = bytearray()
    for byte in data:
        if byte == 0:
            out.append(len(block) + 1)
            out += block
            block.clear()
            continue
        block.append(byte)
        if len(block) == 0xFE:
            out.append(0xFF)
            out += block
            block.clear()
    out.append(len(block) + 1)
    out += block
    return bytes(out)


def cobs_decode(frame: bytes) -> bytes:
    """Decode one COBS frame (without its ``0x00``) exactly like the reference decoder."""

    out = bytearray()
    index = 0
    size = len(frame)
    while index < size:
        code = frame[index]
        index += 1
        if code == 0 or index + code - 1 > size:
            raise PacketError("cobs", "the frame is not valid COBS")
        out += frame[index : index + code - 1]
        index += code - 1
        if code != 0xFF and index < size:
            out.append(0)
    return bytes(out)


@dataclass(frozen=True, slots=True)
class Packet:
    """One transport packet: header fields and its ``(type, payload)`` records."""

    link: int
    source: int
    seq: int
    t_us: int
    records: tuple[tuple[int, bytes], ...]
    size: int


def build_packet(
    link: int, source: int, seq: int, t_us: int, records: list[tuple[int, bytes]]
) -> bytes:
    """Encode a transport packet (with CRC, without COBS); used to build test streams."""

    body = bytearray(_HEADER.pack(PACKET_VERSION, link, source, len(records), seq, t_us))
    for rtype, payload in records:
        if len(payload) > 0xFF:
            raise ValueError("a packet record holds at most 255 bytes")
        body += bytes((rtype, len(payload))) + payload
    body += _U32.pack(zlib.crc32(body))
    return bytes(body)


def parse_packet(data: bytes) -> Packet:
    """Check and split one decoded packet; raise :class:`PacketError` if it is not one."""

    if len(data) < PACKET_HEADER + PACKET_CRC:
        raise PacketError("short", f"the packet is {len(data)} bytes, shorter than a header")
    if data[0] != PACKET_VERSION:
        raise PacketError("version", f"packet version {data[0]} is not {PACKET_VERSION}")
    body = len(data) - PACKET_CRC
    if _U32.unpack_from(data, body)[0] != zlib.crc32(memoryview(data)[:body]):
        raise PacketError("crc", "the packet CRC does not match")
    records: list[tuple[int, bytes]] = []
    pos = PACKET_HEADER
    while pos < body:
        if pos + 2 > body or pos + 2 + data[pos + 1] > body:
            raise PacketError("framing", "a record runs past the end of the packet")
        length = data[pos + 1]
        records.append((data[pos], data[pos + 2 : pos + 2 + length]))
        pos += 2 + length
    _version, link, source, count, seq, t_us = _HEADER.unpack_from(data)
    if len(records) != count:
        raise PacketError("count", f"the packet says {count} records but holds {len(records)}")
    return Packet(link, source, seq, t_us, tuple(records), len(data))


class FrameSplitter:
    """Split a byte stream at ``0x00`` delimiters, bounded to one maximum frame.

    Bytes before the first delimiter of a connection may be the tail of a
    frame that started before the port was opened; they are reported like any
    other undecodable frame. A run of more than :data:`MAX_FRAME` bytes
    without a delimiter is discarded (and counted once) up to the next
    delimiter, so line noise cannot grow the buffer.
    """

    def __init__(self, max_frame: int = MAX_FRAME) -> None:
        self._max = max_frame
        self._buffer = bytearray()
        self._discarding = False
        self.oversize = 0
        self.discarded_bytes = 0

    @property
    def pending(self) -> int:
        """Bytes of an incomplete frame held for the next chunk."""

        return len(self._buffer)

    def feed(self, data: bytes) -> list[bytes]:
        """Return every complete, nonempty frame that ``data`` finishes."""

        frames: list[bytes] = []
        start = 0
        size = len(data)
        while start < size:
            end = data.find(0, start)
            piece = data[start:] if end < 0 else data[start:end]
            if self._discarding:
                self.discarded_bytes += len(piece)
            else:
                self._buffer += piece
                if len(self._buffer) > self._max:
                    self.oversize += 1
                    self.discarded_bytes += len(self._buffer)
                    self._buffer.clear()
                    self._discarding = True
            if end < 0:
                break
            if not self._discarding and self._buffer:
                frames.append(bytes(self._buffer))
            self._buffer.clear()
            self._discarding = False
            start = end + 1
        return frames

    def reset(self) -> None:
        self._buffer.clear()
        self._discarding = False


class SeqStep(str, Enum):
    FIRST = "first"
    NEXT = "next"
    GAP = "gap"
    DUPLICATE = "duplicate"
    BACKWARD = "backward"


@dataclass(frozen=True, slots=True)
class SeqResult:
    step: SeqStep
    lost: int = 0


class LinkSequence:
    """Per-link packet sequence (32 bits, wraps): 1 = next, 0 = duplicate, >1 = gap.

    A step backwards (more than half the counter range ahead) means the sender
    restarted its count or the packet is late; over a serial link packets
    cannot be reordered, so the caller decides which from the packet time.
    """

    MODULUS = 1 << 32

    def __init__(self) -> None:
        self.last: int | None = None

    def observe(self, seq: int) -> SeqResult:
        last, self.last = self.last, seq
        if last is None:
            return SeqResult(SeqStep.FIRST)
        delta = (seq - last) % self.MODULUS
        if delta == 1:
            return SeqResult(SeqStep.NEXT)
        if delta == 0:
            return SeqResult(SeqStep.DUPLICATE)
        if delta < self.MODULUS // 2:
            return SeqResult(SeqStep.GAP, delta - 1)
        return SeqResult(SeqStep.BACKWARD)

    def restart(self) -> None:
        self.last = None
