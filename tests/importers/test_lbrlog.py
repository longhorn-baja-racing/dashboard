"""The .lbrlog reader matches the firmware's golden fixtures and survives damage.

Expectations come from the firmware's own files: ``session.json`` and
``can_capture.json`` (the firmware codecs' decode) and ``session.csv`` (the
reference decoder's export of the same log). Damaged logs are built from the
golden bytes or with the small record writer below, which follows
``docs/protocol.md`` directly.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import random
import struct
import sys
import time
import zlib
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from lbr_dashboard.core import CancellationRequested, CancellationToken
from lbr_dashboard.core.lbr_session import NODE_CLOCK, SHARED_CLOCK
from lbr_dashboard.importers import lbrlog
from lbr_dashboard.importers.lbr_catalog_data import CATALOG_JSON
from lbr_dashboard.importers.lbrlog import (
    Catalog,
    ChannelKey,
    LbrLog,
    LbrlogError,
    catalog_hash,
    read_lbrlog,
    read_lbrlog_stream,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "lbr"
#: Binary fixtures are stored as hex text (see tests/fixtures/lbr/README.md).
GOLDEN_BYTES = bytes.fromhex((FIXTURES / "session.lbr.hex").read_text())
CATALOG_HASH = 580578432
#: Offsets of the records in session.lbr, from session.json.
OFFSETS = [r["offset"] for r in json.loads((FIXTURES / "session.json").read_text())["records"]]
END_OF_RECORDS = 688  # session.json: END at 666, 22 bytes; 9 truncated bytes follow


# -- record writer (docs/protocol.md, "Log format") ---------------------------


def record(rtype: int, payload: bytes, flags: int = 0) -> bytes:
    header = struct.pack("<BBH", rtype, flags, len(payload))
    return b"\xa5\x5a" + header + payload + struct.pack("<I", zlib.crc32(header + payload))


def session(
    *,
    fmt: int = 1,
    proto: int = 4,
    catalog_hash: int = CATALOG_HASH,
    node: int = 6,
    start_us: int = 1_000,
    config: bytes = b"node=central\n",
) -> bytes:
    fixed = struct.pack(
        "<HHIIIBBHIQ", fmt, proto, 512, catalog_hash, zlib.crc32(config), node, 1, 0, 7, start_us
    )
    return record(0x01, fixed + config)


def sample(
    signal: int,
    source: int,
    type_index: int,
    bits: int,
    t_us: int,
    *,
    valid: bool = True,
    synced: bool = True,
    seq: int = 0,
) -> bytes:
    flags = type_index | (0x10 if valid else 0) | (0x20 if synced else 0)
    return record(0x02, struct.pack("<HBBIQI", signal, source, flags, seq, t_us, bits))


def can(t_us: int, can_id: int, data: bytes, flags: int = 0) -> bytes:
    payload = struct.pack("<QIBB", t_us, can_id, flags, len(data)) + data.ljust(8, b"\0")
    return record(0x04, payload)


def end(t_us: int, written: int) -> bytes:
    return record(0x07, struct.pack("<QI", t_us, written))


def f32_bits(value: float) -> int:
    return struct.unpack("<I", struct.pack("<f", value))[0]


def f32(value: float) -> float:
    return struct.unpack("<f", struct.pack("<f", value))[0]


def read(data: bytes, **kwargs: Any) -> LbrLog:
    return read_lbrlog_stream(io.BytesIO(data), total_bytes=len(data), **kwargs)


def rows(log: LbrLog) -> list[tuple]:
    """Every sample as (key, t_us, value or None, status, text, seq) in log order."""

    out = []
    for channel in log.channels:
        for i in range(channel.count):
            text = channel.texts[i] if channel.texts else None
            usable = channel.available[i] and channel.value_type != "text"
            value = float(channel.values[i]) if usable else None
            out.append(
                (
                    int(channel.order[i]),
                    channel.key,
                    int(channel.times_us[i]),
                    value,
                    channel.statuses[int(channel.status[i])],
                    text,
                    int(channel.seq[i]),
                )
            )
    return [row[1:] for row in sorted(out, key=lambda row: row[0])]


def accounted(log: LbrLog) -> int:
    integrity = log.integrity
    return integrity.record_bytes + integrity.skipped_bytes + integrity.truncated_tail


# -- golden fixtures -----------------------------------------------------------


def csv_expectations() -> tuple[list[tuple], dict[str, str], dict[str, str], dict[str, str]]:
    """The reference decoder's export as reader rows, plus session/end/integrity rows."""

    expected = []
    session_rows: dict[str, str] = {}
    integrity: dict[str, str] = {}
    other: dict[str, str] = {}
    with (FIXTURES / "session.csv").open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            whole, _, micros = row["t_s"].partition(".")
            t_us = int(whole) * 1_000_000 + int(micros)
            kind, node, name, value = row["kind"], row["node"], row["name"], row["value"]
            clock = NODE_CLOCK if row["synced"] == "0" else SHARED_CLOCK
            seq = int(row["seq"]) if row["seq"] else -1
            if kind == "sample":
                number = float(value) if value else None
                expected.append(
                    (
                        ChannelKey(node, "sample", name, clock),
                        t_us,
                        number,
                        row["status"],
                        None,
                        seq,
                    )
                )
            elif kind in ("event", "fault"):
                key = ChannelKey(node, "sample", kind.upper(), clock)
                expected.append((key, t_us, float(value), row["status"], name, seq))
            elif kind in ("heartbeat", "health", "loss"):
                status = row["status"] if name == "err_flags" else "ok"
                expected.append(
                    (ChannelKey(node, kind, name), t_us, float(value), status, None, -1)
                )
            elif kind == "text":
                expected.append((ChannelKey(node, "text", ""), t_us, None, "ok", value, -1))
            elif kind == "session":
                session_rows[name] = value
            elif kind == "integrity":
                integrity[name] = value
            else:
                other[f"{kind}.{name}"] = value
    return expected, session_rows, integrity, other


def golden_file(tmp_path: Path) -> Path:
    path = tmp_path / "session.lbr"
    path.write_bytes(GOLDEN_BYTES)
    return path


def test_golden_fixture_is_the_pinned_firmware_file() -> None:
    digest = hashlib.sha256(GOLDEN_BYTES).hexdigest()
    assert digest == "29933a8e14328389054d2cb72f7e0237ce9c87e58dce9ca04ac114e62d694a27"
    capture = bytes.fromhex((FIXTURES / "can_capture.bin.hex").read_text())
    assert hashlib.sha256(capture).hexdigest() == (
        "d3f7f88ff452a93054e9064ae539d9d5c5a290c2c5feca8c3d12abe144a78fff"
    )


def test_golden_log_matches_the_reference_csv_export_value_for_value(
    tmp_path: Path,
) -> None:
    log = read_lbrlog(golden_file(tmp_path))
    expected, session_rows, integrity, other = csv_expectations()
    actual = rows(log)
    assert len(actual) == len(expected) == 51
    for got, want in zip(actual, expected):
        key, t_us, value, status, text, seq = got
        assert (key, t_us, status, seq) == (want[0], want[1], want[3], want[5])
        if want[4] is not None:
            assert text == want[4]
        if want[2] is None:
            assert value is None
        else:
            # The CSV prints the shortest decimal that reads back as the same f32.
            assert value in (want[2], f32(want[2])), (key, value, want[2])
    s = log.session
    assert s is not None
    assert {k: str(getattr(s, k)) for k in session_rows} == session_rows
    assert other["format.lbr_csv_version"] == "1"
    assert other["config.node"] == "central" and other["config.can_bitrate"] == "500000"
    assert dict(s.config) == {"node": "central", "node_id": "6", "can_bitrate": "500000"}
    assert int(other["end.records_written"]) == log.integrity.records_written == 18
    i = log.integrity
    assert {
        "records": i.records,
        "corrupt_regions": i.corrupt_regions,
        "skipped_bytes": i.skipped_bytes,
        "truncated_tail": i.truncated_tail,
        "time_sync_frames": i.time_sync_frames,
        "time_follow_up_frames": i.time_follow_up_frames,
        "update_data_frames": i.update_frames,
        "catalog_checked": int(i.catalog_checked),
    } == {k: int(v) for k, v in integrity.items()}
    assert i.truncated_offset == END_OF_RECORDS
    assert i.file_size == len(GOLDEN_BYTES) == accounted(log)
    assert i.sha256 == hashlib.sha256(GOLDEN_BYTES).hexdigest()


def test_golden_log_matches_the_firmware_decode_bit_for_bit(tmp_path: Path) -> None:
    log = read_lbrlog(golden_file(tmp_path))
    expected = json.loads((FIXTURES / "session.json").read_text())
    samples = [r["sample"] for r in expected["records"] if r["type"] == "SAMPLE"]
    samples += [
        s
        for r in expected["records"]
        if r["type"] == "CAN_FRAME"
        for s in r["frame"].get("samples", [])
    ]
    catalog = json.loads(CATALOG_JSON)
    nodes = {n["id"]: n["name"] for n in catalog["nodes"]}
    for want in samples:
        clock = SHARED_CLOCK if want["synced"] else NODE_CLOCK
        channel = log.channel(nodes[want["source"]], "sample", want["name"], clock)
        index = int(np.flatnonzero(channel.times_us == want["t_us"])[0])
        assert int(channel.seq[index]) == want["seq"]
        assert bool(channel.available[index]) == want["valid"]
        bits = int(want["value_bits"], 16)
        if not want["valid"]:
            assert channel.statuses[int(channel.status[index])] == want["reason"]
            assert np.isnan(channel.values[index])
        elif "event" in want:
            assert channel.texts[index] == want["event"]
            assert channel.values[index] == want["event_arg"]
        elif want["type"] == "f32":
            assert f32_bits(float(channel.values[index])) == bits
        elif want["type"] == "i32":
            assert channel.values[index] == struct.unpack("<i", struct.pack("<I", bits))[0]
        else:
            assert channel.values[index] == bits
    health = next(r["health"] for r in expected["records"] if r["type"] == "HEALTH")
    for name, value in health.items():
        if name != "node_id":
            assert log.channel("central", "health", name).values.tolist() == [value]
    unsynced = log.channel("stress", "sample", "STRESS_3", NODE_CLOCK)
    assert unsynced.times_us.tolist() == [20015998343868]
    assert expected["truncated_tail"] == log.integrity.truncated_tail == 9


def test_can_capture_decodes_like_the_firmware() -> None:
    capture = bytes.fromhex((FIXTURES / "can_capture.bin.hex").read_text())
    frames = [capture[i : i + 22] for i in range(0, len(capture), 22)]
    log = read(session() + b"".join(record(0x04, frame) for frame in frames))
    expected = json.loads((FIXTURES / "can_capture.json").read_text())["frames"]
    assert len(expected) == len(frames) == 27
    catalog = json.loads(CATALOG_JSON)
    nodes = {n["id"]: n["name"] for n in catalog["nodes"]}
    err_flags = catalog["heartbeat_err_flags"]
    seen: dict[ChannelKey, int] = {}

    def next_index(key: ChannelKey) -> int:
        seen[key] = seen.get(key, -1) + 1
        return seen[key]

    for frame in expected:
        for want in frame.get("samples", []):
            clock = SHARED_CLOCK if want["synced"] else NODE_CLOCK
            key = ChannelKey(nodes[want["source"]], "sample", want["name"], clock)
            channel = log.channel(*_fields(key))
            i = next_index(key)
            assert int(channel.times_us[i]) == want["t_us"]
            assert int(channel.seq[i]) == want["seq"]
            assert bool(channel.available[i]) == want["valid"]
            bits = int(want["value_bits"], 16)
            if not want["valid"]:
                assert channel.statuses[int(channel.status[i])] == want["reason"]
            elif "event" in want:
                assert (channel.texts[i], channel.values[i]) == (want["event"], want["event_arg"])
            elif want["type"] == "f32":
                assert f32_bits(float(channel.values[i])) == bits
            elif want["type"] == "i32":
                assert channel.values[i] == struct.unpack("<i", struct.pack("<I", bits))[0]
            else:
                assert channel.values[i] == bits
        if "heartbeat" in frame:
            node = nodes[frame["sender"]]
            state = log.channel(node, "heartbeat", "state")
            flags = log.channel(node, "heartbeat", "err_flags")
            assert state.values.tolist() == [frame["heartbeat"]["state"]]
            assert flags.values.tolist() == [frame["heartbeat"]["err_flags"]]
            names = [k for k, bit in err_flags.items() if frame["heartbeat"]["err_flags"] & bit]
            assert flags.statuses[int(flags.status[0])] == ("|".join(names) or "ok")
        if "health_page" in frame:
            uptime = log.channel(nodes[frame["sender"]], "health", "uptime_ms")
            data = bytes.fromhex(frame["data"])
            assert uptime.values.tolist() == [struct.unpack_from("<I", data)[0]]
            assert uptime.times_us.tolist() == [frame["t_us"]]
    command = log.channel("central", "command", "")
    assert command.texts == ("SESSION_START 2a000000000000",)
    raw = log.channel("node0", "can_raw", "")
    assert raw.texts == (f"0x{expected[-1]['id']:08X} {expected[-1]['data']}",)
    integrity = log.integrity
    assert (integrity.time_sync_frames, integrity.time_follow_up_frames) == (1, 1)
    assert integrity.update_frames == 4
    assert integrity.rejected == integrity.corrupt_regions == 0


def _fields(key: ChannelKey) -> tuple[str, str, str, str]:
    return key.node, key.kind, key.name, key.clock


def test_bundled_catalog_is_the_firmware_catalog() -> None:
    assert CATALOG_JSON == (FIXTURES / "catalog.json").read_text(encoding="ascii")
    catalog = Catalog.bundled()
    assert catalog.hash == catalog.computed_hash() == CATALOG_HASH


def test_reading_never_changes_the_file(tmp_path: Path) -> None:
    damaged = bytearray(GOLDEN_BYTES)
    damaged[120] ^= 0xFF
    for name, data in (("golden.lbrlog", GOLDEN_BYTES), ("damaged.lbrlog", bytes(damaged))):
        path = tmp_path / name
        path.write_bytes(data)
        path.chmod(0o444)
        before = (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_size)
        log = read_lbrlog(path)
        assert log.integrity.sha256 == hashlib.sha256(data).hexdigest()
        assert (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_size) == before


# -- damage and recovery ---------------------------------------------------------


def test_bit_flip_skips_only_the_damaged_record() -> None:
    damaged = bytearray(GOLDEN_BYTES)
    damaged[OFFSETS[2] + 10] ^= 0x04  # inside the GPS_LATITUDE sample record
    log = read(bytes(damaged))
    integrity = log.integrity
    assert integrity.corrupt_regions == 1
    assert integrity.skipped_bytes == OFFSETS[3] - OFFSETS[2] == 30
    assert [(r.offset, r.length) for r in integrity.regions] == [(OFFSETS[2], 30)]
    assert integrity.records == 17
    keys = {c.key for c in log.channels}
    assert ChannelKey("central", "sample", "GPS_LATITUDE") not in keys
    golden = rows(read(GOLDEN_BYTES))
    assert rows(log) == [row for row in golden if row[0].name != "GPS_LATITUDE"]
    assert (
        "skipped 30 corrupt bytes in 1 region(s) at offset 114 (30 bytes)"
        in (integrity.problems()[0])
    )
    assert accounted(log) == len(damaged)


def test_garbage_between_records_is_skipped_and_located() -> None:
    garbage = b"\x00noise\xa5\x5a\x02\x00\xff\xffmore noise\xa5"
    at = OFFSETS[5]
    data = GOLDEN_BYTES[:at] + garbage + GOLDEN_BYTES[at:]
    log = read(data)
    assert [(r.offset, r.length) for r in log.integrity.regions] == [(at, len(garbage))]
    assert rows(log) == rows(read(GOLDEN_BYTES))
    assert accounted(log) == len(data)


def test_garbage_before_the_session_record_still_checks_the_catalog() -> None:
    data = b"\xa5\xa5\x5a\x01garbage" + GOLDEN_BYTES
    log = read(data)
    assert log.integrity.catalog_checked and log.session is not None
    assert log.integrity.regions[0].offset == 0
    assert rows(log) == rows(read(GOLDEN_BYTES))


def test_damaged_session_record_decodes_with_an_unchecked_catalog() -> None:
    damaged = bytearray(GOLDEN_BYTES)
    damaged[20] ^= 0x01
    log = read(bytes(damaged))
    assert log.session is None and not log.integrity.catalog_checked
    assert any("was not checked" in line for line in log.integrity.problems())
    assert log.channel("driver_input", "sample", "BRAKE_PEDAL").values.tolist() == [12.5]


def test_nonzero_record_flags_are_not_a_record() -> None:
    bad = bytearray(sample(0x0600, 6, 2, 5, 2_000))
    bad[3] = 1  # flags must be 0; the CRC covers them, so recompute it
    bad[-4:] = struct.pack("<I", zlib.crc32(bytes(bad[2:-4])))
    log = read(session() + bytes(bad) + end(3_000, 2))
    assert log.integrity.corrupt_regions == 1 and log.integrity.skipped_bytes == len(bad)


@pytest.mark.parametrize("cut", [1, 5, 9, 600, 300, END_OF_RECORDS])
def test_truncation_reports_the_incomplete_tail(cut: int) -> None:
    data = GOLDEN_BYTES[: len(GOLDEN_BYTES) - cut] if cut < 100 else GOLDEN_BYTES[:cut]
    log = read(data)
    boundaries = [*OFFSETS, END_OF_RECORDS]
    last = max(offset for offset in boundaries if offset <= len(data))
    assert log.integrity.truncated_tail == len(data) - last
    assert log.integrity.truncated_offset == (last if len(data) > last else None)
    assert log.integrity.corrupt_regions == 0
    assert log.integrity.records == sum(1 for e in boundaries[1:] if e <= len(data))
    assert accounted(log) == len(data)


def test_truncated_tail_followed_by_nothing_valid_is_not_a_corrupt_region() -> None:
    data = GOLDEN_BYTES[:END_OF_RECORDS] + b"\xa5\x5a\x02\x00\x14\x00" + b"\x01" * 10
    log = read(data)
    assert log.integrity.truncated_tail == 16 and log.integrity.corrupt_regions == 0


def test_random_damage_never_fails_and_accounts_for_every_byte() -> None:
    generator = random.Random(30)
    for _ in range(200):
        data = bytearray(GOLDEN_BYTES)
        for _ in range(generator.randint(1, 6)):
            position = generator.randrange(len(data))
            action = generator.random()
            if action < 0.5:
                data[position] ^= 1 << generator.randrange(8)
            elif action < 0.75:
                data[position:position] = bytes(generator.randrange(256) for _ in range(7))
            else:
                del data[position : position + generator.randint(1, 40)]
        try:
            log = read(bytes(data))
        except LbrlogError as error:
            assert "No valid .lbrlog record" in str(error)
            continue
        assert accounted(log) == len(data)
        for channel in log.channels:
            assert channel.count == channel.times_us.size == channel.values.size


def test_large_chunked_read_matches_and_damage_spans_chunks() -> None:
    frames = [session(start_us=0)]
    for k in range(3000):
        data = struct.pack("<hhhH", k % 500, -k % 700, 3, k & 0xFFFF)
        frames.append(can(k * 10_000 + 450, 0x100, data))
    blob = b"".join(frames)
    whole = read(blob)
    chunked = read(blob, chunk_size=lbrlog.MAX_RECORD)
    assert rows(whole) == rows(chunked)
    damaged = bytearray(blob)
    damaged[lbrlog.MAX_RECORD - 3 : lbrlog.MAX_RECORD + 50] = b"\xa5" * 53
    log = read(bytes(damaged), chunk_size=lbrlog.MAX_RECORD)
    assert log.integrity.corrupt_regions == 1
    assert accounted(log) == len(damaged)


# -- versions, catalogs and refusals --------------------------------------------


def test_unknown_log_format_version_is_refused_clearly() -> None:
    with pytest.raises(LbrlogError, match="format version 2; this dashboard reads version 1"):
        read(session(fmt=2) + end(1, 1))


def test_unknown_protocol_version_is_refused_clearly() -> None:
    with pytest.raises(LbrlogError, match="protocol version 5; this dashboard reads protocol"):
        read(session(proto=5) + end(1, 1))


def test_later_session_record_with_another_version_is_refused() -> None:
    with pytest.raises(LbrlogError, match="format version 3"):
        read(GOLDEN_BYTES[:END_OF_RECORDS] + session(fmt=3))


def test_unknown_catalog_is_refused_with_a_way_out() -> None:
    with pytest.raises(LbrlogError, match="catalog.json of the firmware that wrote it"):
        read(session(catalog_hash=1234) + end(1, 1))


def test_session_switching_catalog_mid_log_is_refused() -> None:
    with pytest.raises(LbrlogError, match="names catalog 99"):
        read(GOLDEN_BYTES[:END_OF_RECORDS] + session(catalog_hash=99))


def test_matching_catalog_next_to_the_log_is_used(tmp_path: Path) -> None:
    raw = json.loads(CATALOG_JSON)
    raw["signals"][0]["name"] = "BRAKE_PRESSURE"  # renames signal 0x100
    raw["catalog_hash"] = catalog_hash(raw)
    (tmp_path / "catalog.json").write_text(json.dumps(raw), encoding="utf-8")
    log_path = tmp_path / "00000012.lbrlog"
    data = struct.pack("<hhhH", 1250, 0, 0, 0)
    heartbeat = can(300, 0x703, b"\x01\x00")
    log_path.write_bytes(
        session(catalog_hash=raw["catalog_hash"]) + heartbeat + can(450, 0x100, data)
    )
    log = read_lbrlog(log_path)
    assert log.integrity.catalog_origin == str(tmp_path / "catalog.json")
    assert log.channel("driver_input", "sample", "BRAKE_PRESSURE").values.tolist() == [12.5]
    # The bundled catalog still applies to logs written with it.
    golden = tmp_path / "golden.lbrlog"
    golden.write_bytes(GOLDEN_BYTES)
    assert read_lbrlog(golden).integrity.catalog_origin == "bundled catalog"


def test_broken_catalog_files_are_ignored_or_refused(tmp_path: Path) -> None:
    (tmp_path / "catalog.json").write_text("{not json", encoding="utf-8")
    path = tmp_path / "log.lbrlog"
    path.write_bytes(GOLDEN_BYTES)
    assert read_lbrlog(path).integrity.catalog_origin == "bundled catalog"
    raw = json.loads(CATALOG_JSON)
    raw["catalog_hash"] += 1
    with pytest.raises(LbrlogError, match="does not match its own catalog_hash"):
        Catalog(raw, "edited")
    with pytest.raises(LbrlogError, match="not a usable firmware catalog"):
        Catalog({"catalog_hash": 1}, "partial")
    with pytest.raises(LbrlogError, match="cannot read catalog"):
        Catalog.from_file(tmp_path / "catalog.json")
    (tmp_path / "list.json").write_text("[]", encoding="utf-8")
    with pytest.raises(LbrlogError, match="not a usable firmware catalog"):
        Catalog.from_file(tmp_path / "list.json")


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"", "The file is empty"),
        (b"timestamp_ms,value\n0,1\n", "No valid .lbrlog record"),
        (GOLDEN_BYTES[:5], "No valid .lbrlog record"),
    ],
)
def test_files_that_are_not_logs_are_refused(data: bytes, message: str) -> None:
    with pytest.raises(LbrlogError, match=message):
        read(data)


def test_missing_file_is_reported(tmp_path: Path) -> None:
    with pytest.raises(LbrlogError, match="Cannot read missing.lbrlog"):
        read_lbrlog(tmp_path / "missing.lbrlog", catalogs=(Catalog.bundled(),))


# -- record kinds ------------------------------------------------------------------


def test_outputs_ticks_unknown_and_rejected_records() -> None:
    key = record(0x20, struct.pack("<HBB", 3, 1, 6) + b"CVT/Ratio")
    flag = record(0x20, struct.pack("<HBB", 4, 4, 0) + b"Alarm/CvtHot")
    values = [
        record(0x21, struct.pack("<QHBI", 10_000, 3, 0, f32_bits(2.5))),
        record(0x21, struct.pack("<QHBI", 20_000, 3, 9, 0)),  # NODE_LOST
        record(0x21, struct.pack("<QHBI", 30_000, 4, 0, 1)),
        record(0x21, struct.pack("<QHBI", 40_000, 7, 0, f32_bits(1.0))),  # no KEY yet
        record(0x21, struct.pack("<QHBI", 50_000, 3, 42, 0)),
    ]
    tick = record(0x22, struct.pack("<QBBH", 10_000, 1, 0, 0))
    unknown = record(0x30, b"future")
    bad_flags = record(0x02, struct.pack("<HBBIQI", 0x0600, 6, 0x82, 0, 60_000, 5))
    short = record(0x05, b"\x00" * 4)
    health = bytearray(97)
    health[1 + 8 * 3 + 2] = 1  # reserved field of page 3
    bad_health = record(0x03, bytes(health))
    bad_dlc = record(0x04, struct.pack("<QIBB", 1, 0x100, 0, 9) + b"\0" * 8)
    data = b"".join(
        [session(), key, flag, *values, tick, unknown, bad_flags, short, bad_health, bad_dlc]
    )
    log = read(data)
    ratio = log.channel("central", "output", "CVT/Ratio")
    assert ratio.unit == "%" and ratio.value_type == "f32"
    assert ratio.times_us.tolist() == [10_000, 20_000, 50_000]
    assert ratio.values[0] == 2.5 and not ratio.available[1]
    assert [ratio.statuses[i] for i in ratio.status] == ["ok", "NODE_LOST", "status42"]
    assert log.channel("central", "output", "Alarm/CvtHot").values.tolist() == [1.0]
    assert log.channel("central", "output", "key7").values.tolist() == [1.0]
    integrity = log.integrity
    assert integrity.tick_records == 1 and dict(integrity.unknown_records) == {0x30: 1}
    assert integrity.rejected == 4
    reasons = [(r.record_type, r.reason) for r in integrity.rejected_records]
    assert reasons == [
        (0x02, "bad sample flags 0x82"),
        (0x05, "payload is 4 bytes, expected 16"),
        (0x03, "reserved field of health page 3 is set"),
        (0x04, "bad CAN dlc 9"),
    ]
    offset = data.index(bad_flags)
    assert integrity.rejected_records[0].offset == offset
    problems = " ".join(integrity.problems())
    assert f"SAMPLE at {offset}: bad sample flags 0x82" in problems
    assert "and 1 more" in problems
    assert "no END record" in problems and "type 48: 1" in problems
    assert integrity.damaged


def test_unavailable_nan_unknown_signal_and_type_conflicts() -> None:
    nan = f32_bits(float("nan"))
    data = b"".join(
        [
            session(),
            sample(0x0504, 4, 1, 3, 1_000, valid=False),  # TEMP_CVT, SENSOR_FAULT
            sample(0x0504, 4, 1, 99, 2_000, valid=False),  # unknown reason code
            sample(0x0504, 4, 1, nan, 3_000),
            sample(0x0504, 4, 4, 1, 4_000),  # bool on an f32 channel
            sample(0x7F01, 9, 6, 0xDEADBEEF, 5_000, seq=11),  # unknown signal: kept raw
            sample(0x0700, 6, 5, 0x0003_0063, 6_000),  # EVENT with an unknown code
            sample(0x0700, 6, 5, 2, 7_000, valid=False),  # unavailable EVENT
            sample(0x0611, 6, 4, 5, 8_000),  # bool: any nonzero is true
            sample(0x0600, 6, 2, 1, (1 << 63) // 1000 + 1),  # beyond int64 nanoseconds
        ]
    )
    log = read(data)
    temp = log.channel("temperature", "sample", "TEMP_CVT")
    assert [temp.statuses[i] for i in temp.status] == ["SENSOR_FAULT", "reason_99", "ok"]
    assert temp.available.tolist() == [False, False, True] and np.isnan(temp.values[2])
    raw = log.channel("corner_bl", "sample", "signal_0x7f01")
    assert raw.value_type == "raw" and raw.values.tolist() == [0xDEADBEEF]
    assert raw.seq.tolist() == [11]
    event = log.channel("central", "sample", "EVENT")
    assert event.texts == ("code_99", "") and event.values[0] == 3
    assert [event.statuses[i] for i in event.status] == ["ok", "NO_DATA"]
    assert log.channel("central", "sample", "COCKPIT_LINK").values.tolist() == [1.0]
    reasons = [r.reason for r in log.integrity.rejected_records]
    assert reasons[0] == "temperature TEMP_CVT: value type changed from f32 to bool"
    assert "beyond the signed 64-bit nanosecond range" in reasons[1]


def test_bus_sync_tracking_follows_heartbeats_and_events() -> None:
    accel = struct.pack("<hhhH", 100, 200, 300, 0)
    frames = [
        session(start_us=0),
        can(100, 0x300, accel),  # corner_fl before any heartbeat: node clock
        can(200, 0x707, b"\x01\x00"),  # corner_fl heartbeat: synced
        can(10_100, 0x300, struct.pack("<hhhH", 1, 2, 3, 1)),
        can(10_200, 0x20 + 7, struct.pack("<I", 1) + bytes([2, 0x10, 0x27, 0])),  # BOOT
        can(20_100, 0x300, struct.pack("<hhhH", 4, 5, 6, 2)),  # unsynced again
        can(20_200, 0x20 + 6, struct.pack("<HH", 0x51, 7) + bytes([3, 0, 0, 0])),  # FOUND 7
        can(20_300, 0x707, b"\x01\x00"),
        can(30_100, 0x300, struct.pack("<hhhH", 7, 8, 9, 3)),
        can(30_200, 0x501, struct.pack("<h", -32768 + 2)),  # CVT temp: NO_DATA
        can(30_300, 0x200, struct.pack("<HHHH", 0xFFF9, 1234, 0, 3)),  # NOT_FITTED engine
        can(30_400, 0x7FF, b"\x01\x02\x03\x04\x05\x06\x07\x08"),  # unknown id
        can(30_500, 0x720 + 0x0B, b"\0" * 8),  # health page 11 of node 2, zero fields
        can(30_600, 0x700 + 16 * 2 + 3, b"\0\0\x01\0\0\0\0\0"),  # page 3 reserved set
    ]
    log = read(b"".join(frames))
    node_clock = log.channel("corner_fl", "sample", "IMU_FL_ACCEL_X", NODE_CLOCK)
    shared = log.channel("corner_fl", "sample", "IMU_FL_ACCEL_X")
    assert node_clock.times_us.tolist() == [0, 20_000]
    assert shared.times_us.tolist() == [10_000, 30_000]
    assert shared.seq.tolist() == [1, 3]
    assert f32_bits(float(shared.values[0])) == f32_bits(f32(1 * f32(0.01)))
    boot = log.channel("corner_fl", "sample", "EVENT")
    assert boot.texts == ("BOOT",) and boot.times_us.tolist() == [10_000]
    found = log.channel("central", "sample", "FAULT")
    assert found.texts == ("NODE_FOUND",) and found.values.tolist() == [7.0]
    cvt = log.channel("temperature", "sample", "TEMP_CVT", NODE_CLOCK)
    assert [cvt.statuses[i] for i in cvt.status] == ["NO_DATA"]
    engine = log.channel("powertrain", "sample", "ENGINE_RPM", NODE_CLOCK)
    assert [engine.statuses[i] for i in engine.status] == ["NOT_FITTED"]
    assert log.channel("powertrain", "sample", "CVT_RPM", NODE_CLOCK).values.tolist() == [1234]
    assert log.channel("node0", "can_raw", "").texts == ("0x7FF 0102030405060708",)
    # A health page with a reserved field set is kept as a raw frame of its sender.
    raw = log.channel("powertrain", "can_raw", "")
    assert raw.texts == ("0x723 0000010000000000",)
    assert log.channel("powertrain", "health", "rejected_count").values.tolist() == [0.0]


def test_text_loss_and_health_records_use_the_latest_shared_time() -> None:
    health = bytes([6]) + struct.pack("<IBBH", 1234, 1, 1, 0) + b"\0" * 88
    data = b"".join(
        [
            session(start_us=500),
            record(0x06, b"boot text"),
            sample(0x0600, 6, 2, 7, 2_000),
            record(0x03, health),
            record(0x05, struct.pack("<IIQ", 3, 90, 3_000)),
            record(0x06, b"after loss"),
            end(4_000, 6),
        ]
    )
    log = read(data)
    text = log.channel("central", "text", "")
    assert text.texts == ("boot text", "after loss") and text.times_us.tolist() == [500, 3_000]
    assert log.channel("central", "health", "uptime_ms").times_us.tolist() == [2_000]
    assert log.channel("central", "loss", "dropped_records").values.tolist() == [3.0]
    assert (log.integrity.dropped_records, log.integrity.dropped_bytes) == (3, 90)
    assert log.integrity.records_written == 6 and not log.integrity.damaged


# -- progress, cancellation and size ---------------------------------------------


def big_log(frames: int) -> bytes:
    parts = [session(start_us=0), can(100, 0x703, b"\x01\x00")]
    for k in range(frames):
        data = struct.pack("<hhhH", k % 1000, 2, -3, k & 0xFFFF)
        parts.append(can(k * 10_000 + 450, 0x100, data))
    return b"".join(parts)


def test_large_log_reports_progress_and_reads_every_sample(tmp_path: Path) -> None:
    path = tmp_path / "large.lbrlog"
    path.write_bytes(big_log(60_000))
    reports: list[tuple[int, int]] = []
    started = time.perf_counter()
    log = read_lbrlog(path, progress=lambda done, total: reports.append((done, total)))
    elapsed = time.perf_counter() - started
    size = path.stat().st_size
    assert log.sample_count == 180_002  # and one heartbeat (state, err_flags)
    brake = log.channel("driver_input", "sample", "BRAKE_PEDAL")
    assert np.all(np.diff(brake.times_us) == 10_000)
    assert brake.seq[-1] == 59_999 and brake.values[-1] == f32(999 * f32(0.01))
    assert reports[-1] == (size, size) and len(reports) >= 2
    assert all(b[0] >= a[0] for a, b in zip(reports, reports[1:]))
    assert all(total == size for _, total in reports)
    assert elapsed < 20, f"reading {size} bytes took {elapsed:.1f} s"


def test_cancellation_stops_the_read_and_closes_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "large.lbrlog"
    path.write_bytes(big_log(40_000))
    token = CancellationToken()
    opened: list[Any] = []
    real_open = Path.open

    def tracking_open(self: Path, *args: Any, **kwargs: Any):
        handle = real_open(self, *args, **kwargs)
        if self == path:
            opened.append(handle)
        return handle

    monkeypatch.setattr(Path, "open", tracking_open)

    def progress(done: int, total: int) -> None:
        if done > total // 3:
            token.cancel()

    with pytest.raises(CancellationRequested):
        read_lbrlog(path, cancellation=token, progress=progress)
    assert len(opened) == 1 and opened[0].closed and opened[0].mode == "rb"
    if sys.platform.startswith("linux"):
        assert str(path) not in {os.path.realpath(f.path) for f in os.scandir("/proc/self/fd")}
    already = CancellationToken()
    already.cancel()
    with pytest.raises(CancellationRequested):
        read(GOLDEN_BYTES, cancellation=already)
