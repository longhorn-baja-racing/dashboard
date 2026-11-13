"""Qt-free model of a firmware session exported as lbr-csv (version 1).

The firmware specification (``docs/protocol.md`` in the firmware repository,
"CSV export") defines the format. This module only holds decoded data; the
reader lives in :mod:`lbr_dashboard.importers.lbr_csv`.

Times are integer microseconds exactly as exported. Unavailable samples keep
their reason and are never turned into zero. Samples taken before a node was
synchronized to the Central (``synced = 0``) are on that node's own clock and
are kept in separate channels so they are never aligned with shared-clock data.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

import numpy as np
import numpy.typing as npt

#: Clock of samples on the Central's shared time base (epoch: Central boot).
SHARED_CLOCK = "shared"
#: Clock of samples taken before the node was synchronized to the Central.
NODE_CLOCK = "node"

CHANNEL_KINDS = ("sample", "heartbeat", "health")
_SCALED_UNIT = re.compile(r"^(?P<base>.*\S)\s*\*\s*1e(?P<exp>[+-]?\d+)$")
MAX_DISCRETE_LEVELS = 16

#: ``lbr_reset_reason_t`` in the firmware (``health.h``), by code.
RESET_REASONS = (
    "UNKNOWN",
    "POWER_ON",
    "SOFTWARE",
    "PANIC",
    "WATCHDOG",
    "BROWNOUT",
    "DEEP_SLEEP",
    "EXTERNAL",
)


def reset_reason_name(code: str) -> str:
    """Firmware reset reason name for a session ``reset_reason`` value."""

    if code.isdigit() and int(code) < len(RESET_REASONS):
        return RESET_REASONS[int(code)]
    return f"code {code}" if code else ""


def display_unit(unit: str) -> tuple[str, float]:
    """Return the unit to show and the factor to apply, e.g. ``deg*1e-7``.

    Values are stored raw; scaling only happens for display.
    """

    match = _SCALED_UNIT.match(unit)
    if match is None:
        return unit, 1.0
    return match.group("base"), 10.0 ** int(match.group("exp"))


@dataclass(frozen=True, slots=True)
class ChannelKey:
    """Identity of one plottable stream in an export."""

    kind: str
    node: str
    name: str
    clock: str = SHARED_CLOCK

    @property
    def id(self) -> str:
        """Stable lowercase signal ID, e.g. ``driver_input.brake_pedal``."""

        parts = [self.node or "unknown"]
        if self.kind != "sample":
            parts.append(self.kind)
        parts.append(self.name)
        if self.clock == NODE_CLOCK:
            parts.append("node-clock")
        return ".".join(part.lower() for part in parts)

    @property
    def label(self) -> str:
        suffix = " (node clock)" if self.clock == NODE_CLOCK else ""
        return f"{self.name}{suffix}"


@dataclass(frozen=True, eq=False)
class Channel:
    """Time-ordered samples of one channel.

    ``values`` is NaN where a sample is unavailable; use ``available`` (not
    NaN) to tell missing from a real NaN reading. ``status`` indexes
    ``statuses``: ``ok`` or an unavailable reason for samples, the error flag
    names for heartbeat ``err_flags``.
    """

    key: ChannelKey
    unit: str
    times_us: npt.NDArray[np.int64]
    values: npt.NDArray[np.float64]
    available: npt.NDArray[np.bool_]
    status: npt.NDArray[np.uint16]
    statuses: tuple[str, ...]
    seq: npt.NDArray[np.int64]

    def __post_init__(self) -> None:
        for array in (self.times_us, self.values, self.available, self.status, self.seq):
            array.setflags(write=False)

    @property
    def id(self) -> str:
        return self.key.id

    @property
    def count(self) -> int:
        return int(self.times_us.size)

    @property
    def available_count(self) -> int:
        return int(np.count_nonzero(self.available))

    def unavailable_reasons(self) -> dict[str, int]:
        """How many samples were unavailable, by reason, most common first."""

        missing = self.status[~self.available]
        counts = Counter(self.statuses[index] or "unavailable" for index in missing.tolist())
        return dict(counts.most_common())

    def status_labels(self) -> dict[str, int]:
        """Counts of every status label (for example heartbeat error flags)."""

        counts = Counter(self.statuses[index] for index in self.status.tolist())
        return dict(counts.most_common())

    def valid_values(self) -> npt.NDArray[np.float64]:
        values = self.values[self.available]
        return values[np.isfinite(values)]

    @property
    def is_discrete(self) -> bool:
        """Integer-valued with few distinct levels: states, flags, counts."""

        if self.key.kind in ("heartbeat", "health"):
            return True
        values = self.valid_values()
        if values.size == 0:
            return False
        return bool(
            np.all(values == np.round(values)) and np.unique(values).size <= MAX_DISCRETE_LEVELS
        )

    def index_at(self, t_us: int) -> int:
        """Index of the last sample at or before ``t_us``, or -1."""

        return int(np.searchsorted(self.times_us, t_us, side="right")) - 1


@dataclass(frozen=True, slots=True)
class SessionEvent:
    """A timeline entry that is not a channel sample."""

    t_us: int
    kind: str
    node: str
    name: str
    value: str
    synced: bool = True
    seq: int | None = None

    @property
    def label(self) -> str:
        if self.kind == "loss":
            return f"LOSS {self.value}"
        if self.kind in ("event", "fault"):
            arg = f"({self.value})" if self.value else ""
            return f"{self.kind.upper()} {self.name}{arg}"
        if self.kind == "text":
            return self.value if len(self.value) <= 80 else self.value[:77] + "..."
        return f"{self.kind} {self.name} {self.value}".rstrip()


@dataclass(frozen=True, slots=True)
class MalformedRow:
    line: int
    reason: str


@dataclass(frozen=True)
class LbrSession:
    """Everything decoded from one lbr-csv export."""

    source_name: str
    csv_version: int
    logger: str
    session: Mapping[str, str]
    config: tuple[tuple[str, str], ...]
    integrity: Mapping[str, int]
    channels: tuple[Channel, ...]
    events: tuple[SessionEvent, ...]
    warnings: tuple[str, ...]
    row_count: int
    malformed: tuple[MalformedRow, ...] = ()
    malformed_count: int = 0
    dropped_events: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "session", MappingProxyType(dict(self.session)))
        object.__setattr__(self, "integrity", MappingProxyType(dict(self.integrity)))
        object.__setattr__(self, "dropped_events", MappingProxyType(dict(self.dropped_events)))

    def channel(self, channel_id: str) -> Channel | None:
        return next((c for c in self.channels if c.id == channel_id), None)

    def nodes(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(channel.key.node for channel in self.channels))

    def time_range_us(self) -> tuple[int, int] | None:
        """First and last shared-clock time over channels and synced events."""

        starts = [
            int(c.times_us[0]) for c in self.channels if c.count and c.key.clock == SHARED_CLOCK
        ]
        ends = [
            int(c.times_us[-1]) for c in self.channels if c.count and c.key.clock == SHARED_CLOCK
        ]
        timed = [e.t_us for e in self.events if e.synced and e.kind in ("event", "fault", "loss")]
        if not starts and not timed:
            return None
        return min(starts + timed), max(ends + timed)
