"""Data-quality report for a loaded session snapshot.

This adapts a ``SessionSnapshot`` to the per-signal summaries in
``data_quality`` and adds the facts only the session knows: declared rates,
the quality labels sources attach to samples, records evicted by retention and
the health and events each source reported. It is pure, deterministic and
Qt-free, so the same snapshot and interval always give the same report.

Three kinds of fact are kept apart and never added together:

- recorded: stored by the source or file (values, availability, quality
  labels, sequence counters, and the health and events the source reported);
- inferred: derived by the dashboard from timestamps (gaps, rates, expected
  sample counts from a declared rate);
- retention: records the session itself evicted to bound memory. Eviction is
  not data loss and is never counted as missing data.

See docs/data-quality.md for every definition.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from numbers import Integral, Real

from .contracts import SourceError, SourceEvent, SourceEventKind, SourceHealth, SourceState
from .data_quality import (
    DEFAULT_GAP_FACTOR,
    Evidence,
    SequenceEvent,
    SignalQuality,
    summarize_signal,
)
from .session import SessionRecord, SessionSnapshot
from .signals import SignalDescriptor

SEQUENCE_KEY = "sequence"
"""Descriptor ``extra`` key that declares a per-sample sequence counter.

Its value is a mapping ``{"field": <sample extra key>, "contiguous": bool,
"modulus": int (optional)}``. Every record's ``sample.extra[field]`` then holds
the counter value stored by the source.
"""

DISCONNECTED_STATES = frozenset(
    {SourceState.STOPPING, SourceState.STOPPED, SourceState.CLOSED, SourceState.ENDED}
)


@dataclass(frozen=True, slots=True)
class ExpectedSamples:
    """Records expected from a declared rate between the first and last record."""

    evidence = Evidence.INFERRED

    rate_hz: float
    span_start_ns: int
    span_end_ns: int
    expected: int
    observed: int
    """Records in the span, including unavailable readings."""

    @property
    def shortfall(self) -> int:
        """Expected records with no record at all; never negative."""

        return max(0, self.expected - self.observed)


@dataclass(frozen=True, slots=True)
class UnavailableRun:
    """Consecutive records (in time) that the source stored without a value."""

    evidence = Evidence.RECORDED

    start_ns: int
    end_ns: int
    count: int


@dataclass(frozen=True, slots=True)
class CounterEvent:
    """A sequence counter event placed in time by the records on either side of it."""

    evidence = Evidence.RECORDED

    event: SequenceEvent
    previous_ns: int
    """Timestamp of the record before the step, in recorded order."""
    time_ns: int
    """Timestamp of the record after the step."""


@dataclass(frozen=True, slots=True)
class SignalReport:
    descriptor: SignalDescriptor
    quality: SignalQuality
    expected: ExpectedSamples | None
    """None when the signal declares no nominal rate."""
    unavailable_runs: tuple[UnavailableRun, ...]
    quality_labels: tuple[tuple[str, int], ...]
    """Counts of the quality label each record carries (recorded), by label."""
    evicted: int
    """Records of this signal evicted by session retention (not data loss)."""
    counter_note: str | None
    """Why a declared sequence counter could not be used, if it could not."""
    counter_events: tuple[CounterEvent, ...] = ()
    """The first sequence counter events with the times of the records around them."""

    @property
    def signal_id(self) -> str:
        return self.descriptor.identifier


@dataclass(frozen=True, slots=True)
class SourceNote:
    """One retained health, error or end-of-stream event reported by a source."""

    evidence = Evidence.RECORDED

    kind: str
    """``health``, ``error`` or ``end``."""
    code: str
    message: str
    time_ns: int | None
    """The measurement time the source attached to the event, if any."""
    signal_id: str | None
    recoverable: bool | None


@dataclass(frozen=True, slots=True)
class SourceReport:
    source_id: str
    health: SourceHealth | None
    """Latest health the source reported, or None if it has not reported any."""
    notes: tuple[SourceNote, ...]
    evicted: int
    """Records of this source's signals evicted by session retention."""

    @property
    def lost_records(self) -> int | None:
        """Records the source reports it lost before they reached the session."""

        return None if self.health is None else self.health.dropped_records

    @property
    def disconnected(self) -> bool:
        """The source is no longer delivering readings (ended, stopped or closed)."""

        return self.health is not None and self.health.state in DISCONNECTED_STATES


@dataclass(frozen=True, slots=True)
class SessionQualityReport:
    start_ns: int | None
    end_ns: int | None
    """The closed interval summarized; None on both sides for the whole session."""
    signals: tuple[SignalReport, ...]
    sources: tuple[SourceReport, ...]

    @property
    def evicted(self) -> int:
        return sum(item.evicted for item in self.signals)

    def signal(self, signal_id: str) -> SignalReport | None:
        return next((item for item in self.signals if item.signal_id == signal_id), None)


def summarize_snapshot(
    snapshot: SessionSnapshot,
    *,
    start_ns: int | None = None,
    end_ns: int | None = None,
    gap_factor: float = DEFAULT_GAP_FACTOR,
) -> SessionQualityReport:
    """Summarize every signal and source of ``snapshot`` over ``[start_ns, end_ns]``.

    Signals keep schema order. A declared ``nominal_rate_hz`` becomes the
    expected period for gap detection and the basis of the expected sample
    count; without one the median delta is used and no count is expected.
    """

    if start_ns is not None and end_ns is not None and start_ns > end_ns:
        raise ValueError("Interval start must not be after its end")
    signals = tuple(
        _signal_report(snapshot, descriptor, start_ns, end_ns, gap_factor)
        for descriptor in snapshot.schema.signals
    )
    source_ids = sorted(
        {item.source_id for item in snapshot.schema.signals} | set(snapshot.source_health)
    )
    sources = tuple(
        SourceReport(
            source_id,
            snapshot.source_health.get(source_id),
            _notes([event for event in snapshot.source_events if event.source_id == source_id]),
            sum(item.evicted for item in signals if item.descriptor.source_id == source_id),
        )
        for source_id in source_ids
    )
    return SessionQualityReport(start_ns, end_ns, signals, sources)


def _signal_report(
    snapshot: SessionSnapshot,
    descriptor: SignalDescriptor,
    start_ns: int | None,
    end_ns: int | None,
    gap_factor: float,
) -> SignalReport:
    identifier = descriptor.identifier
    # Recorded order is arrival order, so timestamp regressions stay visible.
    records = snapshot.arrival_order(identifier) if identifier in snapshot.series else ()
    times = [record.sample.timestamp_ns for record in records]
    values = [_value(record) for record in records]
    counter, note = _counter(descriptor, records)
    spec = descriptor.extra.get(SEQUENCE_KEY) if counter is not None else None
    modulus = spec.get("modulus") if isinstance(spec, Mapping) else None
    rate = descriptor.nominal_rate_hz
    period = None if rate is None else max(1, round(Fraction(10**9) / Fraction(str(rate))))
    quality = summarize_signal(
        times,
        values,
        name=identifier,
        start_ns=start_ns,
        end_ns=end_ns,
        expected_period_ns=period,
        gap_factor=gap_factor,
        sequence=counter,
        sequence_modulus=modulus if isinstance(modulus, int) else None,
        sequence_contiguous=isinstance(spec, Mapping) and spec.get("contiguous") is True,
    )
    counter_events = tuple(
        CounterEvent(event, times[event.index - 1], times[event.index])
        for event in (quality.sequence.events if quality.sequence is not None else ())
    )
    inside = sorted(
        (record for record in records if _inside(record.sample.timestamp_ns, start_ns, end_ns)),
        key=lambda record: (record.sample.timestamp_ns, record.sequence),
    )
    expected = None
    if rate is not None and inside:
        first, last = inside[0].sample.timestamp_ns, inside[-1].sample.timestamp_ns
        ticks = Fraction(last - first) * Fraction(str(rate)) / 10**9
        expected = ExpectedSamples(rate, first, last, int(ticks + Fraction(1, 2)) + 1, len(inside))
    labels = Counter(record.sample.quality or "-" for record in inside)
    return SignalReport(
        descriptor,
        quality,
        expected,
        _unavailable_runs(inside),
        tuple(sorted(labels.items())),
        snapshot.evicted.get(identifier, 0),
        note,
        counter_events,
    )


def _inside(time_ns: int, start_ns: int | None, end_ns: int | None) -> bool:
    return (start_ns is None or time_ns >= start_ns) and (end_ns is None or time_ns <= end_ns)


def _value(record: SessionRecord) -> float | None:
    """A float for numeric values, a placeholder for other present values, else None."""

    sample = record.sample
    if not sample.available:
        return None
    value = sample.value
    if isinstance(value, Real) and not isinstance(value, bool):
        return float(value)
    return 0.0


def _counter(
    descriptor: SignalDescriptor, records: Sequence[SessionRecord]
) -> tuple[list[int] | None, str | None]:
    spec = descriptor.extra.get(SEQUENCE_KEY)
    if spec is None:
        return None, None
    if not isinstance(spec, Mapping) or not isinstance(spec.get("field"), str):
        return None, "sequence counter declaration has no field name"
    modulus = spec.get("modulus")
    if modulus is not None and (
        isinstance(modulus, bool) or not isinstance(modulus, int) or modulus < 2
    ):
        return None, "sequence counter modulus must be an integer of at least 2"
    field = str(spec["field"])
    counter: list[int] = []
    for record in records:
        value = record.sample.extra.get(field)
        if isinstance(value, bool) or not isinstance(value, Integral):
            return None, f"some records have no {field} counter value"
        value = int(value)
        if modulus is not None and not 0 <= value < modulus:
            return None, f"{field} counter value {value} is outside [0, {modulus})"
        counter.append(value)
    return counter, None


def _unavailable_runs(records: Sequence[SessionRecord]) -> tuple[UnavailableRun, ...]:
    runs: list[UnavailableRun] = []
    start = end = None
    count = 0
    for record in (*records, None):
        if record is not None and not record.sample.available:
            if start is None:
                start = record.sample.timestamp_ns
            end = record.sample.timestamp_ns
            count += 1
            continue
        if start is not None and end is not None:
            runs.append(UnavailableRun(start, end, count))
        start = end = None
        count = 0
    return tuple(runs)


def _notes(events: Sequence[SourceEvent]) -> tuple[SourceNote, ...]:
    """Notes in event order; health that repeats the last reported error adds nothing."""

    notes: list[SourceNote] = []
    last: SourceError | None = None
    for event in events:
        if event.kind is SourceEventKind.END_OF_STREAM:
            assert event.reason is not None
            notes.append(
                SourceNote("end", event.reason, f"stream ended: {event.reason}", None, None, None)
            )
            continue
        if event.kind is SourceEventKind.ERROR:
            error = event.error
        elif event.kind is SourceEventKind.HEALTH:
            assert event.health is not None
            error = event.health.last_error
            if error is None or error == last:
                continue
        else:
            continue
        assert error is not None
        last = error
        time_ns = error.context.get("timestamp_ns")
        signal_id = error.context.get("signal_id")
        notes.append(
            SourceNote(
                "error" if event.kind is SourceEventKind.ERROR else "health",
                error.code,
                error.message,
                int(time_ns)
                if isinstance(time_ns, Integral) and not isinstance(time_ns, bool)
                else None,
                signal_id if isinstance(signal_id, str) else None,
                error.recoverable,
            )
        )
    return tuple(notes)


@dataclass(frozen=True, slots=True)
class Finding:
    """One placed finding for the report's finding list and timeline."""

    kind: str
    """``gap``, ``unavailable``, ``counter`` or ``source``."""
    evidence: Evidence
    signal_id: str | None
    start_ns: int | None
    end_ns: int | None
    detail: str

    @property
    def selectable(self) -> bool:
        """Whether the finding spans a time range that can become the interval."""

        return self.start_ns is not None and self.end_ns is not None and self.kind != "source"


def findings(report: SessionQualityReport) -> list[Finding]:
    """Gaps, unavailable runs, counter events and source notes, in time order."""

    found: list[Finding] = []
    for item in report.signals:
        timing = item.quality.timing
        for gap in timing.gaps:
            found.append(
                Finding(
                    "gap",
                    Evidence.INFERRED,
                    item.signal_id,
                    gap.start_ns,
                    gap.end_ns,
                    f"no record for {_millis(gap.duration_ns)}, "
                    f"~{gap.missing_periods} missing periods of "
                    f"{_millis(timing.reference_period_ns)}",
                )
            )
        for run in item.unavailable_runs:
            found.append(
                Finding(
                    "unavailable",
                    Evidence.RECORDED,
                    item.signal_id,
                    run.start_ns,
                    run.end_ns,
                    f"{run.count} {'reading' if run.count == 1 else 'readings'} "
                    "stored without a value",
                )
            )
        for placed in item.counter_events:
            event = placed.event
            detail = f"counter {event.kind.value} {event.previous} -> {event.value}"
            if event.skipped:
                detail += f", {event.skipped} counter values skipped"
            found.append(
                Finding(
                    "counter",
                    Evidence.RECORDED,
                    item.signal_id,
                    min(placed.previous_ns, placed.time_ns),
                    max(placed.previous_ns, placed.time_ns),
                    detail,
                )
            )
    for source in report.sources:
        for note in source.notes:
            inside = note.time_ns is None or _inside(note.time_ns, report.start_ns, report.end_ns)
            if not inside:
                continue
            found.append(
                Finding(
                    "source",
                    Evidence.RECORDED,
                    note.signal_id,
                    note.time_ns,
                    note.time_ns,
                    f"{source.source_id} reported {note.kind} {note.code}: {note.message}",
                )
            )
    return sorted(
        found,
        key=lambda item: (
            item.start_ns is None,
            item.start_ns or 0,
            item.signal_id or "",
            item.kind,
        ),
    )


SIGNAL_COLUMNS = (
    "Signal",
    "Records [recorded]",
    "Unavailable [recorded]",
    "Expected from rate [inferred]",
    "Gaps [inferred]",
    "Dropped [recorded]",
    "Completeness",
    "Evicted [retention]",
)


def signal_row(item: SignalReport) -> tuple[str, ...]:
    """ASCII cells for ``SIGNAL_COLUMNS``."""

    values, timing, sequence = item.quality.values, item.quality.timing, item.quality.sequence
    unavailable = str(values.missing)
    if values.non_finite:
        unavailable += f" (+{values.non_finite} non-finite)"
    expected = item.expected
    if expected is None:
        expected_text = "- (no declared rate)"
    else:
        expected_text = (
            f"{expected.expected} at {expected.rate_hz:g} Hz, {expected.shortfall} without a record"
        )
    gaps = str(len(timing.gaps))
    if timing.gaps:
        gaps += f" (~{sum(gap.missing_periods for gap in timing.gaps)} periods)"
    if sequence is None:
        dropped = "unknown (no counter)"
    elif sequence.dropped_records is None:
        dropped = "unknown (counter not contiguous)"
    else:
        dropped = str(sequence.dropped_records)
    completeness = item.quality.completeness.value
    if item.counter_note:
        completeness += f" ({item.counter_note})"
    evicted = f"{item.evicted} (not data loss)" if item.evicted else "0"
    return (
        f"{item.descriptor.display_name} ({item.signal_id})",
        str(values.samples),
        unavailable,
        expected_text,
        gaps,
        dropped,
        completeness,
        evicted,
    )


def source_rows(source: SourceReport) -> list[tuple[str, str]]:
    """ASCII ``(label, value)`` rows for one source."""

    health = source.health
    rows = [("Source", source.source_id)]
    if health is None:
        rows.append(("State [recorded]", "no health reported yet"))
    else:
        state = health.state.value
        if health.terminal_reason:
            state += f" ({health.terminal_reason})"
        if source.disconnected:
            state += " - no longer delivering; time after its last record is not missing data"
        rows += [
            ("State [recorded]", state),
            (
                "Lost before the session [recorded]",
                f"{health.dropped_records} records the source reports it lost",
            ),
            ("Waiting in the source buffer [recorded]", str(health.buffered_records)),
            ("Rejected controls [recorded]", str(health.rejected_controls)),
            (
                "Last error [recorded]",
                "-"
                if health.last_error is None
                else f"{health.last_error.code}: {health.last_error.message}",
            ),
        ]
    rows.append(
        (
            "Evicted by retention [retention]",
            f"{source.evicted} older records removed to bound memory (not data loss)"
            if source.evicted
            else "0",
        )
    )
    rows.append(("Source events [recorded]", str(len(source.notes))))
    return rows


def format_time(time_ns: int | None) -> str:
    return "-" if time_ns is None else f"{time_ns / 1e9:.3f} s"


def _millis(duration_ns: int | None) -> str:
    return "-" if duration_ns is None else f"{duration_ns / 1e6:.3f} ms"
