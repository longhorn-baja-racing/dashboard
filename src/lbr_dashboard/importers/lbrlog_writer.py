"""Record received live packets as an ``.lbrlog`` file the replay source can open.

The firmware's log format (``docs/protocol.md``, "Log format", version 1) is a
sequence of self-delimiting, CRC-checked records. The live USB stream carries
the same record types (raw ``CAN_FRAME`` records, ``TEXT``, car logic ``KEY``
and ``VALUE``), so a host recording writes each received record byte for byte
as a log record and the dashboard's ``.lbrlog`` reader replays it with the
same decoding as the live view:

- ``SESSION`` first, with the versions and catalog hash the stream was decoded
  with, the Central as logger, the device time of the first recorded packet as
  start time and ``host_*`` configuration lines that say this is a host capture
  of a live link, not the SD card log (firmware version, reset reason and boot
  count are not on the link and are written as 0);
- the received records in arrival order, unchanged;
- a ``LOSS`` record ahead of the next written record whenever packets were
  lost on the link, frames were rejected, or the recorder's own bounded buffer
  overflowed, followed by a ``TEXT`` record starting with ``host:`` saying which;
- ``END`` on a clean stop, with the number of records written including itself.

Records are appended as they arrive and flushed after every batch (fsync at
most once a second), so an interrupted recording keeps everything up to the
last flush and the reader reports it as not closed cleanly. The destination is
created exclusively and never overwritten.
"""

from __future__ import annotations

import os
import struct
import zlib
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import Condition, Thread, current_thread
from time import monotonic
from typing import BinaryIO

from .lbrlog import (
    LOG_FORMAT_VERSION,
    MAX_PAYLOAD,
    PROTOCOL_VERSION,
    REC_END,
    REC_LOSS,
    REC_SESSION,
    REC_TEXT,
    SYNC0,
    SYNC1,
)

#: Received records held for the writer; more is dropped and recorded as loss.
DEFAULT_BUFFER_BYTES = 8 << 20
FSYNC_INTERVAL_S = 1.0

_SESSION = struct.Struct("<HHIIIBBHIQ")
_LOSS = struct.Struct("<IIQ")
_END = struct.Struct("<QI")
_HEAD = struct.Struct("<BBBBH")
_U32 = struct.Struct("<I")


def encode_record(rtype: int, payload: bytes) -> bytes:
    """One log record: sync, type, flags 0, length, payload, CRC-32 over type..payload."""

    if len(payload) > MAX_PAYLOAD:
        raise ValueError(f"a log record holds at most {MAX_PAYLOAD} bytes")
    head = _HEAD.pack(SYNC0, SYNC1, rtype, 0, len(payload))
    crc = zlib.crc32(payload, zlib.crc32(head[2:]))
    return head + payload + _U32.pack(crc)


def session_payload(
    *,
    catalog_hash: int,
    node_id: int,
    start_time_us: int,
    config: Mapping[str, str],
    fw_version: int = 0,
    reset_reason: int = 0,
    boot_count: int = 0,
) -> bytes:
    lines = []
    for key, value in config.items():
        text = f"{key}={value}"
        if "\n" in text or "\r" in text or not text.isascii():
            raise ValueError("configuration lines must be single-line ASCII")
        lines.append(text)
    config_text = "\n".join(lines).encode("ascii")
    fixed = _SESSION.pack(
        LOG_FORMAT_VERSION,
        PROTOCOL_VERSION,
        fw_version,
        catalog_hash,
        zlib.crc32(config_text),
        node_id,
        reset_reason,
        0,
        boot_count,
        start_time_us,
    )
    return fixed + config_text


class RecordingError(RuntimeError):
    """The destination could not be created or written."""


@dataclass(frozen=True, slots=True)
class RecordingStatus:
    """What a recording has written so far, safe to read from any thread."""

    path: str
    state: str  # "waiting", "recording", "finished", "failed"
    bytes_written: int
    records_written: int
    first_t_us: int | None
    last_t_us: int | None
    elapsed_s: float
    buffered_bytes: int
    dropped_records: int
    dropped_bytes: int
    lost_packets: int
    rejected_bytes: int
    error: str | None

    @property
    def device_span_s(self) -> float:
        if self.first_t_us is None or self.last_t_us is None:
            return 0.0
        return max(0, self.last_t_us - self.first_t_us) / 1e6

    @property
    def has_loss(self) -> bool:
        return bool(self.dropped_records or self.lost_packets or self.rejected_bytes)


@dataclass(frozen=True, slots=True)
class _Item:
    """One queued write: ``SESSION`` plus carried records, or one packet's records.

    ``loss`` is ``(records, bytes, lost packets, rejected bytes)`` to mark with a
    ``LOSS`` record right before this item; ``aux`` is the logger node for
    ``begin`` and the device time before the packet for ``packet``.
    """

    kind: str
    t_us: int
    records: tuple[tuple[int, bytes], ...]
    aux: int
    size: int
    loss: tuple[int, int, int, int] = (0, 0, 0, 0)


def _open_exclusive(path: Path) -> BinaryIO:
    return path.open("xb")


class LiveRecorder:
    """Append received records to an ``.lbrlog`` file on its own writer thread.

    The source worker calls :meth:`begin` once and :meth:`offer` per packet;
    both only queue and never wait for the disk. The queue holds at most
    ``buffer_bytes`` of payload; packets beyond that are dropped, counted and
    recorded as a ``LOSS`` record. A write error stops the recording (the file
    keeps what was flushed) and is reported in :meth:`status`; reception is
    not affected. :meth:`stop` writes ``END`` and closes the file.
    """

    def __init__(
        self,
        path: Path,
        *,
        catalog_hash: int,
        config: Mapping[str, str],
        buffer_bytes: int = DEFAULT_BUFFER_BYTES,
        fsync_interval_s: float = FSYNC_INTERVAL_S,
        opener: Callable[[Path], BinaryIO] = _open_exclusive,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.path = Path(path)
        self._catalog_hash = catalog_hash
        self._config = dict(config)
        self._limit = buffer_bytes
        self._fsync_interval = fsync_interval_s
        self._opener = opener
        self._clock = clock
        self._condition = Condition()
        self._queue: deque[_Item] = deque()
        self._queued_bytes = 0
        self._stream: BinaryIO | None = None
        self._thread: Thread | None = None
        self._started_at = 0.0
        self._stopped_at: float | None = None
        self._stopping = False
        self._begun = False
        self._state = "waiting"
        self._error: str | None = None
        self._bytes = 0
        self._records = 0
        self._first_t: int | None = None
        self._last_t: int | None = None
        # Loss not yet attached to a queued item, and totals.
        self._pending_loss = (0, 0, 0, 0)
        self._dropped_records = self._dropped_bytes = 0
        self._lost_packets = self._rejected_bytes = 0
        self._end_t: int | None = None

    # -- owner thread --------------------------------------------------------

    def start(self) -> None:
        """Create the file (never overwriting one) and start the writer thread."""

        try:
            self._stream = self._opener(self.path)
        except FileExistsError:
            raise RecordingError(f"{self.path} already exists; choose another name.") from None
        except OSError as error:
            raise RecordingError(f"Cannot create {self.path}: {error.strerror or error}") from None
        self._started_at = self._clock()
        self._thread = Thread(target=self._run, name="lbrlog-recorder", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> bool:
        """Write ``END``, flush and close; True once the file is closed."""

        with self._condition:
            self._stopping = True
            self._condition.notify_all()
        thread = self._thread
        if thread is None:
            self._close_stream()
            return True
        if thread is current_thread():
            return False
        thread.join(timeout)
        return not thread.is_alive()

    def status(self) -> RecordingStatus:
        with self._condition:
            return RecordingStatus(
                str(self.path),
                self._state,
                self._bytes,
                self._records,
                self._first_t,
                self._last_t,
                (
                    ((self._stopped_at or self._clock()) - self._started_at)
                    if self._started_at
                    else 0.0
                ),
                self._queued_bytes,
                self._dropped_records,
                self._dropped_bytes,
                self._lost_packets,
                self._rejected_bytes,
                self._error,
            )

    # -- source worker thread ------------------------------------------------

    def begin(self, start_t_us: int, node_id: int, carry: tuple[tuple[int, bytes], ...]) -> None:
        """Queue ``SESSION`` (and the records that set up decoder state) once."""

        with self._condition:
            if self._begun or self._stopping or self._state == "failed":
                return
            self._begun = True
            size = sum(len(payload) for _, payload in carry)
            self._queue.append(_Item("begin", start_t_us, carry, node_id, size))
            self._queued_bytes += size
            self._condition.notify_all()

    def offer(
        self,
        t_us: int,
        records: tuple[tuple[int, bytes], ...],
        *,
        decoder_t_us: int | None = None,
        lost_packets: int = 0,
        rejected_bytes: int = 0,
    ) -> bool:
        """Queue one packet's records; False if they were dropped (counted as loss).

        ``t_us`` is the packet time, ``decoder_t_us`` the latest device time
        before this packet (the time a ``LOSS`` record ahead of it gets).
        ``lost_packets`` and ``rejected_bytes`` are link losses seen since the
        previous packet.
        """

        with self._condition:
            if not self._begun or self._stopping or self._state == "failed":
                return False
            count, nbytes, lost, rejected = self._pending_loss
            lost += lost_packets
            rejected += rejected_bytes
            self._lost_packets += lost_packets
            self._rejected_bytes += rejected_bytes
            size = sum(len(payload) for _, payload in records)
            if self._queued_bytes + size > self._limit:
                self._pending_loss = (count + len(records), nbytes + size, lost, rejected)
                self._dropped_records += len(records)
                self._dropped_bytes += size
                return False
            self._pending_loss = (0, 0, 0, 0)
            before = t_us if decoder_t_us is None else decoder_t_us
            loss = (count, nbytes, lost, rejected)
            self._queue.append(_Item("packet", t_us, records, before, size, loss))
            self._queued_bytes += size
            self._condition.notify_all()
            return True

    def note_time(self, t_us: int) -> None:
        """Latest device time seen, used for ``END`` when no packet follows."""

        with self._condition:
            self._end_t = t_us

    # -- writer thread -------------------------------------------------------

    def _latest_time(self) -> int:
        with self._condition:
            times = [t for t in (self._end_t, self._last_t) if t is not None]
            return max(times) if times else 0

    def _write(self, rtype: int, payload: bytes) -> None:
        stream = self._stream
        assert stream is not None
        record = encode_record(rtype, payload)
        stream.write(record)
        with self._condition:
            self._bytes += len(record)
            self._records += 1

    def _write_loss(self, t_us: int, loss: tuple[int, int, int, int]) -> None:
        """``LOSS`` (lost packets count as at least one record each) and a ``host:`` note."""

        dropped, dropped_bytes, lost, rejected = loss
        self._write(REC_LOSS, _LOSS.pack(dropped + lost, dropped_bytes + rejected, t_us))
        parts = []
        if lost:
            parts.append(f"{lost} packet(s) lost on the live link")
        if rejected:
            parts.append(f"{rejected} byte(s) of undecodable frames rejected")
        if dropped:
            parts.append(
                f"{dropped} record(s) ({dropped_bytes} bytes) dropped because the recorder "
                "fell behind"
            )
        text = "host: " + "; ".join(parts) + "."
        self._write(REC_TEXT, text.encode("ascii")[:MAX_PAYLOAD])

    def _run(self) -> None:
        last_sync = self._clock()
        try:
            while True:
                with self._condition:
                    while not self._queue and not self._stopping:
                        self._condition.wait(0.25)
                    items = list(self._queue)
                    self._queue.clear()
                    self._queued_bytes = 0
                    stopping = self._stopping
                    tail = (0, 0, 0, 0)
                    if stopping and self._begun:
                        tail, self._pending_loss = self._pending_loss, (0, 0, 0, 0)
                for item in items:
                    if item.kind == "begin":
                        self._write(
                            REC_SESSION,
                            session_payload(
                                catalog_hash=self._catalog_hash,
                                node_id=item.aux,
                                start_time_us=item.t_us,
                                config=self._config,
                            ),
                        )
                        with self._condition:
                            self._state = "recording"
                            self._first_t = self._last_t = item.t_us
                    elif any(item.loss):
                        self._write_loss(item.aux, item.loss)
                    for rtype, payload in item.records:
                        self._write(rtype, payload)
                    if item.kind == "packet":
                        with self._condition:
                            self._last_t = item.t_us
                if any(tail):
                    self._write_loss(self._latest_time(), tail)
                stream = self._stream
                assert stream is not None
                stream.flush()
                now = self._clock()
                if stopping or now - last_sync >= self._fsync_interval:
                    _fsync(stream)
                    last_sync = now
                if stopping:
                    break
            if self._begun:
                with self._condition:
                    written = self._records + 1
                self._write(REC_END, _END.pack(self._latest_time(), written))
                stream.flush()
                _fsync(stream)
            with self._condition:
                self._state = "finished"
                self._stopped_at = self._clock()
        except Exception as error:  # A failing disk must never stop reception.
            message = error.strerror if isinstance(error, OSError) and error.strerror else error
            with self._condition:
                self._state = "failed"
                self._stopped_at = self._clock()
                self._error = f"Writing {self.path.name} failed: {message}"
                self._queue.clear()
                self._queued_bytes = 0
        finally:
            self._close_stream()

    def _close_stream(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.close()
            except OSError as error:
                with self._condition:
                    if self._state != "failed":
                        self._state = "failed"
                        self._error = f"Closing {self.path.name} failed: {error.strerror or error}"


def _fsync(stream: BinaryIO) -> None:
    try:
        fileno = stream.fileno()
    except (AttributeError, OSError, ValueError):
        return  # Not a real file (tests); flush already handed the bytes over.
    os.fsync(fileno)
