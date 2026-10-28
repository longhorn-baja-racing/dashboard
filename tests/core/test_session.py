"""Sparse session acceptance, retention, provenance and atomic snapshot tests."""

from dataclasses import FrozenInstanceError, replace
from itertools import repeat
from threading import Barrier, Thread
from typing import Any, cast

import pytest

from lbr_dashboard.core import (
    IteratorSource,
    RejectedSourceBatch,
    SignalDescriptor,
    SignalSample,
    SignalSchema,
    SourceError,
    SourceEvent,
    SourceEventKind,
    SourceHealth,
    SourceState,
    TelemetrySession,
)


def descriptor(identifier="lab.pressure", source="fixture", kind="numeric", domain="relative"):
    return SignalDescriptor(
        identifier, "Reading", ("lab",), kind, source, canonical_unit="Pa", timestamp_domain=domain
    )


def sample(time=0, value: Any = 1, domain="relative", **kwargs):
    return SignalSample(time, value, timestamp_domain=domain, **kwargs)


def event(time=0, value: Any = 1, identifier="lab.pressure", source="fixture", **kwargs):
    return SourceEvent(
        SourceEventKind.RECORD, source, signal_id=identifier, sample=sample(time, value, **kwargs)
    )


def session(**kwargs):
    return TelemetrySession(SignalSchema(1, (descriptor(),)), **kwargs)


def values(records):
    return [record.sample.value for record in records]


def test_mixed_rates_unrelated_types_preserve_exact_independent_samples() -> None:
    schema = SignalSchema(
        1,
        (
            descriptor(),
            descriptor("other.temperature", "sim"),
            descriptor("switch.enabled", kind="boolean"),
            descriptor("events.notice", kind="event"),
        ),
    )
    store = TelemetrySession(schema, metadata={"source_mapping": {"raw": " k "}})
    fast = [
        sample(
            i * 10_000_000,
            None if i == 50 else i,
            available=i != 50,
            quality="raw",
            unit="psi",
            source_id="fixture",
        )
        for i in range(101)
    ]
    store.extend("lab.pressure", fast)
    store.extend("other.temperature", (sample(0, 20), sample(1_000_000_000, 21)))
    store.extend("switch.enabled", (sample(-3, False), sample(37, True)))
    store.append("events.notice", sample(123, {"code": "ready"}))
    snapshot = store.snapshot()
    assert len(snapshot.series["lab.pressure"]) == 101
    assert len(snapshot.series["other.temperature"]) == 2
    assert values(snapshot.range("switch.enabled", -3, 37)) == [False, True]
    assert snapshot.series["events.notice"][0].sample.value == {"code": "ready"}
    for record, original in zip(snapshot.series["lab.pressure"], fast):
        assert record.sample is original
        assert record.source_id == "fixture" and record.signal_id == "lab.pressure"
        assert record.sample.unit == "psi" and record.sample.quality == "raw"
    missing = snapshot.nearest("lab.pressure", 500_000_000)
    assert missing is not None and missing.sample.value is None and not missing.sample.available
    assert snapshot.nearest("other.temperature", 500_000_000, max_distance_ns=100_000_000) is None
    assert store.range("other.temperature", 1, 999_999_999) == ()
    assert snapshot.domains["lab.pressure"] == "relative"


def test_queries_boundaries_duplicate_ties_out_of_order_and_arrival_provenance() -> None:
    store = session()
    store.extend("lab.pressure", [sample(20, 2), sample(0, 0), sample(10, 1), sample(10, 11)])
    view = store.snapshot()
    assert values(store.range("lab.pressure", 0, 20)) == [0, 1, 11, 2]
    assert values(view.range("lab.pressure", 10, 10)) == [1, 11]
    assert values(view.arrival_order("lab.pressure")) == [2, 0, 1, 11]
    assert store.bounds("lab.pressure") == view.bounds("lab.pressure") == (0, 20)
    for target, expected in ((-1, 0), (0, 0), (5, 0), (10, 11), (15, 11), (30, 2)):
        record = store.nearest("lab.pressure", target)
        assert record is not None and record.sample.value == expected
        assert view.nearest("lab.pressure", target) == record
    assert store.nearest("lab.pressure", -1, max_distance_ns=0) is None
    assert store.nearest("lab.pressure", 30, max_distance_ns=10) == view.series["lab.pressure"][-1]
    duplicate = session()
    duplicate.extend("lab.pressure", [sample(10, 1), sample(10, 2)])
    record = duplicate.nearest("lab.pressure", 0)
    assert record is not None and record.sample.value == 2


def test_empty_queries_unknown_signal_and_exact_large_negative_times() -> None:
    store = session()
    assert store.bounds("lab.pressure") is None
    assert store.snapshot().bounds("lab.pressure") is None
    assert store.nearest("lab.pressure", 0) is None
    assert store.range("lab.pressure", 0, 1) == ()
    for query in (
        lambda: store.range("unknown.id", 0, 1),
        lambda: store.bounds("unknown.id"),
        lambda: store.nearest("unknown.id", 0),
        lambda: store.append("unknown.id", sample()),
    ):
        with pytest.raises(KeyError):
            query()
    for stamp in (-(2**80) - 1, 2**80 + 1):
        original = sample(stamp)
        store.append("lab.pressure", original)
        assert store.range("lab.pressure", stamp, stamp)[0].sample is original


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_samples_per_signal": 0},
        {"max_samples_per_signal": True},
        {"retention_ns": -1},
        {"retention_ns": 1.5},
        {"max_source_events": 0},
        {"max_batch_events": False},
        {"metadata": {"bad": float("nan")}},
    ],
)
def test_invalid_configuration(kwargs) -> None:
    with pytest.raises(ValueError):
        session(**kwargs)


@pytest.mark.parametrize("stamp", [True, 1.5, float("nan"), float("inf"), None, "1"])
def test_invalid_query_values(stamp) -> None:
    store = session()
    with pytest.raises(ValueError):
        store.range("lab.pressure", stamp, 0)
    with pytest.raises(ValueError):
        store.range("lab.pressure", 0, stamp)
    with pytest.raises(ValueError):
        store.nearest("lab.pressure", stamp)
    with pytest.raises(ValueError):
        store.nearest("lab.pressure", 0, max_distance_ns=stamp if stamp is not None else -1)


def test_reversed_range_negative_tolerance_and_invalid_schema() -> None:
    store = session()
    with pytest.raises(ValueError):
        store.range("lab.pressure", 2, 1)
    with pytest.raises(ValueError):
        store.nearest("lab.pressure", 0, max_distance_ns=-1)
    with pytest.raises(TypeError):
        TelemetrySession(cast(Any, None))


def test_count_and_age_retention_include_cutoff_and_ignore_late_old_data() -> None:
    store = session(max_samples_per_signal=3, retention_ns=10)
    store.extend("lab.pressure", [sample(i, i) for i in (0, 5, 10)])
    old = store.snapshot()
    store.append("lab.pressure", sample(15, 15))
    assert values(store.range("lab.pressure", -100, 100)) == [5, 10, 15]
    store.append("lab.pressure", sample(-100, -100))
    assert values(store.range("lab.pressure", -100, 100)) == [5, 10, 15]
    store.append("lab.pressure", sample(100, 100))
    assert store.bounds("lab.pressure") == (100, 100)
    assert store.snapshot().evicted["lab.pressure"] == 5
    assert values(old.range("lab.pressure", -100, 100)) == [0, 5, 10]
    assert old.evicted["lab.pressure"] == 0


def test_count_retention_duplicates_zero_age_and_per_signal_watermarks() -> None:
    schema = SignalSchema(1, (descriptor(), descriptor("slow.level")))
    store = TelemetrySession(schema, max_samples_per_signal=2, retention_ns=0)
    store.append("slow.level", sample(0, 0))
    store.extend("lab.pressure", [sample(100, 1), sample(100, 2), sample(100, 3), sample(99, 4)])
    assert values(store.range("lab.pressure", 0, 100)) == [2, 3]
    assert values(store.range("slow.level", 0, 100)) == [0]
    assert store.snapshot().evicted["lab.pressure"] == 2
    count_only = session(max_samples_per_signal=2)
    count_only.extend("lab.pressure", [sample(i, i) for i in range(5)])
    assert values(count_only.range("lab.pressure", 0, 5)) == [3, 4]


def test_snapshot_and_nested_metadata_are_immutable_detached_and_consistent() -> None:
    metadata: Any = {"mapping": {"columns": [1, 2]}}
    store = session(metadata=metadata)
    metadata["mapping"]["columns"].append(3)
    store.append("lab.pressure", sample())
    view = store.snapshot()
    store.append("lab.pressure", sample(1, 2))
    assert len(view.series["lab.pressure"]) == 1
    assert view.metadata == {"mapping": {"columns": (1, 2)}}
    for field in ("series", "domains", "source_health", "evicted", "metadata"):
        with pytest.raises(TypeError):
            cast(Any, getattr(view, field))["injected"] = 1
    with pytest.raises(TypeError):
        cast(Any, view.metadata)["mapping"]["columns"][0] = 9
    with pytest.raises(TypeError):
        cast(Any, view.series["lab.pressure"])[0] = None
    with pytest.raises(FrozenInstanceError):
        cast(Any, view.series["lab.pressure"][0]).sequence = 99
    assert view.revision + 1 == store.snapshot().revision
    assert store.schema is view.schema


@pytest.mark.parametrize(
    "bad",
    [
        event(value="bad"),
        event(identifier="unknown.id"),
        event(source="other"),
        SourceEvent(SourceEventKind.DESCRIPTOR, "fixture", descriptor=descriptor(kind="boolean")),
        object(),
    ],
)
def test_invalid_batch_is_atomic_across_records_health_and_metadata(bad) -> None:
    store = session()
    store.append("lab.pressure", sample())
    before = store.snapshot()
    with pytest.raises((ValueError, KeyError)):
        store.ingest(
            (
                event(2, 2),
                SourceEvent(
                    SourceEventKind.HEALTH, "fixture", health=SourceHealth(SourceState.RUNNING)
                ),
                bad,
            )
        )
    assert store.snapshot() == before


def test_mixed_clock_domains_rejected_without_losing_original_times() -> None:
    store = TelemetrySession(SignalSchema(1, (descriptor(domain="unspecified"),)))
    store.append("lab.pressure", sample(-20, 0, domain="device_monotonic"))
    before = store.snapshot()
    with pytest.raises(ValueError, match="timestamp domains"):
        store.append("lab.pressure", sample(-19, 1, domain="unix_utc"))
    assert store.snapshot() == before
    explicit = session()
    with pytest.raises(ValueError):
        explicit.append("lab.pressure", sample(domain="unix_utc"))


def test_batch_limit_is_atomic_and_even_bounds_an_infinite_input_iterator() -> None:
    store = session(max_batch_events=2)
    before = store.snapshot()
    with pytest.raises(ValueError, match="batch exceeds"):
        store.ingest(repeat(event()))
    assert store.snapshot() == before
    assert store.ingest(()) == 0 and store.snapshot() == before
    with pytest.raises(ValueError):
        store.ingest((), source_health={"unknown": SourceHealth(SourceState.OPEN)})
    with pytest.raises(ValueError):
        store.ingest((), source_health={"fixture": cast(Any, 1)})


def test_latest_health_and_errors_terminal_are_retained_beyond_event_history() -> None:
    store = session(max_source_events=2)
    error = SourceError("gap", "Missing records", True)
    store.ingest(
        (
            SourceEvent(SourceEventKind.ERROR, "fixture", error=error),
            SourceEvent(SourceEventKind.END_OF_STREAM, "fixture", reason="cancelled"),
        )
    )
    assert store.snapshot().source_health["fixture"].last_error == error
    assert store.snapshot().source_health["fixture"].state is SourceState.STOPPED
    assert store.snapshot().source_health["fixture"].terminal_reason == "cancelled"
    newer = SourceHealth(SourceState.RUNNING, dropped_records=7)
    newest = replace(newer, dropped_records=9)
    store.ingest(
        (
            SourceEvent(SourceEventKind.HEALTH, "fixture", health=newer),
            SourceEvent(SourceEventKind.HEALTH, "fixture", health=newest),
        )
    )
    view = store.snapshot()
    assert len(view.source_events) == 2
    assert view.source_health["fixture"] == newest
    fatal = SourceError("lost", "Disconnected")
    store.ingest(
        (
            SourceEvent(SourceEventKind.ERROR, "fixture", error=fatal),
            SourceEvent(SourceEventKind.END_OF_STREAM, "fixture", reason="failed"),
        )
    )
    assert store.snapshot().source_health["fixture"].state is SourceState.FAILED


def test_source_integration_retains_health_when_controls_cannot_fit() -> None:
    schema = SignalSchema(1, (descriptor(),))

    def broken(token):
        yield event()
        raise OSError("Read failed")

    source = IteratorSource("fixture", schema, broken, capacity=1, publish_timeout=0)
    store = TelemetrySession(schema, max_samples_per_signal=3)
    source.open()
    source.start()
    try:
        assert source.join(1)
        assert store.ingest_source(source) == 0
        view = store.snapshot()
        health = view.source_health["fixture"]
        assert health.last_error is not None and health.terminal_reason == "failed"
        assert health.dropped_records == 1 and health.rejected_controls == 2
        assert view.schema == schema
    finally:
        assert source.close()


@pytest.mark.parametrize("live", [False, True])
def test_same_queries_for_real_finite_and_live_source_interfaces(live) -> None:
    schema = SignalSchema(1, (descriptor(),))
    source = IteratorSource(
        "fixture",
        schema,
        lambda token: iter((event(0, 0), event(10, None, available=False), event(20, 2))),
        is_live=live,
    )
    store = TelemetrySession(schema, max_samples_per_signal=10)
    source.open()
    source.start()
    try:
        assert source.join(1)
        assert store.ingest_source(source, limit=1) == 0  # descriptor only
        assert store.ingest_source(source) == 3
        assert values(store.range("lab.pressure", 0, 20)) == [0, None, 2]
        assert store.snapshot().source_health["fixture"].terminal_reason == "complete"
        assert store.ingest_source(source, limit=0) == 0
    finally:
        assert source.close()


def test_source_preflight_rejection_does_not_drain_and_bad_batch_is_recoverable() -> None:
    schema = SignalSchema(1, (descriptor(domain="unspecified"),))
    source = IteratorSource(
        "fixture", schema, lambda token: iter((event(), event(1, domain="other"))), is_live=True
    )
    source.open()
    source.start()
    try:
        assert source.join(1)
        for store in (
            TelemetrySession(schema),
            session(max_samples_per_signal=10),
            TelemetrySession(
                SignalSchema(1, (descriptor(source="other"),)), max_samples_per_signal=10
            ),
        ):
            before = source.events.stats
            with pytest.raises(ValueError):
                store.ingest_source(source)
            assert source.events.stats == before
        store = TelemetrySession(schema, max_samples_per_signal=10)
        with pytest.raises(ValueError):
            store.ingest_source(source, limit=-1)
        before = store.snapshot()
        with pytest.raises(RejectedSourceBatch) as rejected:
            store.ingest_source(source)
        assert len(rejected.value.events) == 4
        assert rejected.value.__cause__ is not None
        assert store.snapshot() == before
    finally:
        assert source.close()


def test_multi_signal_batches_and_snapshots_are_consistent_under_concurrency() -> None:
    schema = SignalSchema(1, (descriptor(), descriptor("slow.level")))
    store = TelemetrySession(schema, max_samples_per_signal=5)
    barrier = Barrier(2)
    failures = []

    def writer():
        try:
            for i in range(30):
                store.ingest((event(i, i), event(i, i, "slow.level")))
                barrier.wait(3)
                barrier.wait(3)
        except Exception as error:
            failures.append(error)
            barrier.abort()

    worker = Thread(target=writer)
    worker.start()
    try:
        for i in range(30):
            barrier.wait(3)
            view = store.snapshot()
            assert values(view.series["lab.pressure"]) == values(view.series["slow.level"])
            assert view.revision == i + 1
            assert len(view.series["lab.pressure"]) <= 5
            assert view.evicted["lab.pressure"] == view.evicted["slow.level"]
            barrier.wait(3)
    finally:
        if failures:
            barrier.abort()
        worker.join(3)
        assert not worker.is_alive() and not failures


def test_large_batched_input_and_narrow_queries() -> None:
    store = session()
    store.extend("lab.pressure", (sample(i, i) for i in range(10000)))
    assert len(store.range("lab.pressure", 5432, 5442)) == 11
    record = store.nearest("lab.pressure", 9999)
    assert record is not None and record.sample.value == 9999
    assert store.bounds("lab.pressure") == (0, 9999)
    assert store.snapshot().evicted["lab.pressure"] == 0


def test_known_signal_rejects_other_known_sources_and_explicit_health_wins() -> None:
    schema = SignalSchema(1, (descriptor(), descriptor("other.level", source="other")))
    store = TelemetrySession(schema)
    with pytest.raises(ValueError, match="event source"):
        store.ingest((event(source="other"),))
    current = SourceHealth(SourceState.FAILED, last_error=SourceError("lost", "Lost input"))
    store.ingest(
        (
            SourceEvent(SourceEventKind.HEALTH, "fixture", health=SourceHealth(SourceState.OPEN)),
            SourceEvent(SourceEventKind.END_OF_STREAM, "fixture", reason="complete"),
        ),
        source_health={"fixture": current},
    )
    assert store.snapshot().source_health["fixture"] == current


def test_queries_match_reference_results_with_random_late_duplicate_and_missing_records() -> None:
    from random import Random

    random = Random(8123)
    samples = [
        sample(random.randrange(-200, 200), None if i % 7 == 0 else i, available=i % 7 != 0)
        for i in range(500)
    ]
    store = session()
    store.extend("lab.pressure", samples)
    expected = sorted(enumerate(samples), key=lambda pair: (pair[1].timestamp_ns, pair[0]))
    for _ in range(100):
        first, last = sorted((random.randrange(-250, 250), random.randrange(-250, 250)))
        target = random.randrange(-250, 250)
        reference = [value for _, value in expected if first <= value.timestamp_ns <= last]
        assert [item.sample for item in store.range("lab.pressure", first, last)] == reference
        index, value = min(
            enumerate(samples),
            key=lambda pair: (abs(pair[1].timestamp_ns - target), pair[1].timestamp_ns, -pair[0]),
        )
        actual = store.nearest("lab.pressure", target)
        assert actual is not None and actual.sample is value and actual.sequence == index + 1


def test_repeated_identical_health_poll_does_not_advance_revision() -> None:
    store = session()
    health = {"fixture": SourceHealth(SourceState.ENDED, terminal_reason="complete")}
    store.ingest((), source_health=health)
    before = store.snapshot()
    store.ingest((), source_health=health)
    assert store.snapshot() == before
