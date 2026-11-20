"""Replay a firmware ``.lbrlog`` session log as a generic telemetry source.

:mod:`.lbrlog` decodes the file (off the GUI thread, with progress and
cancellation). This module maps its channels onto the generic signal model:

- one :class:`SignalDescriptor` per channel, with a stable ID that matches the
  lbr-csv view (``driver_input.brake_pedal``, ``central.health.uptime_ms``,
  ``stress.stress_3.node-clock``), the catalog unit and description, the node
  as device and the frame rate as nominal rate;
- one :class:`SignalSample` per reading at the time the log gives for that
  measurement, in integer nanoseconds (microseconds x 1000, no float);
- unavailable readings stay unavailable with their reason as quality;
- samples on the Central's clock use the ``lbr_central_us`` timestamp domain,
  samples taken before a node was synchronized ``lbr_node_clock.<node>``, so
  the two are never compared.

Anything the reader skipped, rejected or counted is reported once as a
recoverable source error at the start of the replay, so it shows in the source
status, and in full in the schema metadata.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from typing import Any

import numpy as np

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
from ..core.lbr_session import NODE_CLOCK
from .lbrlog import TYPE_GROUPS, LbrLog, LbrlogError, LogChannel, Progress, read_lbrlog

SHARED_DOMAIN = "lbr_central_us"
NODE_DOMAIN_PREFIX = "lbr_node_clock."
#: The replay keeps one object per reading in memory; larger logs are refused.
MAX_REPLAY_SAMPLES = 500_000
#: Check for cancellation once per this many generated readings.
_CANCEL_EVERY = 256

_INVALID = re.compile(r"[^a-z0-9]+")


def _segment(text: str) -> str:
    """Lowercase ID segment: runs of other characters become one underscore."""

    segment = _INVALID.sub("_", text.lower()).strip("_")
    if not segment:
        return "x"
    return segment if segment[0].isalpha() else f"x{segment}"


def signal_identifier(channel: LogChannel) -> str:
    """Stable signal ID, the same as the lbr-csv view's channel ID where it has one."""

    key = channel.key
    parts = [_segment(key.node or "logger")]
    if key.kind != "sample":
        parts.append(_segment(key.kind))
    if key.name:
        # Output keys are ``Group/Name``: keep the grouping as ID segments.
        parts.extend(_segment(part) for part in key.name.split("/") if part)
    if key.clock == NODE_CLOCK:
        parts.append("node-clock")
    return ".".join(parts)


def timestamp_domain(channel: LogChannel) -> str:
    if channel.key.clock == NODE_CLOCK:
        return NODE_DOMAIN_PREFIX + _segment(channel.key.node)
    return SHARED_DOMAIN


def _display_name(channel: LogChannel) -> str:
    key = channel.key
    name = key.name or key.kind
    if key.kind in ("heartbeat", "loss"):
        name = f"{key.kind} {name}"
    return f"{name} (node clock)" if key.clock == NODE_CLOCK else name


def _descriptor(channel: LogChannel, identifier: str, source_id: str) -> SignalDescriptor:
    key = channel.key
    path = (_segment(key.node or "logger"),)
    if key.kind != "sample":
        path += (_segment(key.kind),)
    display: dict[str, Any] = {
        "lbr_node": key.node,
        "lbr_kind": key.kind,
        "lbr_name": key.name,
        "lbr_clock": key.clock,
        "lbr_value_type": channel.value_type,
    }
    if channel.node_id is not None:
        display["lbr_node_id"] = channel.node_id
    if channel.signal_id is not None:
        display["lbr_signal_id"] = channel.signal_id
    return SignalDescriptor(
        identifier,
        _display_name(channel),
        path,
        TYPE_GROUPS[channel.value_type],
        source_id,
        canonical_unit=channel.unit or None,
        device_id=key.node or None,
        nominal_rate_hz=channel.rate_hz,
        description=channel.description,
        display=display,
        timestamp_domain=timestamp_domain(channel),
    )


def _sample_maker(channel: LogChannel, source_id: str):
    """Build the function that turns sample ``i`` of ``channel`` into a SignalSample."""

    domain = timestamp_domain(channel)
    unit = channel.unit or None
    group = TYPE_GROUPS[channel.value_type]
    times = channel.times_us
    values = channel.values
    available = channel.available
    status = channel.status
    statuses = channel.statuses
    texts = channel.texts
    integer = channel.value_type not in ("f32", "bool")

    def make(index: int) -> SignalSample:
        quality = statuses[int(status[index])]
        t_ns = int(times[index]) * 1000
        extra: dict[str, Any] = {}
        value: Any = None
        ok = bool(available[index])
        if ok:
            number = float(values[index])
            if group == "string":
                value = texts[index]
            elif group == "event":
                value = {"code": texts[index], "argument": int(number)}
            elif group == "boolean":
                value = number != 0.0
            elif integer:
                value = int(number)
            elif isfinite(number):
                value = number
            else:
                # A real NaN or infinite reading: the sample model holds finite
                # numbers only, so keep it as unavailable and say what it was.
                ok, quality = False, "non_finite"
                extra["reading"] = repr(number)
        return SignalSample(
            t_ns,
            value,
            available=ok,
            quality=quality,
            timestamp_domain=domain,
            source_id=source_id,
            unit=unit,
            extra=extra,
        )

    return make


def recovery_error(log: LbrLog) -> SourceError | None:
    """One recoverable error describing everything skipped or lost, or None."""

    problems = log.integrity.problems()
    if not problems:
        return None
    return SourceError(
        "lbrlog_recovered" if log.integrity.damaged else "lbrlog_incomplete",
        "Log read with problems: " + "; ".join(problems) + ".",
        recoverable=True,
        context=log.integrity.to_dict(),
    )


@dataclass(frozen=True)
class PreparedLbrlog:
    """A decoded log and its generic schema, ready to replay any number of times."""

    log: LbrLog
    schema: SignalSchema
    source_id: str
    identifiers: tuple[str, ...]

    def records(self) -> Iterator[tuple[str, SignalSample]]:
        """Every reading as ``(signal ID, sample)`` in log order."""

        channels = self.log.channels
        makers = [_sample_maker(c, self.source_id) for c in channels]
        if not channels:
            return
        order = np.concatenate([c.order for c in channels])
        owner = np.concatenate(
            [np.full(c.count, i, dtype=np.int32) for i, c in enumerate(channels)]
        )
        index = np.concatenate([np.arange(c.count, dtype=np.int64) for c in channels])
        sort = np.argsort(order, kind="stable")
        for channel_index, sample_index in zip(owner[sort].tolist(), index[sort].tolist()):
            yield self.identifiers[channel_index], makers[channel_index](sample_index)

    def source(self) -> IteratorSource:
        source_id = self.source_id
        error = recovery_error(self.log)

        def generate(token: CancellationToken) -> Iterator[SourceEvent]:
            if error is not None:
                yield SourceEvent(SourceEventKind.ERROR, source_id, error=error)
            for count, (identifier, sample) in enumerate(self.records()):
                if count % _CANCEL_EVERY == 0:
                    token.raise_if_cancelled()
                yield SourceEvent(
                    SourceEventKind.RECORD, source_id, signal_id=identifier, sample=sample
                )

        return IteratorSource(source_id, self.schema, generate)


def prepare_log(log: LbrLog, source_id: str) -> PreparedLbrlog:
    """Map a decoded log onto generic descriptors (IDs made unique if they collide)."""

    descriptors = []
    identifiers = []
    used: set[str] = set()
    for channel in log.channels:
        identifier = base = signal_identifier(channel)
        suffix = 2
        while identifier in used:
            identifier = f"{base}-{suffix}"
            suffix += 1
        used.add(identifier)
        identifiers.append(identifier)
        descriptors.append(_descriptor(channel, identifier, source_id))
    metadata: dict[str, Any] = {"format": "lbrlog", "integrity": log.integrity.to_dict()}
    if log.session is not None:
        session = log.session
        metadata["session"] = {
            "format_version": session.format_version,
            "protocol_version": session.protocol_version,
            "fw_version": session.fw_version,
            "catalog_hash": session.catalog_hash,
            "config_crc": session.config_crc,
            "node_id": session.node_id,
            "logger": session.logger,
            "reset_reason": session.reset_reason,
            "boot_count": session.boot_count,
            "start_time_us": session.start_time_us,
            "config": [list(pair) for pair in session.config],
        }
    schema = SignalSchema(1, tuple(descriptors), metadata=metadata)
    return PreparedLbrlog(log, schema, source_id, tuple(identifiers))


def prepare_lbrlog(
    path: Path,
    token: CancellationToken,
    progress: Progress | None = None,
    *,
    max_samples: int = MAX_REPLAY_SAMPLES,
) -> PreparedLbrlog:
    """Read and map a log off the GUI thread; the file is opened read-only."""

    token.raise_if_cancelled()
    log = read_lbrlog(path, cancellation=token, progress=progress)
    if log.sample_count > max_samples:
        raise LbrlogError(
            f"{path.name} holds {log.sample_count:,} readings; the replay source keeps at "
            f"most {max_samples:,} in memory. Export it with the firmware's lbr_decode.py "
            "csv and open the CSV with File > Open."
        )
    token.raise_if_cancelled()
    return prepare_log(log, str(path.resolve()))
