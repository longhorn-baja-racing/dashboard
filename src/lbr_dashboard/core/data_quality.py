"""Deterministic data-quality summaries for recorded, timestamped signals.

Inputs are plain parallel sequences: integer nanosecond timestamps and values,
where ``None`` and NaN both mean "missing". Unlike interval queries, these
summaries do not assume sorted timestamps: samples are walked in recorded order
so that timestamp regressions can be reported.

Intervals are closed: a sample at exactly ``start_ns`` or ``end_ns`` is inside.

Every finding is labelled with the evidence that established it (see
``Evidence``). Nothing here claims a root cause, and completeness is only ever
reported from a recorded sequence counter; without one it is unknown.
See docs/data-quality.md for every definition.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from itertools import pairwise
from statistics import median_low
from typing import ClassVar

DEFAULT_GAP_FACTOR = 1.5
"""A positive delta longer than this many reference periods is a gap."""

MAX_EXAMPLES = 10
"""Most regression and sequence-event examples kept per signal (counts are exact)."""


class Evidence(str, Enum):
    """Who established a finding."""

    RECORDED = "recorded"
    """Read directly from the recording: stored values and stored sequence counters."""

    INFERRED = "inferred"
    """Derived by the dashboard from timestamps, possibly against a reference period."""


class Completeness(str, Enum):
    """Whether every record in the interval is known to be present."""

    UNKNOWN = "unknown"
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"


class PeriodSource(str, Enum):
    """Where the reference period used for gap detection came from."""

    EXPECTED = "expected"
    """Supplied by the caller, for example from a documented logging rate."""

    MEDIAN = "median"
    """The median positive delta between samples of this signal."""


class SequenceEventKind(str, Enum):
    MISSING = "missing"
    REPEAT = "repeat"
    ROLLOVER = "rollover"
    RESET = "reset"


@dataclass(frozen=True, slots=True)
class Gap:
    """A stretch of time with no samples, longer than the gap threshold."""

    evidence: ClassVar[Evidence] = Evidence.INFERRED

    start_ns: int
    """Timestamp of the last sample before the gap."""
    end_ns: int
    """Timestamp of the first sample after the gap."""
    missing_periods: int
    """Approximate number of reference periods with no sample."""

    @property
    def duration_ns(self) -> int:
        return self.end_ns - self.start_ns


@dataclass(frozen=True, slots=True)
class Regression:
    """A sample whose timestamp is earlier than the sample recorded before it."""

    evidence: ClassVar[Evidence] = Evidence.INFERRED

    index: int
    """Position of the later sample in the input sequences."""
    previous_ns: int
    time_ns: int


@dataclass(frozen=True, slots=True)
class ValueCounts:
    """Counts of usable and unusable values in the interval."""

    evidence: ClassVar[Evidence] = Evidence.RECORDED

    samples: int
    valid: int
    missing: int
    """Values that are ``None`` or NaN."""
    non_finite: int
    """Values that are positive or negative infinity."""


@dataclass(frozen=True, slots=True)
class Timing:
    """Timestamp-derived findings for the interval."""

    evidence: ClassVar[Evidence] = Evidence.INFERRED

    first_ns: int | None
    """Earliest timestamp in the interval (in time, not recorded order)."""
    last_ns: int | None
    """Latest timestamp in the interval."""
    duplicate_timestamps: int
    """Samples whose timestamp equals that of another sample in the interval."""
    regressions: int
    regression_examples: tuple[Regression, ...]
    min_delta_ns: int | None
    median_delta_ns: int | None
    """Lower median of the positive deltas, so it is always an observed delta."""
    max_delta_ns: int | None
    rate_hz: float | None
    """Rate estimated from ``median_delta_ns``, not a measured or configured rate."""
    reference_period_ns: int | None
    reference_source: PeriodSource | None
    gap_factor: float
    gaps: tuple[Gap, ...]


@dataclass(frozen=True, slots=True)
class SequenceEvent:
    """One step of a recorded sequence counter that was not ``previous + 1``."""

    evidence: ClassVar[Evidence] = Evidence.RECORDED

    kind: SequenceEventKind
    index: int
    """Position of the sample in the input sequences."""
    previous: int
    value: int
    skipped: int
    """Counter values skipped by this step (zero for repeats and resets)."""


@dataclass(frozen=True, slots=True)
class SequenceSummary:
    """Findings from a per-sample sequence counter stored in the recording."""

    evidence: ClassVar[Evidence] = Evidence.RECORDED

    contiguous: bool
    """Whether the counter is defined to increase by exactly one per record."""
    modulus: int | None
    runs: int
    """Stretches of counter values separated by resets."""
    missing_values: int
    """Counter values skipped inside runs, including across rollovers."""
    repeats: int
    rollovers: int
    resets: int
    events: tuple[SequenceEvent, ...]

    @property
    def dropped_records(self) -> int | None:
        """Confirmed dropped records; ``None`` when the counter is not contiguous."""

        return self.missing_values if self.contiguous else None

    @property
    def completeness(self) -> Completeness:
        if not self.contiguous or self.runs == 0:
            return Completeness.UNKNOWN
        if self.missing_values:
            return Completeness.INCOMPLETE
        if self.resets:
            return Completeness.UNKNOWN  # Records lost across a reset cannot be counted.
        return Completeness.COMPLETE


@dataclass(frozen=True, slots=True)
class SignalQuality:
    """Data-quality summary of one signal over one closed interval."""

    name: str
    start_ns: int | None
    end_ns: int | None
    values: ValueCounts
    timing: Timing
    sequence: SequenceSummary | None

    @property
    def completeness(self) -> Completeness:
        """Unknown without a recorded counter; never assumed complete."""

        return Completeness.UNKNOWN if self.sequence is None else self.sequence.completeness


def summarize_signal(
    times_ns: Sequence[int],
    values: Sequence[float | None],
    *,
    name: str = "",
    start_ns: int | None = None,
    end_ns: int | None = None,
    expected_period_ns: int | None = None,
    gap_factor: float = DEFAULT_GAP_FACTOR,
    sequence: Sequence[int] | None = None,
    sequence_modulus: int | None = None,
    sequence_contiguous: bool = False,
) -> SignalQuality:
    """Summarize one signal over the closed interval ``[start_ns, end_ns]``.

    ``None`` for either bound leaves that side open. Timestamps may be in any
    order. ``expected_period_ns`` replaces the median delta as the reference
    period for gaps. ``sequence`` is an optional per-sample counter stored in
    the recording; ``sequence_modulus`` is its rollover modulus and
    ``sequence_contiguous`` states that it increases by exactly one per record,
    which is what turns skipped counter values into confirmed dropped records.
    """

    if len(values) != len(times_ns):
        raise ValueError("Times and values must have the same length")
    if sequence is not None and len(sequence) != len(times_ns):
        raise ValueError("Sequence counter must have one value per sample")
    if sequence is None and (sequence_modulus is not None or sequence_contiguous):
        raise ValueError("Sequence options require a sequence counter")
    if start_ns is not None and end_ns is not None and start_ns > end_ns:
        raise ValueError("Interval start must not be after its end")
    if expected_period_ns is not None and expected_period_ns <= 0:
        raise ValueError("Expected period must be positive")
    if not gap_factor > 1.0:
        raise ValueError("Gap factor must be greater than 1")
    if sequence_modulus is not None:
        if sequence_modulus < 2:
            raise ValueError("Sequence modulus must be at least 2")
        if sequence is not None and any(not 0 <= item < sequence_modulus for item in sequence):
            raise ValueError("Sequence values must be in [0, modulus)")

    selected = [
        index
        for index, time in enumerate(times_ns)
        if (start_ns is None or time >= start_ns) and (end_ns is None or time <= end_ns)
    ]
    sequence_summary = None
    if sequence is not None:
        sequence_summary = _summarize_sequence(
            [(index, sequence[index]) for index in selected],
            sequence_modulus,
            sequence_contiguous,
        )
    return SignalQuality(
        name=name,
        start_ns=start_ns,
        end_ns=end_ns,
        values=_count_values([values[index] for index in selected]),
        timing=_summarize_timing(
            [(index, times_ns[index]) for index in selected], expected_period_ns, gap_factor
        ),
        sequence=sequence_summary,
    )


def summarize_session(
    signals: Mapping[str, tuple[Sequence[int], Sequence[float | None]]],
    *,
    start_ns: int | None = None,
    end_ns: int | None = None,
    expected_periods_ns: Mapping[str, int] | None = None,
    gap_factor: float = DEFAULT_GAP_FACTOR,
) -> tuple[SignalQuality, ...]:
    """Summarize each signal independently, ordered by signal name.

    Each signal gets its own reference period, so signals logged at different
    rates do not affect each other's gaps or rate estimates.
    """

    periods = expected_periods_ns or {}
    unknown = sorted(set(periods) - set(signals))
    if unknown:
        raise ValueError(f"Expected periods given for unknown signals: {', '.join(unknown)}")
    return tuple(
        summarize_signal(
            *signals[name],
            name=name,
            start_ns=start_ns,
            end_ns=end_ns,
            expected_period_ns=periods.get(name),
            gap_factor=gap_factor,
        )
        for name in sorted(signals)
    )


def _count_values(values: Sequence[float | None]) -> ValueCounts:
    missing = non_finite = 0
    for value in values:
        if value is None or math.isnan(value):
            missing += 1
        elif not math.isfinite(value):
            non_finite += 1
    return ValueCounts(
        samples=len(values),
        valid=len(values) - missing - non_finite,
        missing=missing,
        non_finite=non_finite,
    )


def _summarize_timing(
    samples: Sequence[tuple[int, int]], expected_period_ns: int | None, gap_factor: float
) -> Timing:
    regressions = 0
    examples: list[Regression] = []
    for (_, previous), (index, time) in pairwise(samples):
        if time < previous:
            regressions += 1
            if len(examples) < MAX_EXAMPLES:
                examples.append(Regression(index, previous, time))

    # Coverage in time is judged on sorted timestamps: a sample recorded out of
    # order still covers its moment, so a regression is not also a gap.
    ordered = sorted(time for _, time in samples)
    deltas = [later - earlier for earlier, later in pairwise(ordered)]
    positive = [delta for delta in deltas if delta > 0]
    median = median_low(positive) if positive else None

    reference = expected_period_ns if expected_period_ns is not None else median
    source = None
    if expected_period_ns is not None:
        source = PeriodSource.EXPECTED
    elif median is not None:
        source = PeriodSource.MEDIAN
    gaps: tuple[Gap, ...] = ()
    if reference is not None:
        threshold = gap_factor * reference
        gaps = tuple(
            Gap(earlier, later, _missing_periods(later - earlier, reference))
            for earlier, later in pairwise(ordered)
            if later - earlier > threshold
        )
    return Timing(
        first_ns=ordered[0] if ordered else None,
        last_ns=ordered[-1] if ordered else None,
        duplicate_timestamps=len(deltas) - len(positive),
        regressions=regressions,
        regression_examples=tuple(examples),
        min_delta_ns=min(positive) if positive else None,
        median_delta_ns=median,
        max_delta_ns=max(positive) if positive else None,
        rate_hz=1e9 / median if median is not None else None,
        reference_period_ns=reference,
        reference_source=source,
        gap_factor=gap_factor,
        gaps=gaps,
    )


def _missing_periods(duration_ns: int, period_ns: int) -> int:
    """Whole periods that fit in the gap, minus the one ending at its last sample."""

    return max(0, (2 * duration_ns + period_ns) // (2 * period_ns) - 1)


def _summarize_sequence(
    counters: Sequence[tuple[int, int]], modulus: int | None, contiguous: bool
) -> SequenceSummary:
    missing = repeats = rollovers = resets = 0
    events: list[SequenceEvent] = []
    for (_, previous), (index, value) in pairwise(counters):
        if value == previous + 1:
            continue
        if value == previous:
            repeats += 1
            event = SequenceEvent(SequenceEventKind.REPEAT, index, previous, value, 0)
        elif value > previous:
            missing += value - previous - 1
            event = SequenceEvent(
                SequenceEventKind.MISSING, index, previous, value, value - previous - 1
            )
        elif modulus is not None and value + modulus - previous <= modulus // 2:
            skipped = value + modulus - previous - 1
            rollovers += 1
            missing += skipped
            event = SequenceEvent(SequenceEventKind.ROLLOVER, index, previous, value, skipped)
        else:
            resets += 1
            event = SequenceEvent(SequenceEventKind.RESET, index, previous, value, 0)
        if len(events) < MAX_EXAMPLES:
            events.append(event)
    return SequenceSummary(
        contiguous=contiguous,
        modulus=modulus,
        runs=resets + 1 if counters else 0,
        missing_values=missing,
        repeats=repeats,
        rollovers=rollovers,
        resets=resets,
        events=tuple(events),
    )


def format_quality(quality: SignalQuality) -> list[tuple[str, str]]:
    """Return ASCII ``(label, value)`` rows describing one summary."""

    values, timing, sequence = quality.values, quality.timing, quality.sequence
    rec, inf = f"[{Evidence.RECORDED.value}]", f"[{Evidence.INFERRED.value}]"
    rows = [
        ("Signal", quality.name or "-"),
        ("Interval", _format_interval(quality.start_ns, quality.end_ns)),
        (f"Samples {rec}", str(values.samples)),
        (f"Valid values {rec}", str(values.valid)),
        (f"Missing values {rec}", str(values.missing)),
        (f"Non-finite values {rec}", str(values.non_finite)),
        (f"First time {inf}", _seconds(timing.first_ns)),
        (f"Last time {inf}", _seconds(timing.last_ns)),
        (f"Duplicate timestamps {inf}", str(timing.duplicate_timestamps)),
        (f"Timestamp regressions {inf}", str(timing.regressions)),
    ]
    rows += [
        (
            f"  regression at sample {item.index}",
            f"{_seconds(item.previous_ns)} -> {_seconds(item.time_ns)}",
        )
        for item in timing.regression_examples
    ]
    rows += [
        (
            f"Delta min / median / max {inf}",
            " / ".join(
                _millis(item)
                for item in (timing.min_delta_ns, timing.median_delta_ns, timing.max_delta_ns)
            ),
        ),
        (
            f"Rate estimate {inf}",
            "-" if timing.rate_hz is None else f"{timing.rate_hz:.3f} Hz (from median delta)",
        ),
        (f"Gap reference {inf}", _format_reference(timing)),
        (f"Gaps {inf}", str(len(timing.gaps))),
    ]
    rows += [
        (
            f"  gap {number}",
            f"{_seconds(gap.start_ns)} -> {_seconds(gap.end_ns)} "
            f"({_millis(gap.duration_ns)}, ~{gap.missing_periods} missing periods)",
        )
        for number, gap in enumerate(timing.gaps[:MAX_EXAMPLES], start=1)
    ]
    if len(timing.gaps) > MAX_EXAMPLES:
        rows.append(("  ...", f"{len(timing.gaps) - MAX_EXAMPLES} more gaps"))
    if sequence is None:
        rows.append(("Completeness", "unknown (no sequence counter)"))
        return rows
    dropped = sequence.dropped_records
    rows += [
        (f"Sequence counter {rec}", _format_counter(sequence)),
        (f"Counter runs {rec}", str(sequence.runs)),
        (f"Missing counter values {rec}", str(sequence.missing_values)),
        (
            f"Confirmed dropped records {rec}",
            "unknown (counter not defined as contiguous)" if dropped is None else str(dropped),
        ),
        (f"Counter repeats {rec}", str(sequence.repeats)),
        (f"Counter rollovers {rec}", str(sequence.rollovers)),
        (f"Counter resets {rec}", str(sequence.resets)),
    ]
    rows += [
        (f"  {event.kind.value} at sample {event.index}", f"{event.previous} -> {event.value}")
        for event in sequence.events
    ]
    rows.append((f"Completeness {rec}", sequence.completeness.value))
    return rows


def _seconds(time_ns: int | None) -> str:
    return "-" if time_ns is None else f"{time_ns / 1e9:.6f} s"


def _millis(duration_ns: int | None) -> str:
    return "-" if duration_ns is None else f"{duration_ns / 1e6:.3f} ms"


def _format_interval(start_ns: int | None, end_ns: int | None) -> str:
    start = "start of data" if start_ns is None else _seconds(start_ns)
    end = "end of data" if end_ns is None else _seconds(end_ns)
    return f"[{start}, {end}] (closed)"


def _format_reference(timing: Timing) -> str:
    if timing.reference_source is None:
        return "-"
    label = "expected period" if timing.reference_source is PeriodSource.EXPECTED else "median"
    return (
        f"{_millis(timing.reference_period_ns)} {label}, "
        f"gap if delta > {timing.gap_factor:g} x reference"
    )


def _format_counter(sequence: SequenceSummary) -> str:
    kind = "contiguous" if sequence.contiguous else "not defined as contiguous"
    modulus = "no rollover" if sequence.modulus is None else f"modulus {sequence.modulus}"
    return f"{kind}, {modulus}"
