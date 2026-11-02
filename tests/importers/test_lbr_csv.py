"""The lbr-csv reader matches the firmware's reference export exactly."""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

from lbr_dashboard.core.lbr_session import NODE_CLOCK, SHARED_CLOCK
from lbr_dashboard.importers import lbr_csv
from lbr_dashboard.importers.lbr_csv import (
    CSV_COLUMNS,
    LbrCsvCancelled,
    LbrCsvError,
    LbrCsvImporter,
    is_lbr_csv,
    parse_time_us,
    reset_reason_name,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "lbr" / "session.csv"
DEMO = ROOT / "sample_data" / "lbr_demo_session.csv"
HEADER = ",".join(CSV_COLUMNS)
FORMAT_ROW = "0.000000,format,,lbr_csv_version,1,,,,"


def write(tmp_path: Path, *rows: str, name: str = "log.csv") -> Path:
    path = tmp_path / name
    path.write_text("\n".join(rows) + "\n", encoding="utf-8", newline="")
    return path


def minimal(tmp_path: Path, *rows: str) -> Path:
    tail = [
        "1.000000,session,central,protocol_version,4,,,,",
        "9.000000,end,central,records_written,1,,,,",
        "9.000000,integrity,central,records,1,,,,",
    ]
    return write(tmp_path, HEADER, FORMAT_ROW, *rows, *tail)


def read(path: Path):
    return LbrCsvImporter().import_session(path)


# The firmware fixture ----------------------------------------------------------------


def test_fixture_is_the_pinned_firmware_file() -> None:
    digest = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    assert digest == "3123ec520eee382524ab2218910aa79e2705e00f3864268639f96b8087faea4e"


def test_fixture_session_metadata() -> None:
    session = read(FIXTURE)
    assert session.source_name == "session.csv" and session.csv_version == 1
    assert session.row_count == 71 and session.malformed_count == 0
    assert session.logger == "central"
    assert dict(session.session) == {
        "format_version": "1",
        "protocol_version": "4",
        "fw_version": "512",
        "catalog_hash": "2862307193",
        "config_crc": "777070580",
        "reset_reason": "5",
        "boot_count": "42",
        "start_time_us": "1500000",
    }
    assert session.config == (("node", "central"), ("node_id", "6"), ("can_bitrate", "500000"))
    assert dict(session.integrity) == {
        "records": 18,
        "corrupt_regions": 0,
        "skipped_bytes": 0,
        "truncated_tail": 9,
        "time_sync_frames": 0,
        "time_follow_up_frames": 0,
        "catalog_checked": 1,
    }


def test_fixture_warnings_surface_every_problem_in_the_log() -> None:
    warnings = read(FIXTURE).warnings
    assert len(warnings) == 4
    assert "BROWNOUT reset" in warnings[0]
    assert "truncated record (9 bytes)" in warnings[1]
    assert "dropped 5 record(s) (150 bytes)" in warnings[2]
    assert "1 sample(s) from stress" in warnings[3]


def test_every_fixture_value_and_time_matches_the_file_exactly() -> None:
    session = read(FIXTURE)
    with FIXTURE.open(newline="") as handle:
        rows = [r for r in csv.DictReader(handle) if r["kind"] in ("sample", "heartbeat", "health")]
    assert sum(channel.count for channel in session.channels) == len(rows) == 47
    for row in rows:
        whole, micros = row["t_s"].split(".")
        t_us = int(whole) * 1_000_000 + int(micros)
        clock = NODE_CLOCK if row["kind"] == "sample" and row["synced"] == "0" else SHARED_CLOCK
        matches = [
            c
            for c in session.channels
            if (c.key.kind, c.key.node, c.key.name, c.key.clock)
            == (row["kind"], row["node"], row["name"], clock)
        ]
        assert len(matches) == 1, row
        channel = matches[0]
        index = int(np.flatnonzero(channel.times_us == t_us)[0])
        assert channel.unit == row["unit"]
        if row["value"] == "":
            assert not channel.available[index] and np.isnan(channel.values[index])
        else:
            assert channel.available[index] and channel.values[index] == float(row["value"])
        assert channel.seq[index] == (int(row["seq"]) if row["seq"] else -1)


def test_fixture_specific_samples() -> None:
    session = read(FIXTURE)
    accel = session.channel("imu.imu_ch_accel_z")
    assert accel is not None and accel.unit == "m/s^2"
    assert accel.times_us.tolist() == [1_000_000] and accel.values.tolist() == [-9.80665]
    link = session.channel("central.cockpit_link")
    assert link is not None and link.seq.tolist() == [4294967295]
    cvt = session.channel("temperature.temp_cvt")
    assert cvt is not None and cvt.available_count == 0
    assert cvt.unavailable_reasons() == {"NOT_FITTED": 1}
    stress = session.channel("stress.stress_3.node-clock")
    assert stress is not None and stress.key.clock == NODE_CLOCK
    assert stress.times_us.tolist() == [20_015_998_343_868]
    assert session.channel("stress.stress_3") is None
    flags = session.channel("stress.heartbeat.err_flags")
    assert flags is not None and flags.status_labels() == {"NOT_SYNCED|SENSOR": 1}
    uptime = session.channel("central.health.uptime_ms")
    assert uptime is not None and uptime.values.tolist() == [2000.0]


def test_fixture_events() -> None:
    events = read(FIXTURE).events
    assert [(e.t_us, e.kind, e.name, e.value) for e in events] == [
        (2_500_000, "loss", "dropped_records", "5 records, 150 bytes"),
        (2_500_000, "text", "text", 'I (2500) central: fixture "text" record'),
        (4_000_000, "fault", "NODE_LOST", "5"),
    ]
    assert events[2].synced and events[2].seq == 0


def test_fixture_channels_are_grouped_by_node() -> None:
    session = read(FIXTURE)
    assert session.nodes() == (
        "central",
        "corner_br",
        "corner_fl",
        "driver_input",
        "imu",
        "stress",
        "temperature",
    )
    kinds = [c.key.kind for c in session.channels if c.key.node == "driver_input"]
    assert kinds == ["sample", "sample", "sample", "heartbeat", "heartbeat"]


# Detection and refusal ----------------------------------------------------------------


def test_detection(tmp_path: Path) -> None:
    assert is_lbr_csv(FIXTURE)
    assert not is_lbr_csv(ROOT / "sample_data" / "sample_baja_log.csv")
    assert not is_lbr_csv(tmp_path / "missing.csv")
    bom = tmp_path / "bom.csv"
    bom.write_bytes(b"\xef\xbb\xbf" + FIXTURE.read_bytes())
    assert is_lbr_csv(bom)
    assert read(bom).row_count == 71
    broken = write(tmp_path, '"unterminated', name="broken.csv")
    assert not is_lbr_csv(broken)
    empty = write(tmp_path, "", name="empty-line.csv")
    assert not is_lbr_csv(empty)


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ((), "is empty"),
        (("a,b,c", FORMAT_ROW), "not an lbr-csv export"),
        ((HEADER,), "has a header but no rows"),
        ((HEADER, "1.000000,sample,imu,X,1,,ok,1,0"), "does not start with the lbr_csv_version"),
        ((HEADER, "0.000000,format,,lbr_csv_version,2,,,,"), "version 2; this dashboard reads"),
        ((HEADER, "0.000000,format,,lbr_csv_version,,,,,"), "version ?"),
    ],
)
def test_unreadable_files_are_refused(tmp_path: Path, rows: tuple[str, ...], message: str) -> None:
    path = tmp_path / "bad.csv"
    path.write_text("\n".join(rows), encoding="utf-8")
    with pytest.raises(LbrCsvError, match=message.replace("?", r"\?")):
        read(path)


def test_os_errors_become_import_errors(tmp_path: Path) -> None:
    with pytest.raises(LbrCsvError, match="Could not read"):
        read(tmp_path)


# Row handling --------------------------------------------------------------------------


def test_malformed_rows_are_skipped_and_reported(tmp_path: Path) -> None:
    path = minimal(
        tmp_path,
        "1.000000,sample,imu,ACCEL,1.5,m/s^2,ok,1,0",
        "1.5,sample,imu,ACCEL,2,m/s^2,ok,1,1",
        "1.010000,sample,imu,ACCEL,abc,m/s^2,ok,1,2",
        "1.020000,sample,imu,,3,m/s^2,ok,1,3",
        "1.030000,sample,imu,ACCEL",
        "1.040000,format,,lbr_csv_version,1,,,,",
        "1.050000,loss,central,dropped_frames,1,,,,",
        "1.060000,integrity,central,records,many,,,,",
        "",
        "1.070000,sample,imu,ACCEL,4.25,m/s^2,ok,1,4",
    )
    session = read(path)
    accel = session.channel("imu.accel")
    assert accel is not None and accel.values.tolist() == [1.5, 4.25]
    assert session.malformed_count == 7
    assert [m.line for m in session.malformed][:2] == [4, 5]
    assert "6 decimals" in session.malformed[0].reason
    assert any("Skipped 7 malformed row(s); first at line 4" in w for w in session.warnings)


def test_csv_level_errors_are_counted(tmp_path: Path) -> None:
    huge = "x" * (csv.field_size_limit() + 10)
    path = minimal(tmp_path, f"1.000000,text,central,text,{huge},,,,")
    session = read(path)
    assert session.malformed_count == 1 and "field larger" in session.malformed[0].reason


def test_report_limit(tmp_path: Path) -> None:
    rows = [f"1.{i:06d},sample,imu,X,bad,,ok,1,{i}" for i in range(30)]
    session = read(minimal(tmp_path, *rows))
    assert session.malformed_count == 30 and len(session.malformed) == 20


def test_rows_are_sorted_per_channel_with_their_fields(tmp_path: Path) -> None:
    path = minimal(
        tmp_path,
        "2.000000,sample,imu,X,2,,ok,1,2",
        "1.000000,sample,imu,X,,,SENSOR_FAULT,1,1",
        "3.000000,sample,imu,X,3,,ok,1,3",
        "1.000000,sample,imu,X,1,,ok,1,10",
    )
    channel = read(path).channel("imu.x")
    assert channel is not None
    assert channel.times_us.tolist() == [1_000_000, 1_000_000, 2_000_000, 3_000_000]
    assert channel.seq.tolist() == [1, 10, 2, 3]
    assert channel.available.tolist() == [False, True, True, True]
    assert channel.unavailable_reasons() == {"SENSOR_FAULT": 1}


def test_unavailable_without_reason_and_real_nan(tmp_path: Path) -> None:
    path = minimal(
        tmp_path,
        "1.000000,sample,imu,X,,,,1,1",
        "1.100000,sample,imu,X,nan,,ok,1,2",
        "1.200000,health,imu,uptime_ms,,,,,",
    )
    session = read(path)
    x = session.channel("imu.x")
    assert x is not None and x.available.tolist() == [False, True]
    assert x.unavailable_reasons() == {"unavailable": 1}
    assert x.valid_values().size == 0
    health = session.channel("imu.health.uptime_ms")
    assert health is not None and health.unavailable_reasons() == {"unavailable": 1}


def test_units_fill_in_and_conflicts_are_reported(tmp_path: Path) -> None:
    path = minimal(
        tmp_path,
        "1.000000,sample,imu,X,1,,ok,1,1",
        "1.100000,sample,imu,X,2,m/s,ok,1,2",
        "1.200000,sample,imu,X,3,km/h,ok,1,3",
        "1.300000,sample,imu,X,4,,ok,1,4",
    )
    session = read(path)
    channel = session.channel("imu.x")
    assert channel is not None and channel.unit == "m/s"
    assert "Unit changed within: imu.x." in session.warnings


def test_unknown_kinds_are_ignored_with_a_note(tmp_path: Path) -> None:
    path = minimal(tmp_path, "1.000000,future_kind,central,x,1,,,,", "1.000000,,central,x,1,,,,")
    warnings = read(path).warnings
    assert "Ignored 1 row(s) of unknown kind '(empty)'." in warnings
    assert "Ignored 1 row(s) of unknown kind 'future_kind'." in warnings


def test_diagnostic_rows_and_long_text(tmp_path: Path) -> None:
    long_text = "y" * 600
    path = minimal(
        tmp_path,
        f"1.000000,text,central,text,{long_text},,,,",
        "1.100000,command,central,SET_MODE,0a0b,,,,",
        "1.200000,can_raw,central,0x7FF,deadbeef,,,,",
        "1.300000,unknown_record,central,type9,00ff,,,,",
        "1.400000,event,stress,TIME_SYNCED,0,,ok,0,3",
        "1.500000,loss,central,dropped_bytes,8,,,,",
    )
    events = read(path).events
    assert [e.kind for e in events] == [
        "text",
        "command",
        "can_raw",
        "unknown_record",
        "event",
        "loss",
    ]
    assert len(events[0].value) == 500 and events[0].value.endswith("...")
    assert not events[4].synced
    assert events[5].value == "8 bytes"


def test_diagnostic_rows_are_capped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lbr_csv, "MAX_EVENTS_PER_KIND", 2)
    rows = [f"1.{i:06d},can_raw,central,0x7FF,00,,,," for i in range(5)]
    session = read(minimal(tmp_path, *rows))
    assert len(session.events) == 2
    assert dict(session.dropped_events) == {"can_raw": 3}
    assert "Showing the first 2 of 5 'can_raw' rows." in session.warnings


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        (["1.0,session,central,protocol_version,5,,,,"], "protocol version 5"),
        (["1.000000,session,central,format_version,2,,,,"], "Log format version 2"),
        (["1.000000,session,central,reset_reason,3,,,,"], "PANIC reset"),
        (["1.000000,session,central,reset_reason,4,,,,"], "WATCHDOG reset"),
        (["9.000000,integrity,central,corrupt_regions,2,,,,"], "2 corrupt region(s)"),
        (["9.000000,integrity,central,skipped_bytes,40,,,,"], "40 bytes of the log were skipped"),
        (["9.000000,integrity,central,catalog_checked,0,,,,"], "not checked against"),
    ],
)
def test_session_and_integrity_warnings(tmp_path: Path, rows: list[str], expected: str) -> None:
    path = minimal(tmp_path, *[r.replace("1.0,", "1.000000,") for r in rows])
    assert any(expected in warning for warning in read(path).warnings)


def test_missing_session_integrity_and_end_are_reported(tmp_path: Path) -> None:
    warnings = read(write(tmp_path, HEADER, FORMAT_ROW, "1.000000,sample,imu,X,1,,ok,1,1")).warnings
    assert warnings == (
        "No session record: versions and configuration are unknown.",
        "No integrity rows at the end: the export may be incomplete.",
        "No END record: the log was not closed cleanly.",
    )


def test_clean_session_has_no_warnings(tmp_path: Path) -> None:
    assert read(minimal(tmp_path, "1.000000,sample,imu,X,1,,ok,1,1")).warnings == ()


def test_crlf_line_endings(tmp_path: Path) -> None:
    path = tmp_path / "crlf.csv"
    path.write_bytes(FIXTURE.read_bytes().replace(b"\n", b"\r\n"))
    session = read(path)
    assert session.row_count == 71 and session.malformed_count == 0
    assert session.events[1].value == 'I (2500) central: fixture "text" record'


def test_invalid_utf8_is_reported_as_a_malformed_row(tmp_path: Path) -> None:
    path = tmp_path / "bytes.csv"
    path.write_bytes(
        (HEADER + "\n" + FORMAT_ROW + "\n").encode() + b"1.000000,sample,imu,X,\xff,,ok,1,1\n"
    )
    session = read(path)
    assert session.malformed_count == 1


# Progress and cancellation --------------------------------------------------------------


def test_progress_reports_bytes_and_can_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lbr_csv, "PROGRESS_EVERY_BYTES", 200)
    calls: list[tuple[int, int]] = []
    LbrCsvImporter().import_session(DEMO, progress=lambda d, t: calls.append((d, t)) or True)
    size = DEMO.stat().st_size
    assert len(calls) > 5 and calls[-1] == (size, size)
    assert [d for d, _ in calls] == sorted(d for d, _ in calls)
    with pytest.raises(LbrCsvCancelled):
        LbrCsvImporter().import_session(FIXTURE, progress=lambda d, t: False)
    monkeypatch.setattr(lbr_csv, "PROGRESS_EVERY_BYTES", 10**9)
    with pytest.raises(LbrCsvCancelled):
        LbrCsvImporter().import_session(FIXTURE, progress=lambda d, t: d < t)


# Helpers ---------------------------------------------------------------------------------


def test_time_parsing_is_exact() -> None:
    assert parse_time_us("20015998.343868") == 20_015_998_343_868
    assert parse_time_us("0.000001") == 1
    for bad in ("1.5", "-1.000000", "1.0000000", "abc", ""):
        with pytest.raises(ValueError):
            parse_time_us(bad)


def test_reset_reason_names() -> None:
    assert reset_reason_name("5") == "BROWNOUT"
    assert reset_reason_name("0") == "UNKNOWN"
    assert reset_reason_name("42") == "code 42"
    assert reset_reason_name("") == ""


def test_status_table_overflow_keeps_counting() -> None:
    buffer = lbr_csv._ChannelBuffer("")
    buffer.statuses = {f"s{i}": i for i in range(0xFFFF)}
    assert buffer.status_index("new reason") == 0xFFFF
    assert buffer.status_index("another") == 0xFFFF
    assert "new reason" not in buffer.statuses and buffer.statuses["other"] == 0xFFFF


# Demo session -----------------------------------------------------------------------------


def load_generator():
    spec = importlib.util.spec_from_file_location(
        "make_lbr_demo_csv", ROOT / "scripts" / "make_lbr_demo_csv.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["make_lbr_demo_csv"] = module
    spec.loader.exec_module(module)
    return module


def test_demo_session_is_a_valid_export_with_realistic_problems() -> None:
    session = read(DEMO)
    assert session.malformed_count == 0
    assert len(session.warnings) == 2
    gas = session.channel("driver_input.gas_pedal")
    assert gas is not None and gas.count == 998 and gas.available_count == 998
    assert np.all(np.diff(gas.times_us) > 0)
    br = session.channel("temperature.temp_brake_br")
    assert br is not None and br.available_count == 0
    assert br.unavailable_reasons() == {"NOT_FITTED": 100}
    bl = session.channel("temperature.temp_brake_bl")
    assert bl is not None and bl.unavailable_reasons() == {"SENSOR_FAULT": 15}
    assert session.channel("stress.stress_1.node-clock") is not None
    assert [e.kind for e in session.events].count("fault") == 1
    assert any(e.kind == "text" and "not vehicle data" in e.value for e in session.events)


def test_demo_generator_reproduces_the_committed_shape(tmp_path: Path) -> None:
    generator = load_generator()
    out = tmp_path / "demo.csv"
    assert generator.main(["make", str(out)]) == 0
    fresh = out.read_text(encoding="ascii").splitlines()
    committed = DEMO.read_text(encoding="ascii").splitlines()
    assert len(fresh) == len(committed)
    assert [line.split(",")[1:4] for line in fresh] == [line.split(",")[1:4] for line in committed]
    assert generator.csv_f32(0.1) == "0.1" and generator.csv_time(1_234_567) == "1.234567"
