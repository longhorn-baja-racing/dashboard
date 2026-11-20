"""Session data-quality reports keep recorded, inferred and retention facts apart."""

import pytest

from lbr_dashboard.core import TelemetrySession
from lbr_dashboard.core.contracts import (
    SourceError,
    SourceEvent,
    SourceEventKind,
    SourceHealth,
    SourceState,
)
from lbr_dashboard.core.data_quality import Completeness, Evidence, SequenceEventKind
from lbr_dashboard.core.session_quality import (
    SEQUENCE_KEY,
    ExpectedSamples,
    UnavailableRun,
    findings,
    signal_row,
    source_rows,
    summarize_snapshot,
)
from lbr_dashboard.core.signals import SignalDescriptor, SignalSample, SignalSchema

MS = 1_000_000


def descriptor(identifier, kind="numeric", rate=None, source="bench", counter=None):
    return SignalDescriptor(
        identifier,
        identifier.rsplit(".", 1)[-1],
        ("test",),
        kind,
        source,
        nominal_rate_hz=rate,
        extra={} if counter is None else {SEQUENCE_KEY: counter},
    )


def sample(time_ms, value, *, quality=None, **extra):
    return SignalSample(
        time_ms * MS,
        value,
        available=value is not None,
        quality=quality,
        extra=extra,
    )


def session(*descriptors, **options):
    return TelemetrySession(SignalSchema(1, tuple(descriptors)), **options)


def test_declared_rate_gives_expected_counts_gaps_and_unknown_completeness():
    store = session(descriptor("bench.load", rate=10), descriptor("bench.label", "string"))
    # 10 Hz from 0 to 1000 ms with 300 and 400 ms absent and 600 ms unavailable.
    times = [t for t in range(0, 1001, 100) if t not in (300, 400)]
    store.extend(
        "bench.load",
        [sample(t, None if t == 600 else float(t), quality="ok") for t in times],
    )
    store.extend("bench.label", [sample(0, "a"), sample(50, None), sample(500, "b")])
    report = summarize_snapshot(store.snapshot())
    load = report.signal("bench.load")
    assert load is not None
    expected = load.expected
    assert expected == ExpectedSamples(10.0, 0, 1000 * MS, 11, 9)
    assert expected is not None and expected.shortfall == 2
    assert load.quality.values.samples == 9 and load.quality.values.missing == 1
    gaps = load.quality.timing.gaps
    assert [(gap.start_ns, gap.end_ns, gap.missing_periods) for gap in gaps] == [
        (200 * MS, 500 * MS, 2)
    ]
    assert load.unavailable_runs == (UnavailableRun(600 * MS, 600 * MS, 1),)
    assert load.quality_labels == (("ok", 9),)
    assert load.quality.completeness is Completeness.UNKNOWN
    label = report.signal("bench.label")
    assert label is not None and label.expected is None
    assert label.quality.values.missing == 1  # string readings count as present or missing
    assert signal_row(load)[1:] == (
        "9",
        "1",
        "11 at 10 Hz, 2 without a record",
        "1 (~2 periods)",
        "unknown (no counter)",
        "unknown",
        "0",
    )


def test_same_snapshot_and_interval_reproduce_the_report():
    store = session(descriptor("bench.load", rate=100), descriptor("other.flow", rate=3))
    store.extend("bench.load", [sample(t, 1.0) for t in range(0, 500, 10) if t != 250])
    store.extend("other.flow", [sample(0, 2.0), sample(333, None), sample(1000, 4.0)])
    snapshot = store.snapshot()
    first = summarize_snapshot(snapshot, start_ns=100 * MS, end_ns=400 * MS)
    assert summarize_snapshot(snapshot, start_ns=100 * MS, end_ns=400 * MS) == first
    assert findings(first) == findings(
        summarize_snapshot(snapshot, start_ns=100 * MS, end_ns=400 * MS)
    )
    load = first.signal("bench.load")
    assert load is not None and load.quality.values.samples == 30
    flow = first.signal("other.flow")
    assert flow is not None and flow.quality.values.samples == 1
    with pytest.raises(ValueError, match="after its end"):
        summarize_snapshot(snapshot, start_ns=2, end_ns=1)


def test_arrival_order_regressions_are_reported():
    store = session(descriptor("bench.load"))
    store.extend("bench.load", [sample(0, 1.0), sample(20, 2.0), sample(10, 3.0)])
    load = summarize_snapshot(store.snapshot()).signal("bench.load")
    assert load is not None and load.quality.timing.regressions == 1


def test_retention_eviction_is_reported_apart_from_missing_data():
    store = session(descriptor("bench.load", rate=100), max_samples_per_signal=5)
    store.extend("bench.load", [sample(t, float(t)) for t in range(0, 200, 10)])
    report = summarize_snapshot(store.snapshot())
    load = report.signal("bench.load")
    assert load is not None
    assert load.evicted == 15 and report.evicted == 15
    assert load.quality.values.samples == 5 and load.quality.values.missing == 0
    assert load.expected is not None and load.expected.shortfall == 0
    assert load.quality.timing.gaps == ()
    assert signal_row(load)[7] == "15 (not data loss)"
    assert (
        "Evicted by retention [retention]",
        "15 older records removed to bound memory (not data loss)",
    ) in source_rows(report.sources[0])


def test_contiguous_counter_confirms_dropped_records_with_their_times():
    spec = {"field": "seq", "contiguous": True, "modulus": 8}
    store = session(descriptor("bench.load", counter=spec))
    counters = [5, 6, 7, 0, 3, 4]
    store.extend("bench.load", [sample(10 * i, 1.0, seq=value) for i, value in enumerate(counters)])
    load = summarize_snapshot(store.snapshot()).signal("bench.load")
    assert load is not None and load.quality.sequence is not None
    assert load.quality.sequence.dropped_records == 2
    assert load.quality.sequence.rollovers == 1
    assert load.quality.completeness is Completeness.INCOMPLETE
    events = [(item.event.kind, item.previous_ns, item.time_ns) for item in load.counter_events]
    assert events == [
        (SequenceEventKind.ROLLOVER, 20 * MS, 30 * MS),
        (SequenceEventKind.MISSING, 30 * MS, 40 * MS),
    ]
    assert signal_row(load)[5:7] == ("2", "incomplete")
    counter = [
        item for item in findings(summarize_snapshot(store.snapshot())) if item.kind == "counter"
    ]
    assert counter[1].start_ns == 30 * MS and counter[1].end_ns == 40 * MS
    assert counter[1].evidence is Evidence.RECORDED and counter[1].selectable


def test_counter_missing_on_a_record_leaves_completeness_unknown():
    spec = {"field": "seq", "contiguous": True}
    store = session(descriptor("bench.load", counter=spec))
    store.extend("bench.load", [sample(0, 1.0, seq=0), sample(10, 1.0)])
    load = summarize_snapshot(store.snapshot()).signal("bench.load")
    assert load is not None and load.quality.sequence is None
    assert load.counter_note == "some records have no seq counter value"
    assert signal_row(load)[6] == "unknown (some records have no seq counter value)"


def test_counter_not_declared_contiguous_never_confirms_drops():
    store = session(descriptor("bench.load", counter={"field": "seq"}))
    store.extend("bench.load", [sample(0, 1.0, seq=0), sample(10, 1.0, seq=4)])
    load = summarize_snapshot(store.snapshot()).signal("bench.load")
    assert load is not None and load.quality.sequence is not None
    assert load.quality.sequence.dropped_records is None
    assert signal_row(load)[5:7] == ("unknown (counter not contiguous)", "unknown")


def test_source_health_and_events_are_recorded_and_repeats_are_collapsed():
    store = session(descriptor("bench.load"), descriptor("other.flow", source="usb"))
    loss = SourceError(
        "loss", "Records lost", True, {"timestamp_ns": 200 * MS, "signal_id": "bench.load"}
    )
    lossy = SourceHealth(SourceState.RUNNING, dropped_records=4, last_error=loss)
    store.ingest(
        [
            SourceEvent(SourceEventKind.HEALTH, "bench", health=SourceHealth(SourceState.RUNNING)),
            SourceEvent(SourceEventKind.HEALTH, "bench", health=lossy),
            SourceEvent(SourceEventKind.HEALTH, "bench", health=lossy),
            SourceEvent(SourceEventKind.END_OF_STREAM, "bench", reason="complete"),
        ]
    )
    report = summarize_snapshot(store.snapshot())
    bench, usb = report.sources
    assert (bench.source_id, usb.source_id) == ("bench", "usb")
    assert [(note.kind, note.code, note.time_ns) for note in bench.notes] == [
        ("health", "loss", 200 * MS),
        ("end", "complete", None),
    ]
    assert bench.lost_records == 4 and bench.disconnected
    # Each source keeps its own evidence; the other source reported nothing.
    assert usb.health is None and usb.lost_records is None and not usb.disconnected
    rows = dict(source_rows(bench))
    assert rows["State [recorded]"].startswith("ended (complete) - no longer delivering")
    assert rows["Lost before the session [recorded]"] == "4 records the source reports it lost"
    assert rows["Last error [recorded]"] == "loss: Records lost"
    assert dict(source_rows(usb))["State [recorded]"] == "no health reported yet"
    notes = [item for item in findings(report) if item.kind == "source"]
    assert notes[0].start_ns == 200 * MS and not notes[0].selectable
    # Source notes outside the interval are left out; untimed notes always show.
    inside = findings(summarize_snapshot(store.snapshot(), start_ns=300 * MS, end_ns=400 * MS))
    assert [item.detail for item in inside] == [
        "bench reported end complete: stream ended: complete"
    ]
