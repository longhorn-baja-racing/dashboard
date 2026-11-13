"""State segments and sample events for one boolean or category signal.

Inputs are plain parallel sequences: ``times_ns`` (integer nanoseconds,
non-decreasing) and ``values`` (``bool``, ``int`` or ``str`` categories, with
``None`` meaning a missing sample). See docs/state-timeline.md for the rules.

Display policy: a sample's state is shown from its own timestamp until the
next sample, or until ``max_hold_ns`` expires, whichever comes first. After
that the timeline shows "no data" until the next sample. No state is ever
carried through a period nothing was measured in beyond that limit.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from math import isfinite, isnan
from typing import Any

DEFAULT_MAX_SEGMENTS = 1000
DEFAULT_MAX_EVENTS = 2000
NO_DATA_LABEL = "no data"
HOLD_POLICY = (
    "A sample's state is shown from its timestamp until the next sample or until "
    "max_hold_ns expires, whichever is first; after that the timeline shows no data."
)


class SegmentKind(Enum):
    """What a segment or event shows."""

    STATE = "state"  # a known category value
    UNKNOWN = "unknown"  # a bool/int/str value missing from the explicit labels
    UNSUPPORTED = "unsupported"  # a value that is not a category (for example 2.5)
    NO_DATA = "no data"  # missing sample, expired hold or recording without samples
    MIXED = "mixed"  # several short segments or events combined to bound the output


@dataclass(frozen=True, slots=True)
class StateSegment:
    """One span of the timeline, ``[start_ns, end_ns)``.

    ``value`` is the category value (raw value for unknown or unsupported
    values, ``None`` for no data and mixed segments). ``transitions`` counts the
    state changes hidden inside a mixed segment and is 0 otherwise.
    ``no_data_ns`` is how much of the segment is no data.
    """

    start_ns: int
    end_ns: int
    kind: SegmentKind
    value: object
    label: str
    transitions: int = 0
    no_data_ns: int = 0

    @property
    def duration_ns(self) -> int:
        return self.end_ns - self.start_ns


@dataclass(frozen=True, slots=True)
class StateEvent:
    """One input sample at its exact timestamp, or several combined.

    ``index`` is the position of the (first) sample in the input. ``transition``
    is true when the sample's state differs from the state shown just before
    it (for combined events: when any of them does). ``count`` is the number of
    samples combined; ``last_time_ns`` is the time of the last of them.
    """

    time_ns: int
    index: int
    kind: SegmentKind
    value: object
    label: str
    transition: bool
    count: int = 1
    last_time_ns: int | None = None


@dataclass(frozen=True, slots=True)
class StateTimeline:
    """Segments and events inside the requested window, in time order.

    ``sample_count`` is the number of input samples inside the closed window
    and ``transition_count`` the number of state boundaries between segments
    before any merging. Both stay exact when the output was bounded:
    ``transition_count`` equals the sum of ``segment.transitions`` plus the
    boundaries between the returned segments.
    """

    segments: tuple[StateSegment, ...]
    events: tuple[StateEvent, ...]
    sample_count: int
    transition_count: int
    start_ns: int | None
    end_ns: int | None
    max_hold_ns: int | None
    segments_bounded: bool
    events_bounded: bool
    hold_policy: str = HOLD_POLICY

    @property
    def is_empty(self) -> bool:
        return not self.segments and not self.events


_Key = tuple[SegmentKind, str, object]
_NO_DATA_KEY: _Key = (SegmentKind.NO_DATA, "", None)


def build_state_timeline(
    times_ns: Sequence[int],
    values: Sequence[object],
    *,
    labels: Mapping[Any, str] | None = None,
    max_hold_ns: int | None = None,
    data_start_ns: int | None = None,
    data_end_ns: int | None = None,
    window_start_ns: int | None = None,
    window_end_ns: int | None = None,
    max_segments: int = DEFAULT_MAX_SEGMENTS,
    max_events: int = DEFAULT_MAX_EVENTS,
) -> StateTimeline:
    """Build the state segments and sample events of one discrete signal.

    ``labels`` maps category values to display labels; when given, any other
    bool/int/str value is shown as unknown with its raw value kept. Without it
    every bool/int/str value is its own category. Integral floats (``1.0``) are
    read as ints, NaN as missing, other values as unsupported.

    ``data_start_ns``/``data_end_ns`` are the span the recording is known to
    cover; time in it without a sample is shown as no data. The last sample
    holds until ``data_end_ns`` (bounded by ``max_hold_ns``), or until hold
    expiry when no end is given; with neither it has no duration.

    Samples with the same timestamp keep their input order: the last one sets
    the state, but every one of them is returned as an event.

    Only samples needed for ``[window_start_ns, window_end_ns]`` are visited
    (the window ends are found with bisect). At most ``max_segments`` segments
    and ``max_events`` events are returned; extra short ones are combined into
    mixed entries that keep their transition and sample counts.
    """

    if len(times_ns) != len(values):
        raise ValueError("times_ns and values must have the same length")
    if max_hold_ns is not None and (not _is_int(max_hold_ns) or max_hold_ns <= 0):
        raise ValueError("max_hold_ns must be a positive integer")
    if not _is_int(max_segments) or max_segments < 2:
        raise ValueError("max_segments must be an integer of at least 2")
    if not _is_int(max_events) or max_events < 1:
        raise ValueError("max_events must be a positive integer")
    _check_range(data_start_ns, data_end_ns, "data_start_ns", "data_end_ns")
    _check_range(window_start_ns, window_end_ns, "window_start_ns", "window_end_ns")
    lookup = _label_lookup(labels)

    count = len(times_ns)
    event_lo = 0 if window_start_ns is None else bisect_left(times_ns, window_start_ns)
    event_hi = count if window_end_ns is None else bisect_right(times_ns, window_end_ns)
    walk_lo = event_lo
    if window_start_ns is not None:
        walk_lo = min(event_lo, max(bisect_right(times_ns, window_start_ns) - 1, 0))
    walk_hi = max(event_hi, min(walk_lo + 1, count))

    pieces: list[list] = []  # [start_ns, end_ns, key, value, label, no_data_ns]
    if data_start_ns is not None and walk_lo == 0:
        first_ns = times_ns[0] if count else data_end_ns
        if first_ns is not None and data_start_ns < first_ns:
            _append(pieces, data_start_ns, first_ns, _NO_DATA_KEY, None, NO_DATA_LABEL)

    previous_key = (
        _NO_DATA_KEY
        if walk_lo == 0
        else _key_after(times_ns, values, walk_lo - 1, lookup, max_hold_ns)
    )
    raw_events: list[tuple[int, int, _Key, object, str, bool]] = []
    for index in range(walk_lo, walk_hi):
        time_ns = times_ns[index]
        key, value, label = _classify(values[index], lookup)
        boundary = _boundary(times_ns, index, data_end_ns)
        if event_lo <= index < event_hi:
            raw_events.append((time_ns, index, key, value, label, key != previous_key))

        if key is _NO_DATA_KEY:
            end_ns = time_ns if boundary is None else boundary
            _append(pieces, time_ns, end_ns, key, None, label)
            previous_key = key
            continue
        hold_end = None if max_hold_ns is None else time_ns + max_hold_ns
        if boundary is None:
            _append(pieces, time_ns, time_ns if hold_end is None else hold_end, key, value, label)
            previous_key = key
        elif hold_end is not None and hold_end < boundary:
            _append(pieces, time_ns, hold_end, key, value, label)
            _append(pieces, hold_end, boundary, _NO_DATA_KEY, None, NO_DATA_LABEL)
            previous_key = _NO_DATA_KEY
        else:
            _append(pieces, time_ns, boundary, key, value, label)
            previous_key = key

    segments = _clip(pieces, window_start_ns, window_end_ns)
    transition_count = sum(
        1 for left, right in zip(segments, segments[1:]) if left.end_ns == right.start_ns
    )
    bounded_segments = _bound_segments(segments, max_segments)
    events = _bound_events(raw_events, max_events)
    return StateTimeline(
        segments=tuple(bounded_segments),
        events=tuple(events),
        sample_count=len(raw_events),
        transition_count=transition_count,
        start_ns=window_start_ns,
        end_ns=window_end_ns,
        max_hold_ns=max_hold_ns,
        segments_bounded=len(bounded_segments) != len(segments),
        events_bounded=len(events) != len(raw_events),
    )


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_range(start: int | None, end: int | None, start_name: str, end_name: str) -> None:
    if start is not None and end is not None and start > end:
        raise ValueError(f"{start_name} must not be after {end_name}")


def _category_tag(value: object) -> str | None:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, str):
        return "str"
    return None


def _label_lookup(labels: Mapping[Any, str] | None) -> dict | None:
    if labels is None:
        return None
    lookup: dict[tuple[str, object], str] = {}
    for value, label in labels.items():
        tag = _category_tag(value)
        if tag is None:
            raise ValueError(f"label keys must be bool, int or str, not {type(value).__name__}")
        lookup[(tag, value)] = str(label)
    return lookup


def _default_label(tag: str, value: object) -> str:
    if tag == "bool":
        return "true" if value else "false"
    if tag == "str" and value == "":
        return "(empty string)"
    return str(value)


def _classify(value: object, lookup: dict | None) -> tuple[_Key, object, str]:
    """Return the state key, the normalized value and the label of one sample."""

    if value is None:
        return _NO_DATA_KEY, None, NO_DATA_LABEL
    if isinstance(value, float):
        if isnan(value):
            return _NO_DATA_KEY, None, NO_DATA_LABEL
        if not (isfinite(value) and value.is_integer()):
            return (
                (SegmentKind.UNSUPPORTED, "float", repr(value)),
                value,
                (f"unsupported: float {value!r}"),
            )
        value = int(value)
    tag = _category_tag(value)
    if tag is None:
        type_name = type(value).__name__
        return (
            (SegmentKind.UNSUPPORTED, type_name, repr(value)),
            value,
            (f"unsupported type: {type_name}"),
        )
    if lookup is None:
        return (SegmentKind.STATE, tag, value), value, _default_label(tag, value)
    label = lookup.get((tag, value))
    if label is None:
        return (SegmentKind.UNKNOWN, tag, value), value, f"unknown: {ascii(value)}"
    return (SegmentKind.STATE, tag, value), value, label


def _boundary(times_ns: Sequence[int], index: int, data_end_ns: int | None) -> int | None:
    """Time the sample at ``index`` can hold until at most (next sample or data end)."""

    time_ns = times_ns[index]
    if index + 1 < len(times_ns):
        boundary = times_ns[index + 1]
        if boundary < time_ns:
            raise ValueError("times_ns must be non-decreasing")
        return boundary
    if data_end_ns is None:
        return None
    return max(data_end_ns, time_ns)


def _key_after(
    times_ns: Sequence[int],
    values: Sequence[object],
    index: int,
    lookup: dict | None,
    max_hold_ns: int | None,
) -> _Key:
    """State shown just before the sample after ``index``."""

    key = _classify(values[index], lookup)[0]
    next_ns = times_ns[index + 1]
    if max_hold_ns is not None and times_ns[index] + max_hold_ns < next_ns:
        return _NO_DATA_KEY
    return key


def _append(
    pieces: list[list], start_ns: int, end_ns: int, key: _Key, value: object, label: str
) -> None:
    """Add ``[start_ns, end_ns)``, joining it to the previous piece when the state is the same."""

    if end_ns <= start_ns:
        return  # duplicate timestamp (a later sample wins) or a last sample with no duration
    no_data_ns = end_ns - start_ns if key is _NO_DATA_KEY else 0
    if pieces and pieces[-1][2] == key and pieces[-1][1] == start_ns:
        pieces[-1][1] = end_ns
        pieces[-1][5] += no_data_ns
        return
    pieces.append([start_ns, end_ns, key, value, label, no_data_ns])


def _clip(
    pieces: list[list], window_start_ns: int | None, window_end_ns: int | None
) -> list[StateSegment]:
    segments = []
    for start_ns, end_ns, key, value, label, no_data_ns in pieces:
        if window_start_ns is not None:
            start_ns = max(start_ns, window_start_ns)
        if window_end_ns is not None:
            end_ns = min(end_ns, window_end_ns)
        if end_ns <= start_ns:
            continue
        if no_data_ns:
            no_data_ns = end_ns - start_ns
        segments.append(StateSegment(start_ns, end_ns, key[0], value, label, 0, no_data_ns))
    return segments


def _mixed_label(transitions: int, no_data_ns: int) -> str:
    text = f"{transitions} changes"
    return f"{text}, some no data" if no_data_ns else text


def _bound_segments(segments: list[StateSegment], max_segments: int) -> list[StateSegment]:
    """Combine runs of short segments until at most ``max_segments`` remain.

    A segment at least ``2 * span / (max_segments - 1)`` long is kept as is.
    Shorter neighbours are collected until together they reach that length and
    are then returned as one mixed segment. Each kept segment and each full
    group cover at least that length, and each short group is followed by a
    kept segment or the end, so the result has at most ``max_segments`` entries.
    """

    if len(segments) <= max_segments:
        return segments
    span = segments[-1].end_ns - segments[0].start_ns
    min_ns = -(-2 * span // (max_segments - 1))
    result: list[StateSegment] = []
    group: list[StateSegment] = []
    group_ns = 0

    def flush() -> None:
        if len(group) == 1:
            result.append(group[0])
        elif group:
            no_data_ns = sum(item.no_data_ns for item in group)
            transitions = len(group) - 1
            result.append(
                StateSegment(
                    group[0].start_ns,
                    group[-1].end_ns,
                    SegmentKind.MIXED,
                    None,
                    _mixed_label(transitions, no_data_ns),
                    transitions,
                    no_data_ns,
                )
            )
        group.clear()

    for segment in segments:
        if segment.duration_ns >= min_ns:
            flush()
            group_ns = 0
            result.append(segment)
            continue
        group.append(segment)
        group_ns += segment.duration_ns
        if group_ns >= min_ns:
            flush()
            group_ns = 0
    flush()
    return result


def _bound_events(
    raw_events: list[tuple[int, int, _Key, object, str, bool]], max_events: int
) -> list[StateEvent]:
    """Return one event per sample, or combine samples into ``max_events`` time buckets."""

    if len(raw_events) <= max_events:
        return [
            StateEvent(time_ns, index, key[0], value, label, transition, 1, time_ns)
            for time_ns, index, key, value, label, transition in raw_events
        ]
    first_ns = raw_events[0][0]
    span = raw_events[-1][0] - first_ns + 1
    events: list[StateEvent] = []
    start = 0
    while start < len(raw_events):
        bucket = (raw_events[start][0] - first_ns) * max_events // span
        stop = start + 1
        while (
            stop < len(raw_events)
            and (raw_events[stop][0] - first_ns) * max_events // span == bucket
        ):
            stop += 1
        group = raw_events[start:stop]
        time_ns, index, key, value, label, _ = group[0]
        transition = any(item[5] for item in group)
        if len(group) > 1 and any(item[2] != key for item in group):
            kind, value, label = SegmentKind.MIXED, None, f"{len(group)} samples"
        else:
            kind = key[0]
        events.append(
            StateEvent(time_ns, index, kind, value, label, transition, len(group), group[-1][0])
        )
        start = stop
    return events
