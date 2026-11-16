"""One conformance matrix, exercised by actual simulator and explicit CSV/fake adapters."""

from dataclasses import replace
from itertools import cycle
from pathlib import Path
from typing import Any, cast

import pytest

from lbr_dashboard.core import (
    CancellationToken,
    SignalDescriptor,
    SignalSample,
    SignalSchema,
    SourceCapabilities,
    SourceEvent,
    SourceEventKind,
    SourceState,
)
from lbr_dashboard.importers.csv_source import prepare_csv

from .checks import (
    AdapterRun,
    assert_cancelled,
    assert_closed,
    assert_controls,
    assert_failed,
    assert_finite,
    until,
)
from .examples import ControllableFake, Fixture, fixture, make_run


@pytest.fixture(params=["process", "motion"])
def spec(request) -> Fixture:
    return fixture(request.param)


@pytest.fixture(params=["fake", "csv", "simulator"])
def adapter_kind(request) -> str:
    return request.param


def test_descriptor_and_sample_fidelity(adapter_kind: str, spec: Fixture, tmp_path: Path):
    run = make_run(adapter_kind, spec, tmp_path / "fixture.csv")
    assert_finite(run, spec.schema, spec.expected)


def test_repeated_open_and_close(adapter_kind: str, spec: Fixture, tmp_path: Path):
    path = tmp_path / "fixture.csv"
    run = make_run(adapter_kind, spec, path)
    original = path.read_bytes() if path.exists() else None
    for _ in range(3):
        assert_finite(run, spec.schema, spec.expected)
    if original is not None:
        assert path.read_bytes() == original, "reader changed its source file"


def test_truthful_capabilities(adapter_kind: str, spec: Fixture, tmp_path: Path):
    run = make_run(adapter_kind, spec, tmp_path / "fixture.csv", capacity=3)
    try:
        capabilities = run.source.capabilities
        assert not capabilities.is_live
        assert capabilities.buffer_capacity == run.source.events.stats.capacity == 3
        assert_controls(run.source)
    finally:
        assert_closed(run)


def test_same_source_id_does_not_share_lifecycle(adapter_kind: str, spec: Fixture, tmp_path: Path):
    first = make_run(adapter_kind, spec, tmp_path / "first.csv", scenario="live", capacity=2)
    second = make_run(adapter_kind, spec, tmp_path / "second.csv")
    try:
        first.source.open()
        first.source.start()
        until(lambda: first.source.health().dropped_records > 0, "live fixture did not fill")
        second.source.open()
        assert first.source.source_id == second.source.source_id == spec.source_id
        assert first.source.events is not second.source.events, "same ID shared an event queue"
        assert_cancelled(first)
        assert second.source.state is SourceState.OPEN, "closing one run changed the other run"
        second.source.start()
        assert_finite(second, spec.schema, spec.expected, already_started=True)
    finally:
        assert_closed(first)
        assert_closed(second)


def test_cancelled_open(adapter_kind: str, spec: Fixture, tmp_path: Path):
    run = make_run(adapter_kind, spec, tmp_path / "fixture.csv")
    token = CancellationToken()
    token.cancel()
    try:
        run.source.open(token)
        assert run.source.state is SourceState.STOPPED
        assert run.source.health().terminal_reason == "cancelled"
        assert run.source.health().last_error is None
        assert not run.source.events.drain()
    finally:
        assert_closed(run)


def test_cancelled_start(adapter_kind: str, spec: Fixture, tmp_path: Path):
    run = make_run(adapter_kind, spec, tmp_path / "fixture.csv")
    token = CancellationToken()
    token.cancel()
    try:
        run.source.open()
        run.source.start(token)
        until(lambda: run.source.join(0), "pre-cancelled start did not stop on its own")
        assert run.source.state is SourceState.STOPPED
        assert run.source.health().terminal_reason == "cancelled"
        assert run.source.health().last_error is None
        assert all(e.kind is not SourceEventKind.RECORD for e in run.source.events.drain())
    finally:
        assert_closed(run)


def test_finite_backpressure_is_lossless(adapter_kind: str, spec: Fixture, tmp_path: Path):
    run = make_run(adapter_kind, spec, tmp_path / "fixture.csv", capacity=2)
    try:
        run.source.open()
        run.source.start()
        until(
            lambda: run.source.events.stats.buffered == 2, "fixture never filled its bounded queue"
        )
        assert not run.source.join(0.08), "finite source discarded data while its consumer stalled"
        assert_finite(run, spec.schema, spec.expected, already_started=True)
    finally:
        assert_closed(run)


def test_live_overflow_is_bounded_observable_and_cancellable(
    adapter_kind: str,
    spec: Fixture,
    tmp_path: Path,
):
    run = make_run(adapter_kind, spec, tmp_path / "fixture.csv", scenario="live", capacity=2)
    try:
        assert run.source.capabilities.is_live
        run.source.open()
        run.source.start()
        until(lambda: run.source.health().dropped_records > 0, "live overflow was not observable")
        assert run.source.events.stats.buffered <= 2, "live handoff exceeded its capacity"
        assert_cancelled(run)
        assert run.source.health().dropped_records > 0, "close erased recorded loss"
    finally:
        assert_closed(run)


def test_failure_after_start_is_observable(adapter_kind: str, spec: Fixture, tmp_path: Path):
    run = make_run(adapter_kind, spec, tmp_path / "fixture.csv", scenario="error")
    assert_failed(run, has_records=True)


def test_invalid_record_is_never_published(adapter_kind: str, spec: Fixture, tmp_path: Path):
    if adapter_kind == "simulator":
        pytest.skip(
            "simulator validates its configuration; malformed reader output applies to decoders"
        )
    run = make_run(adapter_kind, spec, tmp_path / "fixture.csv", scenario="bad")
    delivered = assert_failed(run, has_records=True)
    assert all(e.signal_id != "unregistered.signal" for e in delivered)


def test_cancel_during_opening_releases_reader(adapter_kind: str, spec: Fixture, tmp_path: Path):
    if adapter_kind == "simulator":
        pytest.skip("in-memory simulator has no blocking input-opening phase")
    run = make_run(adapter_kind, spec, tmp_path / "fixture.csv", scenario="opening")
    try:
        run.source.open()
        run.source.start()
        until(lambda: not run.resources_idle(), "opening fixture never acquired its resource")
        assert_cancelled(run)
    finally:
        assert_closed(run)


def test_original_order_and_duplicate_records_are_preserved(
    adapter_kind: str,
    spec: Fixture,
    tmp_path: Path,
):
    if adapter_kind == "simulator":
        pytest.skip("the simulator's configured schedule is ordered; late input is a decoder case")
    run = make_run(adapter_kind, spec, tmp_path / "fixture.csv", scenario="out_of_order")
    expected = (spec.expected[2], spec.expected[0], spec.expected[0], *spec.expected[1:])
    assert_finite(run, spec.schema, expected)


@pytest.mark.parametrize("corrupt", [b"signal,time_ns\nwrong,not-a-time\n", b"signal\n\xff\n"])
def test_csv_decode_errors_are_reported(spec: Fixture, tmp_path: Path, corrupt: bytes):
    path = tmp_path / "fixture.csv"
    run = make_run("csv", spec, path)
    path.write_bytes(corrupt)
    assert_failed(run, has_records=False)


@pytest.mark.parametrize(
    ("headers", "rows", "times_ns", "columns", "repeats"),
    [
        (
            ("pressure", "pressure"),
            ("-0.000007,0,20", "2.1,,21", "2.1,8,22", "1.5,9,"),
            (-7, 2_100_000, 2_100_000, 1_500_000),
            (("numeric", (0.0, None, 8.0, 9.0)), ("numeric", (20.0, 21.0, 22.0, None))),
            20,
        ),
        (
            ("angle", "state"),
            ("1152921504606.846989,0,ready", "1152921504606.846990,-2,"),
            (2**60 + 13, 2**60 + 14),
            (("numeric", (0.0, -2.0)), ("string", ("ready", None))),
            1,
        ),
    ],
    ids=["process", "motion"],
)
def test_timestamp_ms_csv_replay_matches_the_finite_source_contract(
    tmp_path: Path, headers, rows, times_ns, columns, repeats
):
    path = tmp_path / "log.csv"
    original = f"timestamp_ms,{','.join(headers)}\n" + ("\n".join(rows) + "\n") * repeats
    path.write_bytes(original.encode("utf-8"))
    source_id = str(path.resolve())
    descriptors = []
    expected = []
    for column, (name, (kind, values)) in enumerate(zip(headers, columns), start=1):
        signal_id = f"csv.column.{column}"
        descriptors.append(
            SignalDescriptor(
                signal_id,
                name,
                ("csv",),
                kind,
                source_id,
                timestamp_domain="relative",
                display={"csv_column": column, "csv_header": name},
            )
        )
        expected.extend(
            SourceEvent(
                SourceEventKind.RECORD,
                source_id,
                signal_id=signal_id,
                sample=SignalSample(
                    timestamp_ns,
                    value,
                    available=value is not None,
                    quality="measured" if value is not None else "missing",
                    source_id=source_id,
                    timestamp_domain="relative",
                ),
            )
            for timestamp_ns, value in zip(times_ns * repeats, values * repeats, strict=True)
        )
    run = AdapterRun(prepare_csv(path, CancellationToken()).source())
    source = run.source
    assert_controls(source)
    assert not source.capabilities.is_live
    for _ in range(2):
        try:
            source.open()
            source.start()
            if repeats > 1:
                capacity = source.capabilities.buffer_capacity
                assert len(expected) > capacity, "CSV fixture must exceed queue capacity"
                until(lambda: source.events.stats.buffered == capacity, "CSV queue never filled")
                assert not source.join(0.08), "CSV replay completed while the consumer was stalled"
            assert_finite(
                run, SignalSchema(1, tuple(descriptors)), tuple(expected), already_started=True
            )
        finally:
            assert_closed(run)
    assert path.read_bytes() == original.encode("utf-8")


@pytest.mark.parametrize(
    ("family", "wanted_value", "wrong_value"),
    [("motion", False, 0), ("motion", True, 1), ("process", 7, 7.0)],
    ids=["false-as-zero", "true-as-one", "int-as-float"],
)
def test_conformance_rejects_equal_values_of_the_wrong_type(
    tmp_path: Path, monkeypatch, family: str, wanted_value, wrong_value
):
    spec = fixture(family)
    run = make_run("fake", spec, tmp_path / "unused.csv")
    source = run.source
    source.open()
    drain = source.events.drain

    def wrong_type_drain(limit=None):
        events = []
        for event in drain(limit):
            sample = event.sample
            if (
                sample is not None
                and type(sample.value) is type(wanted_value)
                and sample.value == wanted_value
            ):
                event = replace(event, sample=replace(sample, value=wrong_value))
            events.append(event)
        return tuple(events)

    monkeypatch.setattr(source.events, "drain", wrong_type_drain)
    assert wanted_value == wrong_value
    try:
        source.start()
        with pytest.raises(AssertionError, match="value type changed"):
            assert_finite(run, spec.schema, spec.expected, already_started=True)
    finally:
        assert_closed(run)


def test_supported_controls_require_effect_probes(spec: Fixture):
    source = ControllableFake(spec)

    def seek_probe(candidate):
        target = spec.epoch + 500_000_000
        candidate.seek(target)
        expected = tuple(e for e in spec.expected if e.sample and e.sample.timestamp_ns >= target)
        assert_finite(AdapterRun(candidate), spec.schema, expected)

    def rate_probe(candidate):
        candidate.seek(spec.epoch)
        candidate.set_rate(2)
        assert_finite(AdapterRun(candidate), spec.schema, spec.expected)
        assert source.arrival_ns[-1] == 500_000_000, "rate control did not change delivery schedule"

    try:
        assert_controls(source, seek_probe=seek_probe, rate_probe=rate_probe)
        with pytest.raises(AssertionError, match="effect probe"):
            assert_controls(source)
    finally:
        assert_closed(AdapterRun(source))


@pytest.mark.parametrize("defect", ["identity", "time", "zero", "unbounded", "capacity", "stop"])
def test_the_suite_rejects_broken_adapters(spec: Fixture, tmp_path: Path, monkeypatch, defect: str):
    """Mutate real adapter delivery/control behavior, then invoke the same public assertions."""
    run = make_run("fake", spec, tmp_path / "fixture.csv", capacity=2 if defect == "stop" else 32)
    source = cast(Any, run.source)
    # Open allocates the queue for this run. Mutate the queue that consumers actually use.
    source.open()
    if defect in {"identity", "time", "zero"}:
        original = source.events.drain

        def broken_drain(limit=None):
            events = []
            for event in original(limit):
                if event.kind is SourceEventKind.RECORD:
                    if defect == "identity":
                        event = replace(event, signal_id="wrong.identity")
                    elif defect == "time":
                        event = replace(event, sample=replace(event.sample, timestamp_ns=0))
                    elif not event.sample.available:
                        event = replace(
                            event, sample=replace(event.sample, value=0, available=True)
                        )
                events.append(event)
            return tuple(events)

        monkeypatch.setattr(source.events, "drain", broken_drain)
        message = (
            "identity changed"
            if defect == "identity"
            else "timestamp changed"
            if defect == "time"
            else "missingness"
        )
    elif defect == "unbounded":

        def forever(token):
            for event in cycle(spec.expected):
                token.raise_if_cancelled()
                yield event

        monkeypatch.setattr(source, "_factory", forever)
        message = "unbounded/unexpected"
    elif defect == "capacity":
        monkeypatch.setattr(source, "_capabilities", SourceCapabilities(False, False, False, 1))
        message = "capacity misreported"
    else:
        monkeypatch.setattr(source, "stop", lambda: None)
        source.start()
        until(lambda: source.events.stats.buffered == 2, "mutation never blocked publication")
        try:
            with pytest.raises(AssertionError, match="stop did not release"):
                assert_cancelled(run)
        finally:
            monkeypatch.undo()
            assert_closed(run)
        return
    try:
        source.start()
        with pytest.raises(AssertionError, match=message):
            assert_finite(run, spec.schema, spec.expected, already_started=True)
    finally:
        monkeypatch.undo()
        assert_closed(run)
