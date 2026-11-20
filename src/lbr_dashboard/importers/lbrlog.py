"""Reader for firmware ``.lbrlog`` session logs (log format version 1).

The format is defined by the firmware's ``docs/protocol.md`` ("Log format",
protocol version 4) and pinned by its ``dev/tests/fixtures/session.lbr``. A log
is a sequence of self-delimiting records::

    record := 0xA5 0x5A  type u8  flags u8 (=0)  len u16  payload[len]  crc32 u32
    crc32  := CRC-32/IEEE over type, flags, len, payload

Decoding follows the firmware's reference decoder (``dev/tools/lbr_decode.py``)
record for record, so a value read here is the value the CSV export shows:

- Names, value types, units and every CAN frame layout come from the signal
  catalog, never from tables in this file. The catalog must have the hash the
  log's SESSION record names, so names are never put on the wrong signals.
- A bad CRC or garbage is skipped up to the next valid record and reported as a
  corrupt region with its file offset and length. An incomplete record at the
  end (power loss mid-write) is reported as a truncated tail.
- Records with a valid CRC but an impossible payload are rejected and listed
  with their offset; unknown record types are skipped by length and counted.
- Unavailable samples keep their reason and are never turned into zero.
  Samples taken before a node was synchronized keep their own clock.

Reading is a single streaming pass over the file opened read-only, with
progress and cancellation checks before every 1 MiB read. The input file
is never written.
"""

from __future__ import annotations

import hashlib
import json
import struct
import zlib
from array import array
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

import numpy as np
import numpy.typing as npt

from ..core.lbr_session import NODE_CLOCK, SHARED_CLOCK
from ..core.lifecycle import CancellationToken

LOG_FORMAT_VERSION = 1
PROTOCOL_VERSION = 4

SYNC0, SYNC1 = 0xA5, 0x5A
HEADER = 6
TRAILER = 4
MAX_PAYLOAD = 2048
MAX_RECORD = HEADER + MAX_PAYLOAD + TRAILER
SESSION_FIXED = 32

READ_CHUNK = 1 << 20
#: Regions and rejected records listed individually; the rest are counted.
MAX_REPORTED = 100
MAX_CHANNELS = 4096
INT64_MAX = (1 << 63) - 1
#: Largest time in microseconds that still fits signed 64-bit nanoseconds.
MAX_TIME_US = INT64_MAX // 1000

REC_SESSION, REC_SAMPLE, REC_HEALTH, REC_CAN_FRAME = 0x01, 0x02, 0x03, 0x04
REC_LOSS, REC_TEXT, REC_END = 0x05, 0x06, 0x07
REC_KEY, REC_VALUE, REC_TICK = 0x20, 0x21, 0x22
RECORD_NAMES = {
    REC_SESSION: "SESSION",
    REC_SAMPLE: "SAMPLE",
    REC_HEALTH: "HEALTH",
    REC_CAN_FRAME: "CAN_FRAME",
    REC_LOSS: "LOSS",
    REC_TEXT: "TEXT",
    REC_END: "END",
    REC_KEY: "KEY",
    REC_VALUE: "VALUE",
    REC_TICK: "TICK",
}
CAN_FLAG_EXTENDED = 0x01

#: Car logic output status (``lbr_status`` in the firmware), by code.
OUTPUT_STATUS = (
    "OK",
    "UNSPECIFIED",
    "NOT_FITTED",
    "NO_DATA",
    "SENSOR_FAULT",
    "OUT_OF_RANGE",
    "NOT_CALIBRATED",
    "BUS_ERROR",
    "STALE",
    "NODE_LOST",
    "NODE_MISSING",
    "BAD_MATH",
)

HEALTH_PAGES = 12
#: Heartbeat (health) pages: struct layout and field names; ``None`` is reserved (zero).
HEALTH_FIELDS: tuple[tuple[str, tuple[str | None, ...]], ...] = (
    ("<IBBH", ("uptime_ms", "reset_reason", "state", "flags")),
    ("<IHH", ("fw_version", "protocol_version", "boot_count")),
    ("<II", ("catalog_hash", "config_crc")),
    ("<BBHI", ("can_tec", "can_rec", None, "can_bus_off_count")),
    ("<II", ("can_tx_dropped", "can_rx_dropped")),
    ("<II", ("overflow_count", "queue_high_water")),
    ("<II", ("write_error_count", "transport_error_count")),
    ("<II", ("sensor_status", "calibration_state")),
    ("<II", ("last_sample_age_ms", "last_sd_write_age_ms")),
    ("<HHHH", ("acquire_hz", "publish_hz", "log_hz", None)),
    ("<Ii", ("time_step_count", "last_sync_error_us")),
    ("<II", ("rejected_count", "can_bus_errors")),
)
_HEALTH_STRUCTS = tuple(struct.Struct(layout) for layout, _ in HEALTH_FIELDS)

_ENCODINGS = {
    # name: (struct format, raw value of unavailable reason 0)
    "i16": ("<h", -32768),
    "u16": ("<H", 0xFFF8),
    "u8": ("<B", 0xF8),
    "i32": ("<i", -2147483648),
    "u32": ("<I", 0xFFFFFFF8),
}
_SAMPLE = struct.Struct("<HBBIQI")
_CAN = struct.Struct("<QIBB")
_SESSION = struct.Struct("<HHIIIBBHIQ")
_LOSS = struct.Struct("<IIQ")
_END = struct.Struct("<QI")
_KEY = struct.Struct("<HBB")
_VALUE = struct.Struct("<QHBI")
_TICK = struct.Struct("<QBBH")
_U32 = struct.Struct("<I")
_I32 = struct.Struct("<i")
_F32 = struct.Struct("<f")

#: ``progress(bytes_read, total_bytes)``.
Progress = Callable[[int, int], None]


class LbrlogError(ValueError):
    """The file cannot be read as an ``.lbrlog`` session log."""


class _Rejected(Exception):
    """A record with a valid CRC whose payload this reader cannot accept."""


def _f32(value: float) -> float:
    """Round to IEEE single precision, like the firmware's float fields."""

    return _F32.unpack(_F32.pack(value))[0]


def _bits_to_f32(bits: int) -> float:
    return _F32.unpack(_U32.pack(bits))[0]


def _f32_bits(value: float) -> int:
    return _U32.unpack(_F32.pack(value))[0]


# --------------------------------------------------------------------------
# Catalog


@dataclass(frozen=True, slots=True)
class FieldSpec:
    signal: int
    offset: int
    unpack: Callable[[Any, int], tuple[Any, ...]]
    unavailable_base: int
    scale32: float


@dataclass(frozen=True, slots=True)
class FrameSpec:
    can_id: int
    name: str
    sender: int
    rate_hz: int
    period_us: int
    dlc: int
    counter_offset: int | None
    fields: tuple[FieldSpec, ...]


def catalog_hash(raw: Mapping[str, Any]) -> int:
    """``lbr_catalog_hash`` over the signal and frame tables (docs/protocol.md)."""
    crc = 0
    for signal in raw["signals"]:
        crc = zlib.crc32(
            struct.pack(
                "<HBB",
                signal["id"],
                raw["value_types"].index(signal["type"]),
                raw["units"].index(signal["unit"]),
            ),
            crc,
        )
        crc = zlib.crc32(signal["name"].encode("ascii") + b"\0", crc)
    for frame in raw["frames"]:
        counter = 0xFF if frame["counter_offset"] is None else frame["counter_offset"]
        crc = zlib.crc32(
            struct.pack(
                "<HBHBBB",
                frame["id"],
                frame["sender"],
                frame["rate_hz"],
                frame["dlc"],
                counter,
                len(frame["fields"]),
            ),
            crc,
        )
        for item in frame["fields"]:
            crc = zlib.crc32(
                struct.pack(
                    "<HBBI",
                    item["signal"],
                    item["offset"],
                    raw["encodings"].index(item["enc"]),
                    _f32_bits(item["scale"]),
                ),
                crc,
            )
        crc = zlib.crc32(frame["name"].encode("ascii") + b"\0", crc)
    return crc & 0xFFFFFFFF


class Catalog:
    """Signal and CAN frame metadata from a firmware ``catalog.json``."""

    def __init__(self, raw: Mapping[str, Any], origin: str) -> None:
        try:
            self.origin = origin
            self.raw = raw
            self.hash = int(raw["catalog_hash"])
            self.types: tuple[str, ...] = tuple(raw["value_types"])
            self.units: tuple[str, ...] = tuple(raw["units"])
            self.reasons: tuple[str, ...] = tuple(raw["unavailable_reasons"])
            self.signals: dict[int, Mapping[str, Any]] = {s["id"]: s for s in raw["signals"]}
            self.by_name = {s["name"]: s["id"] for s in raw["signals"]}
            self.events = {e["code"]: e["name"] for e in raw["events"]}
            self.event_ids = {s["id"] for s in raw["signals"] if s["name"] in ("EVENT", "FAULT")}
            self.nodes: dict[int, str] = {n["id"]: n["name"] for n in raw["nodes"]}
            self.ids: Mapping[str, int] = raw["can_ids"]
            self.err_flags: Mapping[str, int] = raw["heartbeat_err_flags"]
            self.commands = {c["code"]: c["name"] for c in raw["commands"]}
            self.central = next(i for i, name in self.nodes.items() if name == "central")
            self.frames: dict[int, FrameSpec] = {}
            self.signal_rate: dict[int, int] = {}
            for frame in raw["frames"]:
                fields = []
                for item in frame["fields"]:
                    fmt, base = _ENCODINGS[item["enc"]]
                    fields.append(
                        FieldSpec(
                            item["signal"],
                            item["offset"],
                            struct.Struct(fmt).unpack_from,
                            base,
                            _f32(item["scale"]),
                        )
                    )
                    self.signal_rate.setdefault(item["signal"], frame["rate_hz"])
                self.frames[frame["id"]] = FrameSpec(
                    frame["id"],
                    frame["name"],
                    frame["sender"],
                    frame["rate_hz"],
                    1_000_000 // frame["rate_hz"],
                    frame["dlc"],
                    frame["counter_offset"],
                    tuple(fields),
                )
            computed = self.computed_hash()
        except (KeyError, TypeError, ValueError, StopIteration, ZeroDivisionError) as error:
            raise LbrlogError(f"{origin}: not a usable firmware catalog ({error!r})") from None
        if computed != self.hash:
            raise LbrlogError(
                f"{origin}: catalog content does not match its own catalog_hash "
                f"({computed} != {self.hash})"
            )

    @classmethod
    def bundled(cls) -> Catalog:
        """The catalog this dashboard ships with (protocol version 4)."""

        from .lbr_catalog_data import CATALOG_JSON

        return cls(json.loads(CATALOG_JSON), "bundled catalog")

    @classmethod
    def from_file(cls, path: Path) -> Catalog:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as error:
            raise LbrlogError(f"{path}: cannot read catalog ({error})") from None
        if not isinstance(raw, Mapping):
            raise LbrlogError(f"{path}: not a usable firmware catalog")
        return cls(raw, str(path))

    def computed_hash(self) -> int:
        return catalog_hash(self.raw)

    def node(self, node_id: int) -> str:
        return self.nodes.get(node_id, f"node{node_id}")

    def is_can_node(self, node_id: int) -> bool:
        return 1 <= node_id <= self.ids["can_node_max"]

    def reason(self, code: int) -> str:
        return self.reasons[code] if 0 <= code < len(self.reasons) else f"reason_{code}"

    def can_sender(self, can_id: int) -> int:
        ids = self.ids
        if can_id > 0x7FF:
            return 0
        if can_id in (
            ids["time_sync"],
            ids["time_follow_up"],
            ids["command"],
            ids["update_ctrl"],
            ids["update_data"],
        ):
            return self.central
        for base in (ids["event_base"], ids["heartbeat_base"], ids["update_status_base"]):
            if base < can_id <= base + ids["can_node_max"]:
                return can_id - base
        if can_id >= ids["health_base"] + 16:
            node, page = (can_id - ids["health_base"]) >> 4, can_id & 0xF
            return node if self.is_can_node(node) and page < HEALTH_PAGES else 0
        frame = self.frames.get(can_id)
        return frame.sender if frame else 0


def candidate_catalogs(path: Path) -> tuple[Catalog, ...]:
    """A ``catalog.json`` next to the log (if readable), then the bundled one."""

    candidates = []
    sibling = path.with_name("catalog.json")
    if sibling.is_file():
        try:
            candidates.append(Catalog.from_file(sibling))
        except LbrlogError:
            pass
    candidates.append(Catalog.bundled())
    return tuple(candidates)


# --------------------------------------------------------------------------
# Result model


@dataclass(frozen=True, slots=True)
class ChannelKey:
    """Identity of one stream in a log: node, record kind, name and clock."""

    node: str
    kind: str
    name: str
    clock: str = SHARED_CLOCK


@dataclass(frozen=True, eq=False)
class LogChannel:
    """Samples of one channel in log order.

    ``values`` holds the number (NaN where unavailable), ``status`` indexes
    ``statuses`` (``ok``, an unavailable reason, or heartbeat error flags) and
    ``texts`` holds strings for text channels and event names for events.
    ``order`` is each sample's position among all samples of the log and
    ``seq`` the stream's sequence number (-1 for records that have none).
    """

    key: ChannelKey
    value_type: str
    unit: str
    description: str
    node_id: int | None
    signal_id: int | None
    rate_hz: int | None
    order: npt.NDArray[np.int64]
    times_us: npt.NDArray[np.int64]
    values: npt.NDArray[np.float64]
    available: npt.NDArray[np.bool_]
    status: npt.NDArray[np.uint32]
    seq: npt.NDArray[np.int64]
    statuses: tuple[str, ...]
    texts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for item in (
            self.order,
            self.times_us,
            self.values,
            self.available,
            self.status,
            self.seq,
        ):
            item.setflags(write=False)

    @property
    def count(self) -> int:
        return int(self.times_us.size)


@dataclass(frozen=True, slots=True)
class SkippedRegion:
    offset: int
    length: int


@dataclass(frozen=True, slots=True)
class RejectedRecord:
    offset: int
    record_type: int
    reason: str


@dataclass(frozen=True)
class LogSession:
    """The SESSION record: versions, logger and the validated configuration."""

    format_version: int
    protocol_version: int
    fw_version: int
    catalog_hash: int
    config_crc: int
    node_id: int
    logger: str
    reset_reason: int
    boot_count: int
    start_time_us: int
    config_text: str

    @property
    def config(self) -> tuple[tuple[str, str], ...]:
        pairs = []
        for line in self.config_text.splitlines():
            key, sep, value = line.partition("=")
            pairs.append((key, value) if sep else ("", line))
        return tuple(pairs)


@dataclass(frozen=True)
class LogIntegrity:
    """Everything the reader skipped, rejected or merely counted."""

    file_size: int
    sha256: str
    records: int
    record_bytes: int
    corrupt_regions: int
    skipped_bytes: int
    regions: tuple[SkippedRegion, ...]
    truncated_tail: int
    truncated_offset: int | None
    rejected: int
    rejected_records: tuple[RejectedRecord, ...]
    unknown_records: Mapping[int, int]
    time_sync_frames: int
    time_follow_up_frames: int
    update_frames: int
    tick_records: int
    catalog_checked: bool
    catalog_origin: str
    records_written: int | None
    dropped_records: int
    dropped_bytes: int

    @property
    def damaged(self) -> bool:
        """Bytes or records of the file could not be used."""

        return bool(self.corrupt_regions or self.truncated_tail or self.rejected)

    def problems(self) -> tuple[str, ...]:
        """Plain sentences for every skipped, rejected or lost part of the session."""

        lines = []
        if self.corrupt_regions:
            where = ", ".join(f"{r.offset} ({r.length} bytes)" for r in self.regions[:5])
            more = "" if self.corrupt_regions <= 5 else f" and {self.corrupt_regions - 5} more"
            lines.append(
                f"skipped {self.skipped_bytes} corrupt bytes in {self.corrupt_regions} "
                f"region(s) at offset {where}{more}"
            )
        if self.truncated_tail:
            lines.append(
                f"ignored a {self.truncated_tail}-byte incomplete record at the end "
                f"(offset {self.truncated_offset})"
            )
        if self.rejected:
            where = ", ".join(
                f"{RECORD_NAMES.get(r.record_type, f'type {r.record_type}')} at {r.offset}: "
                f"{r.reason}"
                for r in self.rejected_records[:3]
            )
            more = "" if self.rejected <= 3 else f" and {self.rejected - 3} more"
            lines.append(f"rejected {self.rejected} malformed record(s) ({where}{more})")
        if self.dropped_records:
            lines.append(
                f"the logger dropped {self.dropped_records} record(s) "
                f"({self.dropped_bytes} bytes) while writing"
            )
        if not self.catalog_checked:
            lines.append(f"no SESSION record, so the {self.catalog_origin} was not checked")
        if self.records_written is None:
            lines.append("no END record (the log was not closed cleanly)")
        if self.unknown_records:
            kinds = ", ".join(f"type {t}: {n}" for t, n in sorted(self.unknown_records.items()))
            lines.append(f"skipped record types this reader does not know ({kinds})")
        return tuple(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_size": self.file_size,
            "sha256": self.sha256,
            "records": self.records,
            "record_bytes": self.record_bytes,
            "corrupt_regions": self.corrupt_regions,
            "skipped_bytes": self.skipped_bytes,
            "regions": [[r.offset, r.length] for r in self.regions],
            "truncated_tail": self.truncated_tail,
            "truncated_offset": self.truncated_offset,
            "rejected": self.rejected,
            "rejected_records": [
                [r.offset, r.record_type, r.reason] for r in self.rejected_records
            ],
            "unknown_records": {str(t): n for t, n in sorted(self.unknown_records.items())},
            "time_sync_frames": self.time_sync_frames,
            "time_follow_up_frames": self.time_follow_up_frames,
            "update_frames": self.update_frames,
            "tick_records": self.tick_records,
            "catalog_checked": self.catalog_checked,
            "catalog_origin": self.catalog_origin,
            "records_written": self.records_written,
            "dropped_records": self.dropped_records,
            "dropped_bytes": self.dropped_bytes,
        }


@dataclass(frozen=True)
class LbrLog:
    path: str
    session: LogSession | None
    channels: tuple[LogChannel, ...]
    integrity: LogIntegrity

    @property
    def sample_count(self) -> int:
        return sum(channel.count for channel in self.channels)

    def channel(self, node: str, kind: str, name: str, clock: str = SHARED_CLOCK) -> LogChannel:
        key = ChannelKey(node, kind, name, clock)
        return next(c for c in self.channels if c.key == key)


# --------------------------------------------------------------------------
# Framing


class RecordScanner:
    """Split a byte stream into CRC-checked records with the spec's recovery rules.

    Equivalent to the reference decoder's ``LogReader`` but streaming: bytes
    that are not a valid record are skipped up to the next one and counted as a
    corrupt region; an incomplete record with no valid record after it is the
    truncated tail. Only a sync byte can start a record, so garbage is skipped
    to the next ``0xA5`` in one step.
    """

    def __init__(
        self,
        stream: BinaryIO,
        chunk_size: int = READ_CHUNK,
        on_read: Callable[[int], None] | None = None,
    ) -> None:
        self._stream = stream
        self._chunk = max(chunk_size, MAX_RECORD)
        self._on_read = on_read
        self._buffer = b""
        self._base = 0
        self._pos = 0
        self._eof = False
        self._digest = hashlib.sha256()
        self.bytes_read = 0
        self.record_bytes = 0
        self.corrupt_regions = 0
        self.skipped_bytes = 0
        self.regions: list[SkippedRegion] = []
        self.truncated_tail = 0
        self.truncated_offset: int | None = None

    @property
    def position(self) -> int:
        """File offset of the next byte to examine."""

        return self._base + self._pos

    @property
    def sha256(self) -> str:
        return self._digest.hexdigest()

    def _fill(self) -> None:
        if self._eof or len(self._buffer) - self._pos >= MAX_RECORD:
            return
        parts = [self._buffer[self._pos :]]
        have = len(parts[0])
        while have < self._chunk:
            if self._on_read is not None:
                self._on_read(self.bytes_read)
            data = self._stream.read(self._chunk - have)
            if not data:
                self._eof = True
                break
            self._digest.update(data)
            self.bytes_read += len(data)
            parts.append(data)
            have += len(data)
        self._base += self._pos
        self._pos = 0
        self._buffer = b"".join(parts)

    def _record_at(self, pos: int) -> int:
        """Length of the valid record at ``pos`` in the buffer, or 0."""

        data = self._buffer
        left = len(data) - pos
        if left < HEADER + TRAILER or data[pos] != SYNC0 or data[pos + 1] != SYNC1:
            return 0
        if data[pos + 3] != 0:
            return 0
        length = data[pos + 4] | data[pos + 5] << 8
        size = HEADER + length + TRAILER
        if length > MAX_PAYLOAD or left < size:
            return 0
        end = pos + HEADER + length
        if _U32.unpack_from(data, end)[0] != zlib.crc32(memoryview(data)[pos + 2 : end]):
            return 0
        return size

    def _tail_candidate(self, pos: int) -> bool:
        data = self._buffer
        left = len(data) - pos
        if data[pos] != SYNC0 or (left >= 2 and data[pos + 1] != SYNC1):
            return False
        if left < HEADER:
            return True
        length = data[pos + 4] | data[pos + 5] << 8
        return length <= MAX_PAYLOAD and left < HEADER + length + TRAILER

    def __iter__(self) -> Iterator[tuple[int, int, memoryview]]:
        """Yield ``(file offset, record type, payload)`` for every valid record."""

        in_garbage = False
        while True:
            self._fill()
            data, pos = self._buffer, self._pos
            if pos >= len(data):
                return
            size = self._record_at(pos)
            if size:
                in_garbage = False
                self._pos = pos + size
                self.record_bytes += size
                yield (
                    self._base + pos,
                    data[pos + 2],
                    memoryview(data)[pos + HEADER : pos + size - TRAILER],
                )
                continue
            # A tail candidate lies within one record of the end; ``_fill``
            # guarantees the whole rest of the file is in the buffer then.
            if (
                self._eof
                and self._tail_candidate(pos)
                and not any(self._record_at(q) for q in range(pos + 1, len(data)))
            ):
                self.truncated_tail = len(data) - pos
                self.truncated_offset = self._base + pos
                self._pos = len(data)
                return
            following = data.find(SYNC0, pos + 1)
            if following < 0:
                following = len(data)
            if not in_garbage:
                self.corrupt_regions += 1
                if len(self.regions) < MAX_REPORTED:
                    self.regions.append(SkippedRegion(self._base + pos, 0))
                in_garbage = True
            skipped = following - pos
            self.skipped_bytes += skipped
            if self.corrupt_regions <= MAX_REPORTED:
                last = self.regions[-1]
                self.regions[-1] = SkippedRegion(last.offset, last.length + skipped)
            self._pos = following


# --------------------------------------------------------------------------
# Decoding


def _unwrap24(t_lo: int, reference: int) -> int:
    delta = (reference - t_lo) & 0xFFFFFF
    if delta >= 0x800000:
        delta -= 0x1000000
    return max(reference - delta, 0)


def _grid_index(counter: int, rx_us: int, period: int) -> int:
    """The grid index nearest the receive time whose low 16 bits are ``counter``."""

    k_rx = rx_us // period
    delta = (k_rx - counter) & 0xFFFF
    if delta >= 0x8000:
        delta -= 0x10000
    k = k_rx - delta
    return counter if k < 0 else k


@dataclass
class _ChannelBuffer:
    key: ChannelKey
    value_type: str
    unit: str
    description: str
    node_id: int | None
    signal_id: int | None
    rate_hz: int | None
    order: array[int] = field(default_factory=lambda: array("q"))
    times: array[int] = field(default_factory=lambda: array("q"))
    values: array[float] = field(default_factory=lambda: array("d"))
    available: array[int] = field(default_factory=lambda: array("b"))
    status: array[int] = field(default_factory=lambda: array("I"))
    seq: array[int] = field(default_factory=lambda: array("q"))
    statuses: dict[str, int] = field(default_factory=lambda: {"ok": 0})
    texts: list[str] = field(default_factory=list)

    def freeze(self) -> LogChannel:
        return LogChannel(
            self.key,
            self.value_type,
            self.unit,
            self.description,
            self.node_id,
            self.signal_id,
            self.rate_hz,
            np.array(self.order, dtype=np.int64),
            np.array(self.times, dtype=np.int64),
            np.array(self.values, dtype=np.float64),
            np.array(self.available, dtype=np.bool_),
            np.array(self.status, dtype=np.uint32),
            np.array(self.seq, dtype=np.int64),
            tuple(self.statuses),
            tuple(self.texts),
        )


#: Value types whose samples share one generic data type; a channel cannot mix groups.
TYPE_GROUPS = {
    "f32": "numeric",
    "i32": "numeric",
    "u32": "numeric",
    "raw": "numeric",
    "none": "numeric",
    "count": "numeric",
    "bool": "boolean",
    "status": "status",
    "event": "event",
    "text": "string",
}

_NAN = float("nan")


class _Decoder:
    """Turns records into channels, following the reference decoder's rules."""

    def __init__(self, catalogs: Sequence[Catalog]) -> None:
        if not catalogs:
            raise ValueError("at least one catalog is required")
        self._candidates = tuple(catalogs)
        self.catalog: Catalog | None = None
        self.catalog_checked = False
        self.session: LogSession | None = None
        self.channels: dict[ChannelKey, _ChannelBuffer] = {}
        self.order = 0
        self.t_us = 0  # latest shared time, for records that carry none
        self.logger = ""
        self.heartbeat: dict[int, int] = {}
        self.keys: dict[int, tuple[str, str, str]] = {}  # key id -> (name, value type, unit)
        self._plans: dict[tuple[int, bool], list[tuple[Any, ...]] | None] = {}
        self.records = 0
        self.rejected = 0
        self.rejected_records: list[RejectedRecord] = []
        self.unknown: Counter[int] = Counter()
        self.time_sync = self.follow_up = self.update = self.ticks = 0
        self.records_written: int | None = None
        self.dropped_records = self.dropped_bytes = 0

    # -- helpers ---------------------------------------------------------

    def _channel(
        self,
        key: ChannelKey,
        value_type: str,
        unit: str = "",
        *,
        node_id: int | None = None,
        signal_id: int | None = None,
    ) -> _ChannelBuffer:
        channel = self.channels.get(key)
        if channel is None:
            if len(self.channels) >= MAX_CHANNELS:
                raise _Rejected(f"more than {MAX_CHANNELS} channels")
            catalog = self.catalog
            info = catalog.signals.get(signal_id) if catalog and signal_id is not None else None
            rate = catalog.signal_rate.get(signal_id) if catalog and signal_id is not None else None
            channel = _ChannelBuffer(
                key,
                value_type,
                unit,
                str(info.get("desc", "")) if info else "",
                node_id,
                signal_id,
                rate,
            )
            self.channels[key] = channel
        elif TYPE_GROUPS[channel.value_type] != TYPE_GROUPS[value_type]:
            raise _Rejected(
                f"{key.node} {key.name}: value type changed from {channel.value_type} "
                f"to {value_type}"
            )
        return channel

    def _append(
        self,
        channel: _ChannelBuffer,
        t_us: int,
        value: float,
        status: str = "ok",
        *,
        available: bool = True,
        text: str | None = None,
        seq: int = -1,
    ) -> None:
        index = channel.statuses.setdefault(status, len(channel.statuses))
        channel.order.append(self.order)
        channel.times.append(t_us)
        channel.values.append(value)
        channel.available.append(available)
        channel.status.append(index)
        channel.seq.append(seq)
        if text is not None:
            channel.texts.append(text)
        self.order += 1

    @staticmethod
    def _check_time(t_us: int) -> None:
        if t_us > MAX_TIME_US:
            raise _Rejected(f"time {t_us} us is beyond the signed 64-bit nanosecond range")

    def _synced(self, node: int) -> bool:
        assert self.catalog is not None
        if node == self.catalog.central:
            return True
        return node in self.heartbeat and not (self.heartbeat[node] & 0x01)

    def _forget_sync(self, source: int, bits: int) -> None:
        """A node that booted, or was lost or found again, is unsynced until its next heartbeat."""

        assert self.catalog is not None
        name = self.catalog.events.get(bits & 0xFFFF)
        if name not in ("BOOT", "NODE_LOST", "NODE_FOUND"):
            return
        node = source if name == "BOOT" else bits >> 16
        if self.catalog.is_can_node(node):
            self.heartbeat[node] = 0x01

    def _sample(
        self,
        signal: int,
        source: int,
        type_name: str,
        valid: bool,
        synced: bool,
        seq: int,
        t_us: int,
        bits: int,
    ) -> None:
        """One measurement, from a SAMPLE record or a CAN frame (like the CSV export)."""

        catalog = self.catalog
        assert catalog is not None
        self._check_time(t_us)
        info = catalog.signals.get(signal)
        node = catalog.node(source)
        clock = SHARED_CLOCK if synced else NODE_CLOCK
        text = None
        if signal in catalog.event_ids:
            name, unit, type_name = str(info["name"]) if info else "EVENT", "", "event"
            text = catalog.events.get(bits & 0xFFFF, f"code_{bits & 0xFFFF}") if valid else ""
        else:
            name = str(info["name"]) if info else f"signal_0x{signal:04x}"
            unit = str(info["unit"]) if info else ""
        channel = self._channel(
            ChannelKey(node, "sample", name, clock),
            type_name,
            unit,
            node_id=source,
            signal_id=signal,
        )
        if not valid:
            value = _NAN
        elif type_name == "event":
            value = float(bits >> 16)  # the argument; the event name is in ``texts``
        elif type_name == "f32":
            value = _bits_to_f32(bits)
        elif type_name == "i32":
            value = float(_I32.unpack(_U32.pack(bits))[0])
        elif type_name == "bool":
            value = 1.0 if bits else 0.0
        else:  # u32, status, raw, none: the unsigned 32-bit value
            value = float(bits)
        status = "ok" if valid else catalog.reason(bits)
        self._append(channel, t_us, value, status, available=valid, text=text, seq=seq)

    def _health_fields(self, page: int, data: Any, offset: int = 0) -> list[tuple[str, int]]:
        names = HEALTH_FIELDS[page][1]
        out = []
        for name, value in zip(names, _HEALTH_STRUCTS[page].unpack_from(data, offset)):
            if name is None:
                if value != 0:
                    raise _Rejected(f"reserved field of health page {page} is set")
            else:
                out.append((name, value))
        return out

    def _health(self, node_id: int, fields: list[tuple[str, int]], t_us: int) -> None:
        assert self.catalog is not None
        node = self.catalog.node(node_id)
        for name, value in fields:
            channel = self._channel(ChannelKey(node, "health", name), "count", node_id=node_id)
            self._append(channel, t_us, float(value))

    def _text(self, node_id: int | None, kind: str, t_us: int, text: str) -> None:
        node = self.catalog.node(node_id) if self.catalog and node_id is not None else self.logger
        channel = self._channel(ChannelKey(node or "logger", kind, ""), "text", node_id=node_id)
        self._append(channel, t_us, _NAN, text=text)

    def _reject(self, offset: int, rtype: int, reason: str) -> None:
        self.rejected += 1
        if len(self.rejected_records) < MAX_REPORTED:
            self.rejected_records.append(RejectedRecord(offset, rtype, reason))

    def _choose_catalog(self, wanted: int | None) -> None:
        if wanted is None:
            self.catalog = self._candidates[-1]
            return
        for candidate in self._candidates:
            if candidate.hash == wanted:
                self.catalog = candidate
                self.catalog_checked = True
                return
        known = ", ".join(f"{c.hash} ({c.origin})" for c in self._candidates)
        raise LbrlogError(
            f"This log was written with signal catalog {wanted}, but the dashboard has "
            f"{known}. Put the catalog.json of the firmware that wrote it next to the "
            "log file and open it again, so names are never put on the wrong signals."
        )

    # -- records ---------------------------------------------------------

    @staticmethod
    def _check_versions(fmt: int, proto: int) -> None:
        if fmt != LOG_FORMAT_VERSION:
            raise LbrlogError(
                f"This log uses .lbrlog format version {fmt}; this dashboard reads "
                f"version {LOG_FORMAT_VERSION} only. Use the dashboard release that "
                "matches the firmware, or export the log to CSV with that firmware's "
                "lbr_decode.py."
            )
        if proto != PROTOCOL_VERSION:
            raise LbrlogError(
                f"This log was written with protocol version {proto}; this dashboard "
                f"reads protocol version {PROTOCOL_VERSION} only. Logs of another "
                "protocol version need that version's decoder."
            )

    def record(self, offset: int, rtype: int, payload: memoryview) -> None:
        self.records += 1
        if self.catalog is None:
            if rtype == REC_SESSION and len(payload) >= SESSION_FIXED:
                fmt, proto, _fw, chash = _SESSION.unpack_from(payload)[:4]
                self._check_versions(fmt, proto)
                self._choose_catalog(chash)
            else:
                self._choose_catalog(None)
        try:
            self._record(offset, rtype, payload)
        except _Rejected as error:
            self._reject(offset, rtype, str(error))

    def _record(self, offset: int, rtype: int, p: memoryview) -> None:
        catalog = self.catalog
        assert catalog is not None
        size = len(p)

        def need(expected: int, *, at_least: bool = False) -> None:
            if size < expected if at_least else size != expected:
                raise _Rejected(f"payload is {size} bytes, expected {expected}")

        if rtype == REC_SESSION:
            need(SESSION_FIXED, at_least=True)
            fmt, proto, fw, chash, ccrc, node, reset, _, boots, start = _SESSION.unpack_from(p)
            self._check_versions(fmt, proto)
            if chash != catalog.hash:
                raise LbrlogError(
                    f"SESSION record at offset {offset} names catalog {chash}, but this "
                    f"log started with catalog {catalog.hash}."
                )
            self.catalog_checked = True
            self.t_us = start
            self.logger = catalog.node(node)
            self.session = LogSession(
                fmt,
                proto,
                fw,
                chash,
                ccrc,
                node,
                self.logger,
                reset,
                boots,
                start,
                bytes(p[SESSION_FIXED:]).decode("latin-1"),
            )
        elif rtype == REC_SAMPLE:
            need(_SAMPLE.size)
            signal, source, flags, seq, t_us, bits = _SAMPLE.unpack_from(p)
            if flags & 0x80 or (flags & 0x0F) >= len(catalog.types):
                raise _Rejected(f"bad sample flags 0x{flags:02x}")
            type_name = catalog.types[flags & 0x0F]
            synced = bool(flags & 0x20)
            valid = bool(flags & 0x10)
            self._sample(signal, source, type_name, valid, synced, seq, t_us, bits)
            if synced:
                self.t_us = t_us
        elif rtype == REC_HEALTH:
            need(1 + 8 * HEALTH_PAGES)
            fields = []
            for page in range(HEALTH_PAGES):
                fields.extend(self._health_fields(page, p, 1 + 8 * page))
            self._health(p[0], fields, self.t_us)
        elif rtype == REC_CAN_FRAME:
            need(22)
            self._can_frame(p)
        elif rtype == REC_LOSS:
            need(_LOSS.size)
            records, nbytes, t_us = _LOSS.unpack_from(p)
            self._check_time(t_us)
            self.t_us = t_us
            self.dropped_records += records
            self.dropped_bytes += nbytes
            node = self.logger or "logger"
            for name, value in (("dropped_records", records), ("dropped_bytes", nbytes)):
                self._append(self._channel(ChannelKey(node, "loss", name), "count"), t_us, value)
        elif rtype == REC_TEXT:
            self._text(None, "text", self.t_us, bytes(p).decode("latin-1"))
        elif rtype == REC_END:
            need(_END.size)
            t_us, written = _END.unpack_from(p)
            self.t_us = t_us
            self.records_written = written
        elif rtype == REC_KEY:
            need(_KEY.size, at_least=True)
            key_id, vtype, unit = _KEY.unpack_from(p)
            name = bytes(p[_KEY.size :]).decode("ascii", "replace")
            type_name = catalog.types[vtype] if vtype < len(catalog.types) else f"type{vtype}"
            self.keys[key_id] = (
                name,
                type_name,
                catalog.units[unit] if unit < len(catalog.units) else "",
            )
        elif rtype == REC_VALUE:
            need(_VALUE.size)
            t_us, key_id, status, bits = _VALUE.unpack_from(p)
            self._check_time(t_us)
            self.t_us = t_us
            name, type_name, unit = self.keys.get(key_id, (f"key{key_id}", "f32", ""))
            is_bool = type_name == "bool"
            channel = self._channel(
                ChannelKey(self.logger or "logger", "output", name),
                "bool" if is_bool else "f32",
                unit,
            )
            if status:
                label = OUTPUT_STATUS[status] if status < len(OUTPUT_STATUS) else f"status{status}"
                self._append(channel, t_us, _NAN, label, available=False)
            else:
                value = (1.0 if bits else 0.0) if is_bool else _bits_to_f32(bits)
                self._append(channel, t_us, value)
        elif rtype == REC_TICK:
            need(_TICK.size)
            self.ticks += 1
        else:
            self.unknown[rtype] += 1

    def _can_frame(self, p: memoryview) -> None:
        catalog = self.catalog
        assert catalog is not None
        t_us, can_id, flags, dlc = _CAN.unpack_from(p)
        if dlc > 8:
            raise _Rejected(f"bad CAN dlc {dlc}")
        self._check_time(t_us)
        data = bytes(p[14 : 14 + dlc])
        extended = bool(flags & CAN_FLAG_EXTENDED)
        sender = 0 if extended else catalog.can_sender(can_id)
        self.t_us = t_us
        ids = catalog.ids
        if not extended and can_id <= 0x7FF:
            node = can_id - ids["heartbeat_base"]
            if catalog.is_can_node(node) and dlc == 2:
                self.heartbeat[node] = data[1]
                name = catalog.node(sender)
                state = self._channel(ChannelKey(name, "heartbeat", "state"), "status")
                flags_channel = self._channel(
                    ChannelKey(name, "heartbeat", "err_flags"), "status", "bitmask"
                )
                names = [k for k, bit in catalog.err_flags.items() if data[1] & bit]
                self._append(state, t_us, float(data[0]))
                self._append(flags_channel, t_us, float(data[1]), "|".join(names) or "ok")
                return
            node = can_id - ids["event_base"]
            if catalog.is_can_node(node) and dlc == 8:
                event_flags = data[4]
                signal = catalog.by_name["FAULT" if event_flags & 1 else "EVENT"]
                bits = _U32.unpack_from(data)[0]
                t_event = _unwrap24(data[5] | data[6] << 8 | data[7] << 16, t_us)
                synced = bool(event_flags & 2)
                seq = (event_flags >> 2) & 0x3F
                self._sample(signal, node, "status", True, synced, seq, t_event, bits)
                self._forget_sync(node, bits)
                return
            frame = catalog.frames.get(can_id)
            if frame is not None and dlc == frame.dlc:
                self._data_frame(frame, t_us, data)
                return
            if can_id == ids["time_sync"] and dlc == 8:
                self.time_sync += 1
                return
            if can_id == ids["time_follow_up"] and dlc == 8:
                self.follow_up += 1
                return
            if can_id == ids["command"] and dlc == 8:
                name = catalog.commands.get(data[0], f"code{data[0]}")
                self._text(sender, "command", t_us, f"{name} {data[1:8].hex()}")
                return
            if can_id in (ids["update_ctrl"], ids["update_data"]) and dlc == 8:
                self.update += 1
                return
            if catalog.is_can_node(can_id - ids["update_status_base"]) and dlc == 8:
                self.update += 1
                return
            if can_id >= ids["health_base"] + 16 and dlc == 8:
                node, page = (can_id - ids["health_base"]) >> 4, can_id & 0xF
                if catalog.is_can_node(node) and page < HEALTH_PAGES:
                    try:
                        fields = self._health_fields(page, data)
                    except _Rejected:
                        pass  # kept below as a raw frame, like the reference decoder
                    else:
                        self._health(node, fields, t_us)
                        return
        ident = f"0x{can_id:08X}" if extended else f"0x{can_id:03X}"
        self._text(sender, "can_raw", t_us, f"{ident} {data.hex()}")

    def _frame_plan(self, frame: FrameSpec, synced: bool) -> list[tuple[Any, ...]] | None:
        """Per frame and clock: each field's decoder and target channel, built once.

        Returns None when a field needs the general path (an event signal).
        """

        cache_key = (frame.can_id, synced)
        if cache_key in self._plans:
            return self._plans[cache_key]
        catalog = self.catalog
        assert catalog is not None
        node = catalog.node(frame.sender)
        clock = SHARED_CLOCK if synced else NODE_CLOCK
        plan: list[tuple[Any, ...]] | None = []
        for spec in frame.fields:
            if spec.signal in catalog.event_ids:
                plan = None
                break
            info = catalog.signals.get(spec.signal)
            type_name = str(info["type"]) if info else "raw"
            name = str(info["name"]) if info else f"signal_0x{spec.signal:04x}"
            unit = str(info["unit"]) if info else ""
            channel = self._channel(
                ChannelKey(node, "sample", name, clock),
                type_name,
                unit,
                node_id=frame.sender,
                signal_id=spec.signal,
            )
            plan.append(
                (spec.unpack, spec.offset, spec.unavailable_base, spec.scale32, type_name, channel)
            )
        self._plans[cache_key] = plan
        return plan

    def _data_frame(self, frame: FrameSpec, t_us: int, data: bytes) -> None:
        catalog = self.catalog
        assert catalog is not None
        period = frame.period_us
        if frame.counter_offset is None:
            k = t_us // period
        else:
            counter = data[frame.counter_offset] | data[frame.counter_offset + 1] << 8
            k = _grid_index(counter, t_us, period)
        synced = self._synced(frame.sender)
        t_sample = k * period
        self._check_time(t_sample)
        seq = k & 0xFFFFFFFF
        plan = self._frame_plan(frame, synced)
        if plan is None:
            self._data_frame_general(frame, synced, seq, t_sample, data)
            return
        decoded = []
        for unpack, offset, base, scale32, type_name, channel in plan:
            raw = unpack(data, offset)[0]
            if base <= raw < base + 8:
                decoded.append((channel, _NAN, False, catalog.reason(raw - base)))
            elif type_name == "f32":
                decoded.append((channel, _f32(raw * scale32), True, "ok"))
            elif type_name == "bool":
                decoded.append((channel, 1.0 if raw else 0.0, True, "ok"))
            elif type_name == "i32":
                decoded.append(
                    (channel, float(_I32.unpack(_U32.pack(raw & 0xFFFFFFFF))[0]), True, "ok")
                )
            else:
                decoded.append((channel, float(raw & 0xFFFFFFFF), True, "ok"))
        order = self.order
        for channel, value, valid, status in decoded:
            channel.order.append(order)
            channel.times.append(t_sample)
            channel.values.append(value)
            channel.available.append(valid)
            channel.status.append(
                0 if valid else channel.statuses.setdefault(status, len(channel.statuses))
            )
            channel.seq.append(seq)
            order += 1
        self.order = order

    def _data_frame_general(
        self, frame: FrameSpec, synced: bool, seq: int, t_sample: int, data: bytes
    ) -> None:
        catalog = self.catalog
        assert catalog is not None
        decoded = []
        for spec in frame.fields:
            raw = spec.unpack(data, spec.offset)[0]
            info = catalog.signals.get(spec.signal)
            type_name = str(info["type"]) if info else "raw"
            if spec.unavailable_base <= raw < spec.unavailable_base + 8:
                decoded.append((spec.signal, type_name, False, raw - spec.unavailable_base))
            elif type_name == "f32":
                decoded.append((spec.signal, type_name, True, _f32_bits(raw * spec.scale32)))
            elif type_name == "bool":
                decoded.append((spec.signal, type_name, True, 1 if raw else 0))
            else:
                decoded.append((spec.signal, type_name, True, raw & 0xFFFFFFFF))
        for signal, type_name, valid, bits in decoded:
            self._sample(signal, frame.sender, type_name, valid, synced, seq, t_sample, bits)


def read_lbrlog_stream(
    stream: BinaryIO,
    *,
    total_bytes: int = 0,
    catalogs: Sequence[Catalog] | None = None,
    name: str = "",
    cancellation: CancellationToken | None = None,
    progress: Progress | None = None,
    chunk_size: int = READ_CHUNK,
) -> LbrLog:
    """Decode an ``.lbrlog`` byte stream; see the module docstring for the rules.

    Cancellation and progress are checked before every chunk read
    (``chunk_size`` bytes, 1 MiB by default).
    """

    decoder = _Decoder(catalogs if catalogs is not None else (Catalog.bundled(),))

    def checkpoint(bytes_read: int) -> None:
        """Runs before every chunk read: the only place the reader waits on I/O."""

        if cancellation is not None:
            cancellation.raise_if_cancelled()
        if progress is not None:
            progress(bytes_read, max(total_bytes, bytes_read))

    scanner = RecordScanner(stream, chunk_size, checkpoint)
    for offset, rtype, payload in scanner:
        decoder.record(offset, rtype, payload)
    if cancellation is not None:
        cancellation.raise_if_cancelled()
    if decoder.records == 0:
        if scanner.bytes_read == 0:
            raise LbrlogError("The file is empty. Choose a session log (.lbrlog).")
        raise LbrlogError(
            "No valid .lbrlog record was found. The file is not a firmware session log, "
            "or it is damaged beyond recovery."
        )
    if progress is not None:
        progress(scanner.bytes_read, max(total_bytes, scanner.bytes_read))
    catalog = decoder.catalog
    assert catalog is not None
    integrity = LogIntegrity(
        file_size=scanner.bytes_read,
        sha256=scanner.sha256,
        records=decoder.records,
        record_bytes=scanner.record_bytes,
        corrupt_regions=scanner.corrupt_regions,
        skipped_bytes=scanner.skipped_bytes,
        regions=tuple(scanner.regions),
        truncated_tail=scanner.truncated_tail,
        truncated_offset=scanner.truncated_offset,
        rejected=decoder.rejected,
        rejected_records=tuple(decoder.rejected_records),
        unknown_records=dict(decoder.unknown),
        time_sync_frames=decoder.time_sync,
        time_follow_up_frames=decoder.follow_up,
        update_frames=decoder.update,
        tick_records=decoder.ticks,
        catalog_checked=decoder.catalog_checked,
        catalog_origin=catalog.origin,
        records_written=decoder.records_written,
        dropped_records=decoder.dropped_records,
        dropped_bytes=decoder.dropped_bytes,
    )
    channels = tuple(channel.freeze() for channel in decoder.channels.values())
    return LbrLog(name, decoder.session, channels, integrity)


def read_lbrlog(
    path: Path,
    *,
    catalogs: Sequence[Catalog] | None = None,
    cancellation: CancellationToken | None = None,
    progress: Progress | None = None,
) -> LbrLog:
    """Decode the log at ``path``, opened read-only and closed before returning."""

    if catalogs is None:
        catalogs = candidate_catalogs(path)
    try:
        with path.open("rb") as stream:
            size = path.stat().st_size
            return read_lbrlog_stream(
                stream,
                total_bytes=size,
                catalogs=catalogs,
                name=str(path),
                cancellation=cancellation,
                progress=progress,
            )
    except OSError as error:
        raise LbrlogError(f"Cannot read {path.name}: {error.strerror or error}") from None
