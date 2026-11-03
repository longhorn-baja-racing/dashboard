"""Arbitrary timestamped CSVs import through an explicit, visible mapping (#31)."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from lbr_dashboard.core.csv_mapping import (
    ISO8601,
    MAX_PROBLEMS,
    ColumnMapping,
    CsvMapping,
    SignalKind,
)
from lbr_dashboard.core.lifecycle import CancellationRequested, CancellationToken
from lbr_dashboard.importers import mapped_csv
from lbr_dashboard.importers.mapped_csv import (
    CsvMappingError,
    MappedCsvImporter,
    detect_kind,
    looks_like_time,
    parse_iso8601,
    parse_time,
    read_sample,
    split_unit,
    suggest_for_file,
    suggest_mapping,
    time_unit_from_header,
)

ROOT = Path(__file__).resolve().parents[2]
LEGACY = ROOT / "sample_data" / "sample_baja_log.csv"
MIXED = ROOT / "tests" / "fixtures" / "csv_mapping" / "mixed_rate.csv"
MS = 1_000_000


def write(tmp_path: Path, text: str, name: str = "data.csv", encoding: str = "utf-8") -> Path:
    path = tmp_path / name
    path.write_bytes(text.encode(encoding))
    return path


def mapped(path: Path, **changes: object):
    encoding = str(changes.get("encoding", "utf-8"))
    suggestion = suggest_for_file(path, encoding=encoding)
    mapping = replace(suggestion.mapping, **changes)  # type: ignore[arg-type]
    return MappedCsvImporter().import_session(path, mapping)


# Header helpers -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("speed (km/h)", ("speed", "km/h")),
        ("temp [deg C]", ("temp", "deg C")),
        ("  plain  ", ("plain", "")),
        ("(V)", ("(V)", "")),
        ("wheel_fl_rpm", ("wheel_fl_rpm", "")),  # suffixes are never read as units
    ],
)
def test_units_are_only_read_from_brackets(header: str, expected: tuple[str, str]) -> None:
    assert split_unit(header) == expected


@pytest.mark.parametrize(
    ("header", "unit"),
    [
        ("timestamp_ms", "ms"),
        ("time (s)", "s"),
        ("Time [seconds]", "s"),
        ("elapsed_us", "us"),
        ("t [" + chr(0xB5) + "s]", "us"),
        ("time ns", "ns"),
        ("time", None),
        ("time (furlongs)", None),
        ("Timestamp_ms", "ms"),
    ],
)
def test_time_units_come_from_the_header(header: str, unit: str | None) -> None:
    assert time_unit_from_header(header) == unit


@pytest.mark.parametrize(
    "header", ["t", "Time", "timestamp_ms", "Timestamp_ms", "gps_time", "time_utc", "elapsed (s)"]
)
def test_time_like_headers(header: str) -> None:
    assert looks_like_time(header)


@pytest.mark.parametrize("header", ["throttle", "timer_count", "uptime_ms", "speed"])
def test_other_headers_are_not_time(header: str) -> None:
    assert not looks_like_time(header)


def test_value_kinds() -> None:
    assert detect_kind(["1", "2.5", "", "nan", "-3e2"]) is SignalKind.NUMBER
    assert detect_kind(["true", "False", "", "1"]) is SignalKind.BOOLEAN
    assert detect_kind(["0", "1", "1"]) is SignalKind.NUMBER  # 0/1 alone stays numeric
    assert detect_kind(["on", "off", "maybe"]) is SignalKind.TEXT
    assert detect_kind(["inf", "1"]) is SignalKind.TEXT
    assert detect_kind(["1", "2", "3", "4", "x"]) is SignalKind.NUMBER  # 4 of 5 numeric
    assert detect_kind(["1", "2", "3", "x", "y"]) is SignalKind.TEXT
    assert detect_kind(["", "NA"]) is SignalKind.NUMBER


def test_times_are_exact_nanoseconds() -> None:
    assert parse_time("1.000000001", "s") == 1_000_000_001
    assert parse_time("10", "ms") == 10 * MS
    assert parse_time("-5", "us") == -5000
    assert parse_time(" 0.5 ", "ns") == 0  # rounds half to even
    assert parse_time("1e3", "ms") == 1000 * MS
    for bad in ("abc", "inf", "NaN"):
        with pytest.raises(ValueError):
            parse_time(bad, "s")


def test_iso_times_keep_fractions_and_zones() -> None:
    assert parse_iso8601("1970-01-01T00:00:01Z") == (1_000_000_000, True)
    assert parse_iso8601("1970-01-01 00:00:00.123456789") == (123_456_789, False)
    assert parse_iso8601("1970-01-01T01:00:00+01:00")[0] == 0
    assert parse_iso8601("1970-01-01T00:00:00,5-0130")[0] == 5_400_500_000_000
    assert parse_iso8601("2026-03-01T12:00:00.5z")[0] == 1_772_366_400_500_000_000
    with pytest.raises(ValueError):
        parse_iso8601("yesterday")


# Suggesting ---------------------------------------------------------------------------


def test_legacy_example_suggests_milliseconds_from_its_header() -> None:
    suggestion = suggest_for_file(LEGACY)
    mapping = suggestion.mapping
    assert suggestion.ambiguities == ()
    assert (mapping.time_column, mapping.time_unit) == (0, "ms")
    assert len(mapping.signals) == 15
    assert mapping.signals[0] == ColumnMapping(1, "wheel_fl_rpm", "", SignalKind.NUMBER)
    assert mapping.problems(read_sample(LEGACY)[0]) == []


def test_a_time_unit_is_never_guessed() -> None:
    suggestion = suggest_mapping(["time", "value"], [["0", "1"], ["10", "2"]])
    assert suggestion.mapping.time_column == 0 and suggestion.mapping.time_unit == ""
    assert suggestion.ambiguities == (
        "The unit of 'time' is not in its header. Choose s, ms, us or ns.",
    )
    assert "Choose the time unit" in suggestion.mapping.problems(["time", "value"])[0]


def test_ambiguous_and_missing_time_columns_are_listed() -> None:
    several = suggest_mapping(["t", "gps_time_s", "v"], [["1", "2", "3"]])
    assert several.mapping.time_column == 1 and several.mapping.time_unit == "s"
    assert several.ambiguities == ("Several columns look like time; using 'gps_time_s', not 't'.",)

    none = suggest_mapping(["a", "b"], [["1", "2"]])
    assert none.mapping.time_column == -1
    assert none.ambiguities[0].startswith("No column looks like time")
    assert none.mapping.problems(["a", "b"])[:2] == [
        "Choose the time column.",
        "Choose the time unit (s, ms, us, ns, iso8601); it is never guessed.",
    ]


def test_iso_columns_are_recognized_by_their_values() -> None:
    rows = [["2026-03-01T12:00:00Z", "1"], ["2026-03-01T12:00:01Z", "2"]]
    suggestion = suggest_mapping(["when", "v"], rows)
    assert (suggestion.mapping.time_column, suggestion.mapping.time_unit) == (0, ISO8601)


def test_repeated_and_empty_names_are_made_unique_and_reported() -> None:
    suggestion = suggest_mapping(["time_s", "v", "v", "", "empty"], [["0", "1", "2", "3", ""]])
    names = [signal.name for signal in suggestion.mapping.signals]
    assert names == ["v", "v_3", "column_4", "empty"]
    assert "Column 3 repeats the name 'v'; using 'v_3'." in suggestion.ambiguities
    assert "'empty' has no values in the first 1 rows." in suggestion.ambiguities


# Applying -----------------------------------------------------------------------------


def test_legacy_example_imports_with_known_times_and_values() -> None:
    session = mapped(LEGACY)
    assert session.summary() == "15 signals from 10 rows"
    assert session.time_range_ns == (0, 90 * MS)
    rpm = session.signal("engine_rpm")
    assert list(rpm.times_ns[:3]) == [0, 10 * MS, 20 * MS]
    assert list(rpm.values[:3]) == [900.0, 950.0, 1100.0]
    assert rpm.source_column == "engine_rpm" and rpm.kind is SignalKind.NUMBER
    with pytest.raises(KeyError):
        session.signal("missing")


def test_mixed_rate_fixture_reports_every_problem() -> None:
    before = hashlib.sha256(MIXED.read_bytes()).hexdigest()
    suggestion = suggest_for_file(MIXED)
    assert suggestion.mapping.time_unit == "s"
    kinds = {signal.name: (signal.unit, signal.kind) for signal in suggestion.mapping.signals}
    assert kinds == {
        "signal_a": ("V", SignalKind.NUMBER),
        "signal_b": ("", SignalKind.NUMBER),
        "slow_signal": ("degC", SignalKind.NUMBER),
        "flag": ("", SignalKind.BOOLEAN),
        "note": ("", SignalKind.TEXT),
    }
    assert suggestion.ambiguities == (
        "'signal_a' looks numeric but has 1 other values ('oops'); they will import as missing.",
    )
    session = MappedCsvImporter().import_session(MIXED, suggestion.mapping)

    assert (session.rows_read, session.rows_skipped) == (10, 1)
    assert session.out_of_order_rows == 1 and session.duplicate_times == 1
    assert session.summary() == (
        "5 signals from 10 rows, 1 row skipped, 4 problems, 1 row out of time order, "
        "1 repeated time"
    )
    # Lines are where each row starts; row 5 continues onto line 6 (quoted line break).
    assert [(p.line, p.column, p.message) for p in session.problems] == [
        (5, "signal_a [V]", "kept as missing: 'oops' is not a number"),
        (7, "time (s)", "skipped: no time"),
        (11, None, "has 4 cells but the header has 6"),
        (12, None, "has 7 cells but the header has 6"),
    ]
    assert session.time_range_ns == (0, 60 * MS)

    # Missing stays missing: the slow signal has its own, sparse timeline.
    slow = session.signal("slow_signal")
    assert list(slow.times_ns) == [0, 25 * MS, 50 * MS] and list(slow.values) == [20, 21, 22]
    assert slow.missing == 6
    a = session.signal("signal_a")
    assert a.invalid == 1 and 30 * MS not in a.times_ns
    # The late row is sorted into place.
    assert list(a.times_ns) == [0, 10 * MS, 20 * MS, 25 * MS, 40 * MS, 40 * MS, 50 * MS, 60 * MS]
    assert list(a.values) == [1.5, 1.6, 1.7, 1.8, 2.0, 2.1, 2.2, 2.3]
    b = session.signal("signal_b")
    assert b.missing == 1 and len(b) == 8  # NaN is missing, not a value
    assert list(b.values)[2:4] == [15.0, 13.0]  # the late row sorts before 0.030 s
    flag = session.signal("flag")
    assert list(flag.values) == [0, 0, 1, 1, 1, 0, 0, 1] and flag.missing == 1
    note = session.signal("note")
    assert note.values == (
        "start",
        "comma, inside",
        "late row",
        "line\nbreak",
        "repeat time",
        "extra",
    )
    assert session.notes == ("1 row was out of time order; samples are sorted by time.",)
    assert hashlib.sha256(MIXED.read_bytes()).hexdigest() == before


def test_bom_crlf_and_other_encodings(tmp_path: Path) -> None:
    text = MIXED.read_text(encoding="ascii").replace("\n", "\r\n")
    bom = write(tmp_path, chr(0xFEFF) + text, "bom.csv")
    assert mapped(bom).signals[0].source_column == "signal_a [V]"

    degree = "time_ms,temp (" + chr(0xB0) + "C)\n0,1\n"
    legacy = write(tmp_path, degree, "cp1252.csv", encoding="cp1252")
    with pytest.raises(CsvMappingError, match="not valid utf-8 text near line 1.*cp1252"):
        suggest_for_file(legacy)
    session = mapped(legacy, encoding="cp1252")
    assert session.signals[0].unit == chr(0xB0) + "C"
    session = MappedCsvImporter().import_session(
        legacy, CsvMapping(0, "ms", (ColumnMapping(1, "temp", "C"),), encoding="cp1252")
    )
    assert list(session.signals[0].values) == [1.0]


def test_semicolon_files_and_quoted_delimiters(tmp_path: Path) -> None:
    path = write(tmp_path, 't (ms);label;v\n0;"a;b";1\n5;c;2\n', "semi.csv")
    suggestion = suggest_for_file(path, delimiter=";")
    session = MappedCsvImporter().import_session(path, suggestion.mapping)
    assert session.signal("label").values == ("a;b", "c")
    assert list(session.signal("v").times_ns) == [0, 5 * MS]


def test_iso_times_without_zone_are_noted(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        "when,v\n2026-03-01T12:00:00,1\n2026-03-01T12:00:00.250,2\nbad,3\n",
    )
    suggestion = suggest_mapping(*read_sample(path))
    assert suggestion.mapping.time_unit == "" or suggestion.mapping.time_column == -1
    session = mapped(path, time_column=0, time_unit=ISO8601, signals=(ColumnMapping(1, "v"),))
    v = session.signal("v")
    assert v.times_ns[1] - v.times_ns[0] == 250 * MS
    assert session.rows_skipped == 1
    assert session.problems[0].message.startswith("skipped: 'bad' is not an ISO 8601")
    assert session.notes == ("Some 'when' values have no time zone; they are read as UTC.",)


def test_preview_reads_only_the_first_rows() -> None:
    mapping = suggest_for_file(LEGACY).mapping
    preview = MappedCsvImporter().import_session(LEGACY, mapping, max_rows=3)
    assert preview.truncated and preview.rows_read == 3
    assert len(preview.signal("engine_rpm")) == 3
    assert preview.notes == ("Preview of the first 3 rows.",)


def test_cancellation_stops_a_long_import(tmp_path: Path) -> None:
    rows = "".join(f"{i},{i}\n" for i in range(5000))
    path = write(tmp_path, "time_ms,v\n" + rows)
    token = CancellationToken()
    token.cancel()
    with pytest.raises(CancellationRequested):
        MappedCsvImporter().import_session(path, suggest_for_file(path).mapping, cancel=token)
    whole = MappedCsvImporter().import_session(
        path, suggest_for_file(path).mapping, cancel=CancellationToken()
    )
    assert whole.rows_read == 5000


def test_problem_list_is_bounded(tmp_path: Path) -> None:
    rows = "".join(f"{i},x\n" for i in range(MAX_PROBLEMS + 25))
    path = write(tmp_path, "time_ms,v\n" + rows)
    mapping = CsvMapping(0, "ms", (ColumnMapping(1, "v"),))
    session = MappedCsvImporter().import_session(path, mapping)
    assert len(session.problems) == MAX_PROBLEMS and session.problems_not_listed == 25
    assert session.signal("v").invalid == MAX_PROBLEMS + 25
    assert f"{MAX_PROBLEMS + 25} problems" in session.summary()


@pytest.mark.parametrize(
    ("mapping", "message"),
    [
        (CsvMapping(5, "ms", (ColumnMapping(1, "v"),)), "Choose the time column."),
        (CsvMapping(0, "minutes", (ColumnMapping(1, "v"),)), "Choose the time unit"),
        (CsvMapping(0, "ms", ()), "Map at least one column"),
        (CsvMapping(0, "ms", (ColumnMapping(9, "v"),)), "Column 10 does not exist."),
        (CsvMapping(0, "ms", (ColumnMapping(0, "t"),)), "'time_ms' is the time column"),
        (CsvMapping(0, "ms", (ColumnMapping(1, " "),)), "'v' needs a signal name."),
        (
            CsvMapping(0, "ms", (ColumnMapping(1, "v"), ColumnMapping(1, "v"))),
            "Two columns are named 'v'",
        ),
        (CsvMapping(0, "ms", (ColumnMapping(1, "v"),), delimiter=";;"), "one character"),
    ],
)
def test_invalid_mappings_say_what_to_change(
    tmp_path: Path, mapping: CsvMapping, message: str
) -> None:
    path = write(tmp_path, "time_ms,v\n0,1\n")
    with pytest.raises(CsvMappingError, match=message.replace("(", r"\(")):
        MappedCsvImporter().import_session(path, mapping)


def test_unreadable_files(tmp_path: Path) -> None:
    mapping = CsvMapping(0, "ms", (ColumnMapping(1, "v"),))
    with pytest.raises(CsvMappingError, match="Could not read missing.csv"):
        MappedCsvImporter().import_session(tmp_path / "missing.csv", mapping)
    empty = write(tmp_path, "\n\n", "empty.csv")
    with pytest.raises(CsvMappingError, match="empty.csv is empty"):
        MappedCsvImporter().import_session(empty, mapping)
    with pytest.raises(CsvMappingError, match="empty.csv is empty"):
        read_sample(empty)
    with pytest.raises(CsvMappingError, match="not a known text encoding"):
        MappedCsvImporter().import_session(empty, replace(mapping, encoding="klingon"))
    nul = write(tmp_path, "time_ms,v\n0,\x001\n", "nul.csv")
    session = MappedCsvImporter().import_session(nul, mapping)
    assert session.signal("v").invalid == 1


def test_csv_errors_name_the_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write(tmp_path, "time_ms,v\n0,1\n")

    def broken(*args: object, **kwargs: object):
        def rows():
            yield ["time_ms", "v"]
            raise mapped_csv.csv.Error("field larger than field limit")

        return rows()

    monkeypatch.setattr(mapped_csv.csv, "reader", broken)
    with pytest.raises(CsvMappingError, match="line 2: field larger than field limit"):
        MappedCsvImporter().import_session(path, CsvMapping(0, "ms", (ColumnMapping(1, "v"),)))
