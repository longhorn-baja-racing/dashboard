"""The picker preset preserves physical identity, exact time and missingness."""

import csv

import pytest

from lbr_dashboard.core import CancellationRequested, CancellationToken, SourceEventKind
from lbr_dashboard.core.csv_mapping import ColumnMapping, CsvMapping, SignalKind
from lbr_dashboard.importers.csv_source import prepare_csv, prepare_mapped_csv, timestamp_ns


@pytest.mark.parametrize(
    "text, expected",
    [
        ("9223372036854.775807", 2**63 - 1),
        ("-9223372036854.775808", -(2**63)),
        ("0.000001", 1),
        ("-0.000001", -1),
        ("1.000001000000000000000000000000", 1000001),
    ],
)
def test_timestamp_conversion_is_exact(text, expected):
    assert timestamp_ns(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "1e999999999",
        "1e-999999999",
        "1" * 129,
        "nan",
        "sNaN",
        "9223372036854.775808",
        "-9223372036854.775809",
        "1.0000010000000000000000000000000000001",
    ],
)
def test_timestamp_precision_cannot_be_rounded_away(text):
    with pytest.raises(ValueError):
        timestamp_ns(text)


def test_csv_source_values_times_duplicates_and_cleanup(tmp_path):
    path = tmp_path / "readings 測定.csv"
    path.write_text(
        "timestamp_ms,value,value,note\n-0.000001,0,7,ready\n10,,8,\n5,3,9,end\n", encoding="utf-8"
    )
    original = path.read_bytes()
    prepared = prepare_csv(path, CancellationToken())
    assert [d.identifier for d in prepared.schema.signals] == [
        "csv.column.1",
        "csv.column.2",
        "csv.column.3",
    ]
    assert [d.data_type for d in prepared.schema.signals] == ["numeric", "numeric", "string"]
    assert [s.timestamp_ns for _, s in prepared.records[:3]] == [-1, 10000000, 5000000]
    assert [s.value for _, s in prepared.records[:3]] == [0, None, 3]
    assert not prepared.records[1][1].available
    source = prepared.source()
    source.open()
    source.start()
    assert source.join(2)
    events = source.events.drain()
    assert len([e for e in events if e.kind is SourceEventKind.RECORD]) == 9
    assert source.close(0) and source.close(0)
    assert path.read_bytes() == original
    path.unlink()  # Closed input handle, including on Windows.


@pytest.mark.parametrize(
    "content, message",
    [
        ("", "no readings"),
        ("timestamp_ms,a\n", "no readings"),
        ("time,a\n0,1\n", "exactly one timestamp_ms"),
        ("timestamp_ms,timestamp_ms,a\n0,0,1\n", "exactly one timestamp_ms"),
        ("timestamp_ms\n0\n", "1-64"),
        ("timestamp_ms,a\n0\n", "expected 2 fields"),
        ("timestamp_ms,a\nnan,1\n", "timestamp_ms must be finite"),
        ("timestamp_ms,a\nInfinity,1\n", "timestamp_ms must be finite"),
        ("timestamp_ms,a\n1e900,1\n", "timestamp_ms must be finite"),
        ("timestamp_ms,a\n0.0000001,1\n", "timestamp_ms must be finite"),
        ("timestamp_ms,a\nbad,1\n", "timestamp_ms must be finite"),
        ("timestamp_ms,a\n0,nan\n", "non-finite reading"),
        ("timestamp_ms,a\n0,1e900\n", "non-finite reading"),
        ('timestamp_ms,a\n0,"unclosed\n', "unexpected end"),
    ],
)
def test_csv_rejects_bad_input(tmp_path, content, message):
    path = tmp_path / "bad.csv"
    path.write_text(content, encoding="utf-8")
    with pytest.raises((ValueError, csv.Error), match=message):
        prepare_csv(path, CancellationToken())
    path.unlink()


def test_csv_filesystem_and_encoding_failures(tmp_path):
    path = tmp_path / "bad.csv"
    with pytest.raises(FileNotFoundError):
        prepare_csv(path, CancellationToken())
    path.write_bytes(b"timestamp_ms,a\n0,\xff\n")
    with pytest.raises(UnicodeDecodeError):
        prepare_csv(path, CancellationToken())


@pytest.mark.parametrize(
    "limit, value, content, message",
    [
        ("MAX_BYTES", 1, "timestamp_ms,a\n0,1\n", "16 MiB"),
        ("MAX_ROWS", 1, "timestamp_ms,a\n0,1\n1,2\n", "20,000"),
        ("MAX_SIGNALS", 1, "timestamp_ms,a,b\n0,1,2\n", "1-64"),
        ("MAX_SAMPLES", 1, "timestamp_ms,a\n0,1\n1,2\n", "200,000"),
    ],
)
def test_bounded_csv_import(tmp_path, monkeypatch, limit, value, content, message):
    monkeypatch.setattr("lbr_dashboard.importers.csv_source." + limit, value)
    path = tmp_path / "bounded.csv"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        prepare_csv(path, CancellationToken())


def test_csv_cancelled_before_and_during_read(tmp_path):
    path = tmp_path / "data.csv"
    path.write_text("timestamp_ms,a\n0,1\n1,2\n", encoding="utf-8")
    token = CancellationToken()
    token.cancel()
    with pytest.raises(CancellationRequested):
        prepare_csv(path, token)

    class CancelDuringRead(CancellationToken):
        calls = 0

        def raise_if_cancelled(self):
            self.calls += 1
            if self.calls == 3:
                self.cancel()
            super().raise_if_cancelled()

    with pytest.raises(CancellationRequested):
        prepare_csv(path, CancelDuringRead())
    path.unlink()


def mapped(*signals: ColumnMapping, unit: str = "s") -> CsvMapping:
    return CsvMapping(0, unit, signals)


def test_mapped_csv_becomes_typed_sparse_signals(tmp_path):
    path = tmp_path / "mapped.csv"
    path.write_text("t,a,flag\n0.5,1,yes\n0.25,,no\nx,3,on\n", encoding="utf-8")
    prepared = prepare_mapped_csv(
        path,
        mapped(ColumnMapping(1, "a", "V"), ColumnMapping(2, "Flag", kind=SignalKind.BOOLEAN)),
        CancellationToken(),
    )
    a, flag = prepared.schema.signals
    assert (a.identifier, a.display_name, a.canonical_unit, a.data_type) == (
        "csv.column.1",
        "a",
        "V",
        "numeric",
    )
    assert (flag.identifier, flag.data_type, flag.canonical_unit) == (
        "csv.column.2",
        "boolean",
        None,
    )
    values = [(i, s.timestamp_ns, s.value) for i, s in prepared.records]
    assert values == [
        ("csv.column.1", 500_000_000, 1.0),
        ("csv.column.2", 250_000_000, False),
        ("csv.column.2", 500_000_000, True),
    ]
    assert prepared.summary == (
        "Imported 2 signals from 3 rows, 1 row skipped, 1 problem, 1 row out of time order"
    )
    source = prepared.source()
    assert getattr(source, "import_summary") == prepared.summary


def test_mapped_csv_without_readings_or_over_limits_is_refused(tmp_path, monkeypatch):
    path = tmp_path / "mapped.csv"
    path.write_text("t,a\nx,1\n,2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="No readings with this mapping"):
        prepare_mapped_csv(path, mapped(ColumnMapping(1, "a")), CancellationToken())
    path.write_text("t,a\n1,1\n2,2\n", encoding="utf-8")
    monkeypatch.setattr("lbr_dashboard.importers.csv_source.MAX_SAMPLES", 1)
    with pytest.raises(ValueError, match="at most 200,000 readings"):
        prepare_mapped_csv(path, mapped(ColumnMapping(1, "a")), CancellationToken())


def test_mapped_csv_cancelled(tmp_path):
    path = tmp_path / "mapped.csv"
    path.write_text("t,a\n1,1\n", encoding="utf-8")
    token = CancellationToken()
    token.cancel()
    with pytest.raises(CancellationRequested):
        prepare_mapped_csv(path, mapped(ColumnMapping(1, "a")), token)
