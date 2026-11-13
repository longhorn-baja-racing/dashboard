"""Manual time-offset alignment between two independent sessions.

One session is the *reference* and the other is the *compared* session. The
comparison timeline is the reference session's own timeline, and the compared
session is shifted onto it by a single integer offset::

    comparison_ns = original_ns                (reference)
    comparison_ns = original_ns + offset_ns    (compared)

Original timestamps are never modified: every query returns the original time
next to the mapped comparison time. All times are integer nanoseconds.

Sessions are plain data (:class:`SessionData` holding :class:`SignalTrace`
values). This module does not load, store or own sessions; callers build these
values from whatever source they have.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from itertools import pairwise
from typing import Any

FORMAT_NAME = "lbr-session-alignment"
FORMAT_VERSION = 1


class Side(Enum):
    """Which session of an alignment a time or sample belongs to."""

    REFERENCE = "reference"
    COMPARED = "compared"


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_id(name: str, value: object) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string, got {type(value).__name__}")
    if not value or value != value.strip():
        raise ValueError(f"{name} must be non-empty without surrounding whitespace, got {value!r}")


@dataclass(frozen=True, slots=True)
class SessionIdentity:
    """Stable identity of a session, kept in saved alignments.

    ``session_id`` is compared exactly. ``label`` is a display name only.
    """

    session_id: str
    label: str = ""

    def __post_init__(self) -> None:
        _check_id("session_id", self.session_id)
        if not isinstance(self.label, str):
            raise ValueError(f"label must be a string, got {type(self.label).__name__}")


@dataclass(frozen=True, slots=True)
class SignalTrace:
    """One signal of a session: parallel timestamps and values plus metadata.

    ``times_ns`` are integer nanoseconds in the session's own time base and
    must be non-decreasing. ``values`` are returned unchanged by queries
    (``None`` or NaN stay as they are). ``unit`` and ``frame`` are free text
    and are only compared, never converted.
    """

    signal_id: str
    times_ns: Sequence[int]
    values: Sequence[float | None]
    unit: str | None = None
    frame: str | None = None

    def __post_init__(self) -> None:
        _check_id("signal_id", self.signal_id)
        if len(self.times_ns) != len(self.values):
            raise ValueError(
                f"signal {self.signal_id!r} has {len(self.times_ns)} timestamps but "
                f"{len(self.values)} values; they must be the same length"
            )
        for index, t_ns in enumerate(self.times_ns):
            if not _is_int(t_ns):
                raise ValueError(
                    f"signal {self.signal_id!r} timestamp at index {index} must be an "
                    f"integer number of nanoseconds, got {t_ns!r}"
                )
        for index, (earlier, later) in enumerate(pairwise(self.times_ns)):
            if later < earlier:
                raise ValueError(
                    f"signal {self.signal_id!r} timestamps must be non-decreasing; "
                    f"index {index + 1} ({later}) is before index {index} ({earlier})"
                )

    @property
    def span_ns(self) -> tuple[int, int] | None:
        """First and last original timestamp, or ``None`` with no samples."""

        if not self.times_ns:
            return None
        return self.times_ns[0], self.times_ns[-1]


@dataclass(frozen=True, slots=True)
class SessionData:
    """A session identity and its signals, for alignment queries only.

    ``signals`` is stored as a tuple; signal ids must be unique.
    """

    identity: SessionIdentity
    signals: Sequence[SignalTrace]
    _by_id: dict[str, SignalTrace] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        signals = tuple(self.signals)
        by_id: dict[str, SignalTrace] = {}
        for signal in signals:
            if signal.signal_id in by_id:
                raise ValueError(
                    f"session {self.identity.session_id!r} has more than one signal "
                    f"{signal.signal_id!r}"
                )
            by_id[signal.signal_id] = signal
        object.__setattr__(self, "signals", signals)
        object.__setattr__(self, "_by_id", by_id)

    def signal(self, signal_id: str) -> SignalTrace | None:
        """Return the signal with this exact id, or ``None``."""

        return self._by_id.get(signal_id)


@dataclass(frozen=True, slots=True)
class SignalMatch:
    """A reference signal paired with a compared signal.

    The ids may differ, so signals from unrelated source types can be matched.
    """

    reference_signal_id: str
    compared_signal_id: str

    def __post_init__(self) -> None:
        _check_id("reference_signal_id", self.reference_signal_id)
        _check_id("compared_signal_id", self.compared_signal_id)

    def signal_id(self, side: Side) -> str:
        if side is Side.REFERENCE:
            return self.reference_signal_id
        return self.compared_signal_id


@dataclass(frozen=True, slots=True)
class SessionAlignment:
    """Two session identities, one offset and the matched signals.

    ``offset_ns`` is added to compared-session times to place them on the
    reference timeline. A positive offset moves the compared session later.
    """

    reference: SessionIdentity
    compared: SessionIdentity
    offset_ns: int = 0
    matches: tuple[SignalMatch, ...] = ()

    def __post_init__(self) -> None:
        if not _is_int(self.offset_ns):
            raise ValueError(f"offset_ns must be an integer, got {self.offset_ns!r}")
        if self.reference.session_id == self.compared.session_id:
            raise ValueError(
                f"reference and compared must be different sessions, both are "
                f"{self.reference.session_id!r}"
            )
        matches = tuple(self.matches)
        if len(set(matches)) != len(matches):
            raise ValueError("matches must not contain the same signal pair twice")
        object.__setattr__(self, "matches", matches)

    def identity(self, side: Side) -> SessionIdentity:
        return self.reference if side is Side.REFERENCE else self.compared

    def offset_for(self, side: Side) -> int:
        """Nanoseconds added to ``side`` times to get comparison times."""

        return 0 if side is Side.REFERENCE else self.offset_ns

    def to_comparison_ns(self, side: Side, original_ns: int) -> int:
        """Map an original timestamp of ``side`` to comparison time."""

        return original_ns + self.offset_for(side)

    def to_original_ns(self, side: Side, comparison_ns: int) -> int:
        """Map a comparison time to the original timestamp of ``side``."""

        return comparison_ns - self.offset_for(side)

    def with_offset(self, offset_ns: int) -> SessionAlignment:
        """Return a copy with a new offset and the same identities and matches."""

        return replace(self, offset_ns=offset_ns)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable document for :func:`alignment_from_dict`."""

        return {
            "format": FORMAT_NAME,
            "version": FORMAT_VERSION,
            "reference": {
                "session_id": self.reference.session_id,
                "label": self.reference.label,
            },
            "compared": {
                "session_id": self.compared.session_id,
                "label": self.compared.label,
            },
            "offset_ns": self.offset_ns,
            "matches": [
                {"reference": match.reference_signal_id, "compared": match.compared_signal_id}
                for match in self.matches
            ],
        }


def _check_session(alignment: SessionAlignment, side: Side, session: SessionData) -> None:
    expected = alignment.identity(side).session_id
    if session.identity.session_id != expected:
        raise ValueError(
            f"{side.value} session is {expected!r} in the alignment but "
            f"{session.identity.session_id!r} was given"
        )


# Signal checks ---------------------------------------------------------------


class IssueKind(Enum):
    MISSING_SIGNAL = "missing_signal"
    UNIT_MISMATCH = "unit_mismatch"
    FRAME_MISMATCH = "frame_mismatch"


@dataclass(frozen=True, slots=True)
class MatchIssue:
    """One problem with a matched signal pair, with a readable message.

    ``side`` is set for ``MISSING_SIGNAL`` and ``None`` for mismatches.
    """

    kind: IssueKind
    match: SignalMatch
    message: str
    side: Side | None = None


def _describe(value: str | None) -> str:
    return "(none)" if value is None else repr(value)


def check_matches(
    alignment: SessionAlignment, reference: SessionData, compared: SessionData
) -> tuple[MatchIssue, ...]:
    """Report missing signals and unit or frame mismatches for every match.

    Units and frames are compared as exact strings. A pair is reported when
    at least one side states a unit (or frame) and the two differ, so a stated
    unit against an unknown one is a mismatch. Two unknown values are not.
    An empty result means every matched pair is present and consistent.
    """

    _check_session(alignment, Side.REFERENCE, reference)
    _check_session(alignment, Side.COMPARED, compared)
    issues: list[MatchIssue] = []
    for match in alignment.matches:
        traces: dict[Side, SignalTrace] = {}
        for side, session in ((Side.REFERENCE, reference), (Side.COMPARED, compared)):
            signal_id = match.signal_id(side)
            trace = session.signal(signal_id)
            if trace is None:
                issues.append(
                    MatchIssue(
                        IssueKind.MISSING_SIGNAL,
                        match,
                        f"signal {signal_id!r} is not in {side.value} session "
                        f"{session.identity.session_id!r}",
                        side,
                    )
                )
            else:
                traces[side] = trace
        if len(traces) != 2:
            continue
        ref, cmp = traces[Side.REFERENCE], traces[Side.COMPARED]
        pair = f"{ref.signal_id!r} and {cmp.signal_id!r}"
        if ref.unit != cmp.unit:
            issues.append(
                MatchIssue(
                    IssueKind.UNIT_MISMATCH,
                    match,
                    f"units differ for {pair}: reference {_describe(ref.unit)}, "
                    f"compared {_describe(cmp.unit)}",
                )
            )
        if ref.frame != cmp.frame:
            issues.append(
                MatchIssue(
                    IssueKind.FRAME_MISMATCH,
                    match,
                    f"frames differ for {pair}: reference {_describe(ref.frame)}, "
                    f"compared {_describe(cmp.frame)}",
                )
            )
    return tuple(issues)


# Overlap ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Overlap:
    """Closed overlap ``[start_ns, end_ns]`` in comparison time, or why there is none.

    ``reference_span`` and ``compared_span`` are each side's first and last
    sample in comparison time (``None`` when that side has no samples).
    ``reason`` is empty when the sessions overlap.
    """

    start_ns: int | None
    end_ns: int | None
    reference_span: tuple[int, int] | None
    compared_span: tuple[int, int] | None
    reason: str = ""

    @property
    def has_overlap(self) -> bool:
        return self.start_ns is not None

    @property
    def duration_ns(self) -> int:
        if self.start_ns is None or self.end_ns is None:
            return 0
        return self.end_ns - self.start_ns


def _span(traces: Iterable[SignalTrace], offset_ns: int) -> tuple[int, int] | None:
    spans = [span for span in (trace.span_ns for trace in traces) if span is not None]
    if not spans:
        return None
    return min(s[0] for s in spans) + offset_ns, max(s[1] for s in spans) + offset_ns


def _selected_traces(
    alignment: SessionAlignment,
    side: Side,
    session: SessionData,
    match: SignalMatch | None,
) -> list[SignalTrace]:
    if match is not None:
        matches: Sequence[SignalMatch] = (match,)
    elif alignment.matches:
        matches = alignment.matches
    else:
        return list(session.signals)
    traces = (session.signal(m.signal_id(side)) for m in matches)
    return [trace for trace in traces if trace is not None]


def overlap(
    alignment: SessionAlignment,
    reference: SessionData,
    compared: SessionData,
    match: SignalMatch | None = None,
) -> Overlap:
    """Return the comparison-time interval covered by both sessions.

    The span of a side runs from its earliest to its latest sample over the
    selected signals: ``match`` alone when given, otherwise every matched
    signal, otherwise every signal of the session. Missing signals are
    skipped (see :func:`check_matches`). Touching spans overlap at one instant.
    """

    _check_session(alignment, Side.REFERENCE, reference)
    _check_session(alignment, Side.COMPARED, compared)
    ref_span = _span(_selected_traces(alignment, Side.REFERENCE, reference, match), 0)
    cmp_span = _span(
        _selected_traces(alignment, Side.COMPARED, compared, match), alignment.offset_ns
    )
    ref_name = alignment.reference.session_id
    cmp_name = alignment.compared.session_id
    if ref_span is None or cmp_span is None:
        empty = [
            f"{side} session {name!r}"
            for side, name, span in (
                ("reference", ref_name, ref_span),
                ("compared", cmp_name, cmp_span),
            )
            if span is None
        ]
        verb = "has" if len(empty) == 1 else "have"
        reason = f"{' and '.join(empty)} {verb} no samples for the selected signals"
        return Overlap(None, None, ref_span, cmp_span, reason)
    start = max(ref_span[0], cmp_span[0])
    end = min(ref_span[1], cmp_span[1])
    if start <= end:
        return Overlap(start, end, ref_span, cmp_span)
    if cmp_span[1] < ref_span[0]:
        gap = ref_span[0] - cmp_span[1]
        reason = (
            f"compared session {cmp_name!r} ends at comparison time {cmp_span[1]} ns, "
            f"{gap} ns before reference session {ref_name!r} starts at {ref_span[0]} ns; "
            f"increase offset_ns by at least {gap} to overlap"
        )
    else:
        gap = cmp_span[0] - ref_span[1]
        reason = (
            f"compared session {cmp_name!r} starts at comparison time {cmp_span[0]} ns, "
            f"{gap} ns after reference session {ref_name!r} ends at {ref_span[1]} ns; "
            f"decrease offset_ns by at least {gap} to overlap"
        )
    return Overlap(None, None, ref_span, cmp_span, reason)


# Window query ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AlignedSamples:
    """Samples of one signal inside a comparison-time window.

    ``comparison_times_ns[i]`` is ``original_times_ns[i]`` plus the side's
    offset. Values, unit and frame are copied unchanged.
    """

    session: SessionIdentity
    side: Side
    signal_id: str
    unit: str | None
    frame: str | None
    comparison_times_ns: tuple[int, ...]
    original_times_ns: tuple[int, ...]
    values: tuple[float | None, ...]

    def __len__(self) -> int:
        return len(self.original_times_ns)


@dataclass(frozen=True, slots=True)
class AlignedWindow:
    """Both sides of one matched pair inside ``[start_ns, end_ns]`` comparison time."""

    start_ns: int
    end_ns: int
    reference: AlignedSamples
    compared: AlignedSamples


def samples_in_window(
    alignment: SessionAlignment,
    side: Side,
    session: SessionData,
    signal_id: str,
    start_ns: int,
    end_ns: int,
) -> AlignedSamples:
    """Return ``side`` samples of ``signal_id`` with ``start_ns <= t <= end_ns``.

    ``start_ns`` and ``end_ns`` are comparison times (closed interval). Raises
    ``ValueError`` for an unknown signal, a session that is not the one named
    in the alignment, non-integer bounds, or ``start_ns > end_ns``.
    """

    _check_session(alignment, side, session)
    if not _is_int(start_ns) or not _is_int(end_ns):
        raise ValueError(f"window bounds must be integers, got {start_ns!r} and {end_ns!r}")
    if start_ns > end_ns:
        raise ValueError(f"start_ns ({start_ns}) is after end_ns ({end_ns})")
    trace = session.signal(signal_id)
    if trace is None:
        raise ValueError(
            f"signal {signal_id!r} is not in {side.value} session {session.identity.session_id!r}"
        )
    offset = alignment.offset_for(side)
    first = bisect_left(trace.times_ns, start_ns - offset)
    stop = bisect_right(trace.times_ns, end_ns - offset)
    original = tuple(trace.times_ns[first:stop])
    return AlignedSamples(
        session=session.identity,
        side=side,
        signal_id=signal_id,
        unit=trace.unit,
        frame=trace.frame,
        comparison_times_ns=tuple(t_ns + offset for t_ns in original),
        original_times_ns=original,
        values=tuple(trace.values[first:stop]),
    )


def window(
    alignment: SessionAlignment,
    reference: SessionData,
    compared: SessionData,
    match: SignalMatch,
    start_ns: int,
    end_ns: int,
) -> AlignedWindow:
    """Return both sides of ``match`` inside the comparison-time window.

    Each side keeps its own sample times; nothing is resampled or interpolated.
    """

    return AlignedWindow(
        start_ns=start_ns,
        end_ns=end_ns,
        reference=samples_in_window(
            alignment, Side.REFERENCE, reference, match.reference_signal_id, start_ns, end_ns
        ),
        compared=samples_in_window(
            alignment, Side.COMPARED, compared, match.compared_signal_id, start_ns, end_ns
        ),
    )


# Save and restore ------------------------------------------------------------


def _require(document: Mapping[str, Any], key: str, where: str) -> Any:
    if key not in document:
        raise ValueError(f"{where} is missing required field {key!r}")
    return document[key]


def _require_object(value: object, where: str, keys: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{where} must be an object, got {type(value).__name__}")
    unknown = sorted(str(key) for key in value if key not in keys)
    if unknown:
        raise ValueError(f"{where} has unknown field(s): {', '.join(unknown)}")
    for key in sorted(keys):
        _require(value, key, where)
    return value


def _require_str(value: object, where: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{where} must be a string, got {type(value).__name__}")
    return value


def _identity_from(value: object, where: str) -> SessionIdentity:
    document = _require_object(value, where, {"session_id", "label"})
    session_id = _require_str(document["session_id"], f"{where}.session_id")
    label = _require_str(document["label"], f"{where}.label")
    try:
        return SessionIdentity(session_id, label)
    except ValueError as error:
        raise ValueError(f"{where}: {error}") from None


_TOP_LEVEL_KEYS = {"format", "version", "reference", "compared", "offset_ns", "matches"}


def alignment_from_dict(document: object) -> SessionAlignment:
    """Rebuild a :class:`SessionAlignment` from :meth:`SessionAlignment.to_dict` output.

    Validation is strict: an unknown format or version, a missing or unknown
    field, or a value of the wrong type raises ``ValueError`` naming the field.
    """

    if not isinstance(document, Mapping):
        raise ValueError(f"alignment must be an object, got {type(document).__name__}")
    format_name = _require(document, "format", "alignment")
    if format_name != FORMAT_NAME:
        raise ValueError(f"alignment format must be {FORMAT_NAME!r}, got {format_name!r}")
    version = _require(document, "version", "alignment")
    if not _is_int(version):
        raise ValueError(f"alignment version must be an integer, got {version!r}")
    if version != FORMAT_VERSION:
        raise ValueError(
            f"unsupported alignment version {version}; this build reads version {FORMAT_VERSION}"
        )
    _require_object(document, "alignment", _TOP_LEVEL_KEYS)
    reference = _identity_from(document["reference"], "reference")
    compared = _identity_from(document["compared"], "compared")
    offset_ns = document["offset_ns"]
    if not _is_int(offset_ns):
        raise ValueError(f"offset_ns must be an integer, got {offset_ns!r}")
    raw_matches = document["matches"]
    if not isinstance(raw_matches, list):
        raise ValueError(f"matches must be a list, got {type(raw_matches).__name__}")
    matches: list[SignalMatch] = []
    for index, item in enumerate(raw_matches):
        where = f"matches[{index}]"
        entry = _require_object(item, where, {"reference", "compared"})
        reference_id = _require_str(entry["reference"], f"{where}.reference")
        compared_id = _require_str(entry["compared"], f"{where}.compared")
        try:
            matches.append(SignalMatch(reference_id, compared_id))
        except ValueError as error:
            raise ValueError(f"{where}: {error}") from None
    return SessionAlignment(reference, compared, offset_ns, tuple(matches))
