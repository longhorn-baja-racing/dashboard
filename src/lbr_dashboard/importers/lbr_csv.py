"""Reader for firmware lbr-csv exports (version 1).

The format is defined by the firmware's ``docs/protocol.md`` ("CSV export")
and pinned by its ``dev/tests/fixtures/session.csv``: a long table with one row
per sample, event, heartbeat/health field, loss record, text line and so on.

Reading is a single streaming pass with compact per-channel buffers, so large
exports do not create one Python object per sample. Malformed rows are counted
and skipped (with the first few reported) instead of aborting the import; a
file that is not lbr-csv, or is a version this reader does not know, is refused.
"""

from __future__ import annotations

import csv
import io
from array import array
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ..core.lbr_session import (
    NODE_CLOCK,
    SHARED_CLOCK,
    Channel,
    ChannelKey,
    LbrSession,
    MalformedRow,
    SessionEvent,
    reset_reason_name,
)

CSV_COLUMNS = ("t_s", "kind", "node", "name", "value", "unit", "status", "synced", "seq")
SUPPORTED_VERSION = 1
TESTED_PROTOCOL_VERSION = 4
TESTED_LOG_FORMAT_VERSION = 1

_CRASH_RESETS = frozenset({"PANIC", "WATCHDOG", "BROWNOUT"})

MAX_REPORTED_MALFORMED = 20
#: Diagnostic rows (raw CAN frames, commands, unknown records, text) can be
#: numerous; keep the first ones for display and count the rest.
MAX_EVENTS_PER_KIND = 5000
MAX_EVENT_TEXT = 500
PROGRESS_EVERY_BYTES = 1 << 18
READ_CHUNK = 1 << 16

_BOM = chr(0xFEFF)
_NAN = float("nan")
_KIND_ORDER = ("sample", "heartbeat", "health")
_EVENT_KINDS = frozenset({"event", "fault"})
_DIAGNOSTIC_KINDS = frozenset({"text", "command", "can_raw", "unknown_record"})

#: ``progress(bytes_read, total_bytes)``; return False to cancel.
Progress = Callable[[int, int], bool]


class LbrCsvError(ValueError):
    """The file is not a readable lbr-csv export."""


class LbrCsvCancelled(Exception):
    """The progress callback asked to stop."""


def is_lbr_csv(path: Path) -> bool:
    """Cheap check: does the file start with the lbr-csv header row?"""

    try:
        with path.open("rb") as handle:
            first = handle.readline(4096)
    except OSError:
        return False
    text = first.decode("utf-8", errors="replace").lstrip(_BOM)
    try:
        header = next(csv.reader([text]))
    except (csv.Error, StopIteration):
        return False
    return tuple(header) == CSV_COLUMNS


def parse_time_us(text: str) -> int:
    """Exact integer microseconds from ``seconds.micros`` (6 decimals)."""

    whole, dot, micros = text.partition(".")
    if dot and len(micros) == 6 and text.isascii() and whole.isdigit() and micros.isdigit():
        return int(whole) * 1_000_000 + int(micros)
    raise ValueError(f"time '{text[:40]}' is not seconds with 6 decimals")


@dataclass
class _ChannelBuffer:
    unit: str
    times: array[int] = field(default_factory=lambda: array("q"))
    values: array[float] = field(default_factory=lambda: array("d"))
    available: array[int] = field(default_factory=lambda: array("b"))
    status: array[int] = field(default_factory=lambda: array("H"))
    seq: array[int] = field(default_factory=lambda: array("q"))
    statuses: dict[str, int] = field(default_factory=lambda: {"ok": 0})

    def status_index(self, label: str) -> int:
        if label not in self.statuses and len(self.statuses) >= 0xFFFF:
            label = "other"  # uint16 status table is full; keep counting, lose detail
        index = self.statuses.get(label)
        if index is None:
            index = self.statuses[label] = len(self.statuses)
        return index

    def build(self, key: ChannelKey) -> Channel:
        times = np.frombuffer(self.times, dtype=np.int64).copy()
        values = np.frombuffer(self.values, dtype=np.float64).copy()
        available = np.frombuffer(self.available, dtype=np.int8).astype(bool)
        status = np.frombuffer(self.status, dtype=np.uint16).copy()
        seq = np.frombuffer(self.seq, dtype=np.int64).copy()
        if times.size > 1 and bool(np.any(np.diff(times) < 0)):
            order = np.argsort(times, kind="stable")
            times, values, available = times[order], values[order], available[order]
            status, seq = status[order], seq[order]
        return Channel(
            key=key,
            unit=self.unit,
            times_us=times,
            values=values,
            available=available,
            status=status,
            statuses=tuple(self.statuses),
            seq=seq,
        )


class _CountingReader(io.RawIOBase):
    """Raw stream that reports progress by bytes read and can cancel the import."""

    def __init__(self, raw: io.RawIOBase, total: int, progress: Progress | None) -> None:
        super().__init__()
        self._raw = raw
        self._total = total
        self._progress = progress
        self._next = PROGRESS_EVERY_BYTES
        self.done = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        count = self._raw.readinto(buffer) or 0
        self.done += count
        if self._progress is not None and self.done >= self._next:
            self._next = self.done + PROGRESS_EVERY_BYTES
            self._report()
        return count

    def _report(self) -> None:
        if self._progress is not None and not self._progress(self.done, self._total):
            raise LbrCsvCancelled

    def finish(self) -> None:
        self._report()


def _text(counter: _CountingReader) -> io.TextIOWrapper:
    buffered = io.BufferedReader(counter, buffer_size=READ_CHUNK)
    # The format is ASCII; undecodable bytes become U+FFFD and fail that row only.
    return io.TextIOWrapper(buffered, encoding="utf-8-sig", errors="replace", newline="")


def _int_or_none(text: str) -> int | None:
    return int(text) if text else None


class _Reader:
    def __init__(self) -> None:
        self.logger = ""
        self.session: dict[str, str] = {}
        self.config: list[tuple[str, str]] = []
        self.integrity: dict[str, int] = {}
        self.end: int | None = None
        self.buffers: dict[ChannelKey, _ChannelBuffer] = {}
        self._lookup: dict[tuple[str, str, str, bool], tuple[ChannelKey, _ChannelBuffer]] = {}
        self.events: list[SessionEvent] = []
        self.event_counts: defaultdict[str, int] = defaultdict(int)
        self.malformed: list[MalformedRow] = []
        self.malformed_count = 0
        self.unknown_kinds: defaultdict[str, int] = defaultdict(int)
        self.loss_records = 0
        self.loss_bytes = 0
        self.rows = 1
        self.unit_conflicts: set[str] = set()

    def bad(self, line: int, reason: str) -> None:
        self.malformed_count += 1
        if len(self.malformed) < MAX_REPORTED_MALFORMED:
            self.malformed.append(MalformedRow(line, reason))

    def add_event(self, event: SessionEvent) -> None:
        self.event_counts[event.kind] += 1
        if self.event_counts[event.kind] <= MAX_EVENTS_PER_KIND:
            self.events.append(event)

    def row(self, fields: list[str]) -> None:
        t_s, kind, node, name, value, unit, status, synced, seq = fields
        t_us = parse_time_us(t_s)
        if kind in _KIND_ORDER:
            self.sample(kind, node, name, value, unit, status, synced, seq, t_us)
        elif kind in _EVENT_KINDS:
            self.add_event(
                SessionEvent(t_us, kind, node, name, value, synced != "0", _int_or_none(seq))
            )
        elif kind == "loss":
            self.loss(t_us, node, name, value)
        elif kind in _DIAGNOSTIC_KINDS:
            text = value if len(value) <= MAX_EVENT_TEXT else value[: MAX_EVENT_TEXT - 3] + "..."
            self.add_event(SessionEvent(t_us, kind, node, name, text))
        elif kind == "session":
            self.logger = self.logger or node
            self.session.setdefault(name, value)
        elif kind == "config":
            self.config.append((name, value))
        elif kind == "integrity":
            self.integrity[name] = int(value)
        elif kind == "end":
            self.end = int(value)
        elif kind == "format":
            raise ValueError("repeated format row")
        else:
            # Newer exporters may add kinds; keep reading and say so.
            self.unknown_kinds[kind or "(empty)"] += 1

    def sample(
        self,
        kind: str,
        node: str,
        name: str,
        value: str,
        unit: str,
        status: str,
        synced: str,
        seq: str,
        t_us: int,
    ) -> None:
        missing = value == ""
        parsed = _NAN if missing else float(value)
        sequence = int(seq) if seq else -1
        node_clock = synced == "0" and kind == "sample"
        cached = self._lookup.get((kind, node, name, node_clock))
        if cached is None:
            if not name:
                raise ValueError("sample without a name")
            key = ChannelKey(kind, node, name, NODE_CLOCK if node_clock else SHARED_CLOCK)
            cached = self._lookup[(kind, node, name, node_clock)] = (key, _ChannelBuffer(unit))
            self.buffers[key] = cached[1]
        key, buffer = cached
        if unit != buffer.unit and unit:
            if buffer.unit:
                self.unit_conflicts.add(key.id)
            else:
                buffer.unit = unit
        label = status or ("unavailable" if missing else "ok")
        index = buffer.statuses.get(label)
        if index is None:
            index = buffer.status_index(label)
        buffer.times.append(t_us)
        buffer.values.append(parsed)
        buffer.available.append(0 if missing else 1)
        buffer.status.append(index)
        buffer.seq.append(sequence)

    def loss(self, t_us: int, node: str, name: str, value: str) -> None:
        amount = int(value)
        if name == "dropped_records":
            self.loss_records += amount
            self.add_event(SessionEvent(t_us, "loss", node, name, f"{amount} records"))
            return
        if name != "dropped_bytes":
            raise ValueError(f"unknown loss field '{name}'")
        self.loss_bytes += amount
        last = self.events[-1] if self.events else None
        if last is not None and last.kind == "loss" and last.t_us == t_us and last.node == node:
            merged = f"{last.value}, {amount} bytes"
            self.events[-1] = SessionEvent(t_us, "loss", node, last.name, merged)
        else:
            self.add_event(SessionEvent(t_us, "loss", node, name, f"{amount} bytes"))

    def warnings(self, channels: list[Channel]) -> list[str]:
        notes: list[str] = []
        protocol = self.session.get("protocol_version")
        if protocol and protocol != str(TESTED_PROTOCOL_VERSION):
            notes.append(
                f"Logged with protocol version {protocol}; this dashboard was checked "
                f"against version {TESTED_PROTOCOL_VERSION}."
            )
        log_format = self.session.get("format_version")
        if log_format and log_format != str(TESTED_LOG_FORMAT_VERSION):
            notes.append(f"Log format version {log_format} (expected {TESTED_LOG_FORMAT_VERSION}).")
        if not self.session:
            notes.append("No session record: versions and configuration are unknown.")
        reset = reset_reason_name(self.session.get("reset_reason", ""))
        if reset in _CRASH_RESETS:
            notes.append(
                f"The logger booted after a {reset} reset: the previous session may have "
                "ended abnormally."
            )
        if not self.integrity:
            notes.append("No integrity rows at the end: the export may be incomplete.")
        corrupt = self.integrity.get("corrupt_regions", 0)
        skipped = self.integrity.get("skipped_bytes", 0)
        if corrupt:
            notes.append(f"The log had {corrupt} corrupt region(s) ({skipped} bytes skipped).")
        elif skipped:
            notes.append(f"{skipped} bytes of the log were skipped.")
        tail = self.integrity.get("truncated_tail", 0)
        if tail:
            notes.append(
                f"The log ended with a truncated record ({tail} bytes), for example after "
                "power loss."
            )
        if self.integrity.get("catalog_checked", 1) == 0:
            notes.append("Signal names were not checked against the firmware catalog.")
        if self.end is None:
            notes.append("No END record: the log was not closed cleanly.")
        if self.loss_records or self.loss_bytes:
            notes.append(
                f"The logger dropped {self.loss_records} record(s) ({self.loss_bytes} bytes) "
                "when its queue overflowed; see the loss markers."
            )
        node_clock = [c for c in channels if c.key.clock == NODE_CLOCK]
        if node_clock:
            nodes = ", ".join(sorted({c.key.node for c in node_clock}))
            count = sum(c.count for c in node_clock)
            notes.append(
                f"{count} sample(s) from {nodes} were taken before time sync and are on the "
                "node's own clock; they are listed separately and not aligned with other data."
            )
        if self.unit_conflicts:
            notes.append(f"Unit changed within: {', '.join(sorted(self.unit_conflicts))}.")
        for kind, count in sorted(self.unknown_kinds.items()):
            notes.append(f"Ignored {count} row(s) of unknown kind '{kind}'.")
        if self.malformed:
            first = self.malformed[0]
            notes.append(
                f"Skipped {self.malformed_count} malformed row(s); first at line "
                f"{first.line}: {first.reason}."
            )
        for kind, count in sorted(self.event_counts.items()):
            if count > MAX_EVENTS_PER_KIND:
                notes.append(f"Showing the first {MAX_EVENTS_PER_KIND} of {count} '{kind}' rows.")
        return notes


def _channel_order(channel: Channel) -> tuple[str, int, str, bool]:
    key = channel.key
    return (key.node, _KIND_ORDER.index(key.kind), key.name, key.clock != SHARED_CLOCK)


class LbrCsvImporter:
    """Read an lbr-csv export into an :class:`LbrSession`."""

    def import_session(self, path: Path, progress: Progress | None = None) -> LbrSession:
        try:
            total = path.stat().st_size
            with path.open("rb", buffering=0) as handle:
                return self._read(handle, path.name, total, progress)
        except OSError as error:
            raise LbrCsvError(f"Could not read {path.name}: {error.strerror or error}") from None

    def _read(
        self, handle: io.RawIOBase, name: str, total: int, progress: Progress | None
    ) -> LbrSession:
        counter = _CountingReader(handle, total, progress)
        reader = csv.reader(_text(counter))
        try:
            header = next(reader)
        except StopIteration:
            raise LbrCsvError(f"{name} is empty.") from None
        if tuple(header) != CSV_COLUMNS:
            raise LbrCsvError(
                f"{name} is not an lbr-csv export: the header must be {','.join(CSV_COLUMNS)}."
            )
        first = next(reader, None)
        if first is None:
            raise LbrCsvError(f"{name} has a header but no rows.")
        if len(first) != len(CSV_COLUMNS) or first[1] != "format" or first[3] != "lbr_csv_version":
            raise LbrCsvError(f"{name} does not start with the lbr_csv_version row.")
        if first[4] != str(SUPPORTED_VERSION):
            raise LbrCsvError(
                f"{name} is lbr-csv version {first[4] or '?'}; this dashboard reads version "
                f"{SUPPORTED_VERSION}. Update the dashboard or re-export with a matching decoder."
            )
        state = _Reader()
        while True:
            try:
                fields = next(reader)
            except StopIteration:
                break
            except csv.Error as error:
                state.bad(reader.line_num, str(error))
                continue
            if not fields:
                continue
            state.rows += 1
            if len(fields) != len(CSV_COLUMNS):
                state.bad(reader.line_num, f"{len(fields)} columns instead of {len(CSV_COLUMNS)}")
                continue
            try:
                state.row(fields)
            except ValueError as error:
                state.bad(reader.line_num, str(error))

        counter.finish()
        channels = sorted(
            (buffer.build(key) for key, buffer in state.buffers.items()), key=_channel_order
        )
        dropped = {
            kind: count - MAX_EVENTS_PER_KIND
            for kind, count in state.event_counts.items()
            if count > MAX_EVENTS_PER_KIND
        }
        return LbrSession(
            source_name=name,
            csv_version=SUPPORTED_VERSION,
            logger=state.logger,
            session=state.session,
            config=tuple(state.config),
            integrity=state.integrity,
            channels=tuple(channels),
            events=tuple(sorted(state.events, key=lambda event: event.t_us)),
            warnings=tuple(state.warnings(channels)),
            row_count=state.rows,
            malformed=tuple(state.malformed),
            malformed_count=state.malformed_count,
            dropped_events=dropped,
        )
