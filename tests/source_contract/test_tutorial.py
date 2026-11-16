"""Run the documented adapter through the shared contract assertions."""

from dataclasses import replace
from pathlib import Path

import pytest

from lbr_dashboard.core import (
    SignalDescriptor,
    SignalSample,
    SignalSchema,
    SourceEvent,
    SourceEventKind,
)

from . import tutorial
from .checks import (
    AdapterRun,
    assert_cancelled,
    assert_closed,
    assert_failed,
    assert_finite,
    records,
    until,
)
from .tutorial import bench_source

FIXTURE = Path(__file__).parents[1] / "fixtures" / "tutorial-bench-log.txt"
DESCRIPTOR = SignalDescriptor(
    "bench.pressure",
    "Bench pressure",
    ("bench",),
    "numeric",
    "tutorial.bench",
    canonical_unit="Pa",
    timestamp_domain="device_monotonic",
)
SCHEMA = SignalSchema(1, (DESCRIPTOR,))
EXPECTED = tuple(
    SourceEvent(
        SourceEventKind.RECORD,
        "tutorial.bench",
        signal_id="bench.pressure",
        sample=SignalSample(
            stamp,
            value,
            available=value is not None,
            source_id="tutorial.bench",
            unit="Pa",
            timestamp_domain="device_monotonic",
        ),
    )
    for stamp, value in ((0, 0), (10_000_000, None), (25_000_000, 7), (40_000_000, 11))
)


def test_tutorial_adapter_conforms():
    assert_finite(AdapterRun(bench_source(FIXTURE)), SCHEMA, EXPECTED)


@pytest.mark.parametrize("ending", [b"", b"\n", b"\r\n"])
def test_exact_timestamp_and_maximum_record_width(tmp_path, ending):
    path = tmp_path / "boundary.txt"
    path.write_bytes(b"9223372036854775," + b"0" * 47 + ending)
    sample = EXPECTED[0].sample
    assert sample is not None
    event = replace(EXPECTED[0], sample=replace(sample, timestamp_ns=9_223_372_036_854_775_000))
    assert_finite(AdapterRun(bench_source(path)), SCHEMA, (event,))


def test_empty_file_is_a_source_failure(tmp_path):
    path = tmp_path / "empty.txt"
    path.write_bytes(b"")
    run = AdapterRun(bench_source(path))
    assert_failed(run, has_records=False)
    error = run.source.health().last_error
    assert error is not None and "no readings" in error.message


def test_record_limit_preserves_accepted_prefix_and_reports_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(tutorial, "MAX_RECORDS", 4)
    path = tmp_path / "oversized.txt"
    # Write a complete fifth record without relying on the fixture's line endings.
    path.write_text(FIXTURE.read_text(encoding="ascii").rstrip() + "\n50000,12\n", encoding="ascii")
    run = AdapterRun(bench_source(path))
    events = assert_failed(run, has_records=True)
    assert records(events) == EXPECTED
    error = run.source.health().last_error
    assert error is not None and "line 5: log exceeds 4 readings" in error.message


def test_conformance_rejects_a_fabricated_zero(tmp_path):
    path = tmp_path / "bad-mapping.txt"
    path.write_text(
        FIXTURE.read_text(encoding="ascii").replace("10000,\n", "10000,0\n"), encoding="ascii"
    )
    with pytest.raises(AssertionError, match="missingness changed"):
        assert_finite(AdapterRun(bench_source(path)), SCHEMA, EXPECTED)


@pytest.mark.parametrize(
    "bad_row",
    [b"bad,1\n", b"1,\xff\n", b"1," + b"9" * 63 + b"\r\n", b"9223372036854776,0\n"],
)
def test_tutorial_adapter_reports_the_failing_line(tmp_path, bad_row):
    path = tmp_path / "invalid.txt"
    path.write_bytes(b"0,0\n" + bad_row)
    events = assert_failed(AdapterRun(bench_source(path)), has_records=True)
    error = next(event.error for event in events if event.kind is SourceEventKind.ERROR)
    assert error is not None and "Bench log line 2" in error.message


def test_tutorial_adapter_releases_the_file_when_stopped_under_backpressure(tmp_path, monkeypatch):
    path = tmp_path / "long.txt"
    path.write_bytes(FIXTURE.read_bytes() * 20)
    open_file = Path.open
    streams = []

    def track_file(self, *args, **kwargs):
        stream = open_file(self, *args, **kwargs)
        if self == path:
            streams.append(stream)
        return stream

    monkeypatch.setattr(Path, "open", track_file)
    source = bench_source(path)
    run = AdapterRun(source, lambda: all(stream.closed for stream in streams))
    try:
        source.open()
        source.start()
        until(lambda: source.events.stats.buffered == 4, "tutorial queue did not fill")
        assert len(streams) == 1 and not streams[0].closed
        assert_cancelled(run)
    finally:
        assert_closed(run)
