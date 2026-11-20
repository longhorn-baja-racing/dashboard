"""The firmware log (.lbrlog) replay source passes the shared source contract.

Expected descriptors and records are written from the firmware's expected
decode (``tests/fixtures/lbr/session.json``) and the mapping rules in
``docs/lbrlog.md``, never by decoding the fixture with the adapter under test.
"""

from __future__ import annotations

import json
import os
import struct
import sys
import zlib
from pathlib import Path
from typing import Any

import pytest

from lbr_dashboard.core import (
    CancellationRequested,
    CancellationToken,
    SignalSample,
    SourceEvent,
    SourceEventKind,
    SourceState,
)
from lbr_dashboard.importers.lbrlog import LbrlogError
from lbr_dashboard.importers.lbrlog_source import prepare_lbrlog

from .checks import (
    AdapterRun,
    assert_cancelled,
    assert_closed,
    assert_controls,
    assert_finite,
    collect,
    records,
    until,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "lbr"
GOLDEN = bytes.fromhex((FIXTURES / "session.lbr.hex").read_text())
DECODE = json.loads((FIXTURES / "session.json").read_text())
CATALOG = json.loads((FIXTURES / "catalog.json").read_text())
NODES = {n["id"]: n["name"] for n in CATALOG["nodes"]}
SIGNALS = {s["name"]: s for s in CATALOG["signals"]}
#: Heartbeat (health) record fields in page order (docs/protocol.md, health.h).
HEALTH_FIELDS = (
    "uptime_ms reset_reason state flags fw_version protocol_version boot_count catalog_hash "
    "config_crc can_tec can_rec can_bus_off_count can_tx_dropped can_rx_dropped overflow_count "
    "queue_high_water write_error_count transport_error_count sensor_status calibration_state "
    "last_sample_age_ms last_sd_write_age_ms acquire_hz publish_hz log_hz time_step_count "
    "last_sync_error_us rejected_count can_bus_errors"
).split()
SHARED = "lbr_central_us"
LOSS = next(r for r in DECODE["records"] if r["type"] == "LOSS")
END_OF_RECORDS = 688


def clean_log() -> bytes:
    """The golden log without its truncated tail and LOSS record: nothing to report."""

    start = LOSS["offset"]
    text = next(r for r in DECODE["records"] if r["type"] == "TEXT")
    return GOLDEN[:start] + GOLDEN[text["offset"] : END_OF_RECORDS]


def write(tmp_path: Path, data: bytes, name: str = "session.lbrlog") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def value_of(sample: dict[str, Any]) -> Any:
    bits = int(sample["value_bits"], 16)
    if not sample["valid"]:
        return None
    if "event" in sample:
        return {"code": sample["event"], "argument": sample["event_arg"]}
    kind = sample["type"]
    if kind == "f32":
        return struct.unpack("<f", struct.pack("<I", bits))[0]
    if kind == "i32":
        return struct.unpack("<i", struct.pack("<I", bits))[0]
    if kind == "bool":
        return bool(bits)
    return bits


def expected_events(source_id: str, *, with_loss: bool) -> tuple[SourceEvent, ...]:
    """Records in log order from the firmware's decode and docs/lbrlog.md."""

    events = []

    def add(identifier: str, t_us: int, value: Any, quality: str, unit: str, domain: str = SHARED):
        sample = SignalSample(
            t_us * 1000,
            value,
            available=value is not None,
            quality=quality,
            timestamp_domain=domain,
            source_id=source_id,
            unit=unit or None,
        )
        events.append(
            SourceEvent(SourceEventKind.RECORD, source_id, signal_id=identifier, sample=sample)
        )

    def add_sample(s: dict[str, Any]) -> None:
        node = NODES[s["source"]]
        identifier = f"{node}.{s['name'].lower()}"
        domain = SHARED
        if not s["synced"]:
            identifier += ".node-clock"
            domain = f"lbr_node_clock.{node}"
        unit = "" if "event" in s else SIGNALS[s["name"]]["unit"]
        add(identifier, s["t_us"], value_of(s), s.get("reason", "ok"), unit, domain)

    latest = 0
    for r in DECODE["records"]:
        kind = r["type"]
        if kind == "SESSION":
            latest = r["start_time_us"]
        elif kind == "SAMPLE":
            add_sample(r["sample"])
            if r["sample"]["synced"]:
                latest = r["sample"]["t_us"]
        elif kind == "CAN_FRAME":
            frame = r["frame"]
            latest = frame["t_us"]
            node = NODES[frame["sender"]]
            if "heartbeat" in frame:
                beat = frame["heartbeat"]
                flags = [
                    k
                    for k, bit in CATALOG["heartbeat_err_flags"].items()
                    if beat["err_flags"] & bit
                ]
                add(f"{node}.heartbeat.state", latest, beat["state"], "ok", "")
                add(
                    f"{node}.heartbeat.err_flags",
                    latest,
                    beat["err_flags"],
                    "|".join(flags) or "ok",
                    "bitmask",
                )
            for s in frame.get("samples", []):
                add_sample(s)
        elif kind == "HEALTH":
            node = NODES[r["health"]["node_id"]]
            for name in HEALTH_FIELDS:
                add(f"{node}.health.{name}", latest, r["health"][name], "ok", "")
        elif kind == "LOSS" and with_loss:
            latest = r["t_us"]
            add("central.loss.dropped_records", latest, r["dropped_records"], "ok", "")
            add("central.loss.dropped_bytes", latest, r["dropped_bytes"], "ok", "")
        elif kind == "TEXT":
            add("central.text", latest, r["text"], "ok", "")
        elif kind == "END":
            latest = r["t_us"]
    return tuple(events)


def fd_probe(path: Path):
    """True while no file descriptor of this process refers to ``path`` (Linux only)."""

    if not sys.platform.startswith("linux"):
        return lambda: True
    target = os.path.realpath(path)

    def idle() -> bool:
        for entry in os.scandir("/proc/self/fd"):
            try:
                if os.path.realpath(entry.path) == target:
                    return False
            except OSError:
                continue
        return True

    return idle


def test_clean_log_meets_the_finite_source_contract(tmp_path: Path) -> None:
    path = write(tmp_path, clean_log())
    original = path.read_bytes()
    prepared = prepare_lbrlog(path, CancellationToken())
    source_id = str(path.resolve())
    expected = expected_events(source_id, with_loss=False)
    assert len(expected) == 51 - 2  # session.csv data rows without the two loss rows
    source = prepared.source()
    run = AdapterRun(source, fd_probe(path))
    assert_controls(source)
    assert not source.capabilities.is_live
    for _ in range(2):
        assert_finite(run, prepared.schema, expected)
    assert path.read_bytes() == original
    by_id = {d.identifier: d for d in prepared.schema.signals}
    assert set(by_id) == {e.signal_id for e in expected}
    brake = by_id["driver_input.brake_pedal"]
    assert (brake.data_type, brake.canonical_unit, brake.device_id) == (
        "numeric",
        "%",
        "driver_input",
    )
    assert brake.nominal_rate_hz == 100 and brake.description == "Brake pedal compression"
    assert brake.timestamp_domain == SHARED and brake.path == ("driver_input",)
    assert brake.display["lbr_signal_id"] == 0x100 and brake.display["lbr_node_id"] == 3
    stress = by_id["stress.stress_3.node-clock"]
    assert stress.timestamp_domain == "lbr_node_clock.stress"
    assert stress.display_name == "STRESS_3 (node clock)"
    kinds = {
        i: by_id[i].data_type
        for i in (
            "central.cockpit_link",
            "central.gps_fix",
            "central.fault",
            "central.text",
            "central.health.uptime_ms",
            "corner_br.heartbeat.err_flags",
        )
    }
    assert kinds == {
        "central.cockpit_link": "boolean",
        "central.gps_fix": "status",
        "central.fault": "event",
        "central.text": "string",
        "central.health.uptime_ms": "numeric",
        "corner_br.heartbeat.err_flags": "status",
    }
    assert by_id["central.health.uptime_ms"].path == ("central", "health")
    metadata: Any = prepared.schema.metadata
    assert metadata["session"]["boot_count"] == 42
    assert metadata["integrity"]["truncated_tail"] == 0


def test_damaged_log_reports_what_was_skipped_then_replays_every_good_record(
    tmp_path: Path,
) -> None:
    damaged = bytearray(GOLDEN)
    gps = next(r for r in DECODE["records"] if r.get("sample", {}).get("name") == "GPS_LATITUDE")
    damaged[gps["offset"] + 12] ^= 0x10
    path = write(tmp_path, bytes(damaged))
    prepared = prepare_lbrlog(path, CancellationToken())
    source = prepared.source()
    try:
        source.open()
        source.start()
        events = collect(source)
    finally:
        assert_closed(AdapterRun(source))
    errors = [e.error for e in events if e.kind is SourceEventKind.ERROR]
    assert len(errors) == 1 and errors[0] is not None and errors[0].recoverable
    message = errors[0].message
    assert f"skipped 30 corrupt bytes in 1 region(s) at offset {gps['offset']}" in message
    assert "9-byte incomplete record at the end (offset 688)" in message
    assert "dropped 5 record(s)" in message
    assert errors[0].context["regions"] == ((gps["offset"], 30),)
    first_record = next(i for i, e in enumerate(events) if e.kind is SourceEventKind.RECORD)
    assert events.index(next(e for e in events if e.kind is SourceEventKind.ERROR)) < first_record
    wanted = [
        e
        for e in expected_events(str(path.resolve()), with_loss=True)
        if e.signal_id != "central.gps_latitude"
    ]
    assert [(e.signal_id, e.sample) for e in records(events)] == [
        (e.signal_id, e.sample) for e in wanted
    ]
    assert events[-1].reason == "complete" and source.health().last_error == errors[0]


# -- a longer synthetic log: backpressure and cancellation --------------------------


def record(rtype: int, payload: bytes) -> bytes:
    header = struct.pack("<BBH", rtype, 0, len(payload))
    return b"\xa5\x5a" + header + payload + struct.pack("<I", zlib.crc32(header + payload))


def f32(value: float) -> float:
    return struct.unpack("<f", struct.pack("<f", value))[0]


def long_log(frames: int) -> bytes:
    config = b"node=central\n"
    fixed = struct.pack(
        "<HHIIIBBHIQ", 1, 4, 512, CATALOG["catalog_hash"], zlib.crc32(config), 6, 1, 0, 1, 0
    )
    parts = [record(0x01, fixed + config)]
    parts.append(record(0x04, struct.pack("<QIBB", 50, 0x703, 0, 2) + b"\x01\x00" + b"\0" * 6))
    for k in range(frames):
        data = struct.pack("<hhhH", k, -k, 7, k)
        parts.append(record(0x04, struct.pack("<QIBB", k * 10_000 + 400, 0x100, 0, 8) + data))
    parts.append(record(0x07, struct.pack("<QI", frames * 10_000, frames + 3)))
    return b"".join(parts)


def long_expected(source_id: str, frames: int) -> tuple[SourceEvent, ...]:
    scale = {f["name"]: f32(f["scale"]) for f in CATALOG["frames"][0]["fields"]}
    events = []

    def add(identifier: str, t_us: int, value: Any, unit: str | None) -> None:
        sample = SignalSample(
            t_us * 1000,
            value,
            quality="ok",
            timestamp_domain=SHARED,
            source_id=source_id,
            unit=unit,
        )
        events.append(
            SourceEvent(SourceEventKind.RECORD, source_id, signal_id=identifier, sample=sample)
        )

    add("driver_input.heartbeat.state", 50, 1, None)
    add("driver_input.heartbeat.err_flags", 50, 0, "bitmask")
    for k in range(frames):
        for name, raw, unit in (
            ("BRAKE_PEDAL", k, "%"),
            ("GAS_PEDAL", -k, "%"),
            ("STEERING_ANGLE", 7, "deg"),
        ):
            add(f"driver_input.{name.lower()}", k * 10_000, f32(raw * scale[name]), unit)
    return tuple(events)


def test_stalled_consumer_loses_nothing_and_stop_releases_the_worker(tmp_path: Path) -> None:
    frames = 60  # 182 records: more than the queue holds, within the collector limit
    path = write(tmp_path, long_log(frames))
    prepared = prepare_lbrlog(path, CancellationToken())
    expected = long_expected(str(path.resolve()), frames)
    capacity = prepared.source().capabilities.buffer_capacity
    assert len(expected) > capacity
    source = prepared.source()
    run = AdapterRun(source, fd_probe(path))
    try:
        source.open()
        source.start()
        until(lambda: source.events.stats.buffered == capacity, "queue never filled")
        assert not source.join(0.08), "replay finished while its consumer was stalled"
        assert_finite(run, prepared.schema, expected, already_started=True)
    finally:
        assert_closed(run)
    stopped = AdapterRun(prepared.source(), fd_probe(path))
    stopped.source.open()
    stopped.source.start()
    until(lambda: stopped.source.events.stats.buffered == capacity, "queue never filled")
    assert_cancelled(stopped)


def test_cancelled_open_and_start_publish_no_records(tmp_path: Path) -> None:
    prepared = prepare_lbrlog(write(tmp_path, clean_log()), CancellationToken())
    for stage in ("open", "start"):
        source = prepared.source()
        token = CancellationToken()
        token.cancel()
        try:
            if stage == "open":
                source.open(token)
            else:
                source.open()
                source.start(token)
                until(lambda: source.join(0), "pre-cancelled start did not stop")
            assert source.state is SourceState.STOPPED
            assert source.health().terminal_reason == "cancelled"
            assert all(e.kind is not SourceEventKind.RECORD for e in source.events.drain())
        finally:
            assert_closed(AdapterRun(source))


def test_preparation_refusals_are_actionable(tmp_path: Path) -> None:
    newer = bytearray(clean_log())
    newer[6:8] = struct.pack("<H", 2)  # SESSION format_version
    newer[84 - 4 : 84] = struct.pack("<I", zlib.crc32(bytes(newer[2 : 84 - 4])))
    with pytest.raises(LbrlogError, match="format version 2"):
        prepare_lbrlog(write(tmp_path, bytes(newer), "newer.lbrlog"), CancellationToken())
    with pytest.raises(LbrlogError, match="holds 49 readings; the replay source keeps at most 10"):
        prepare_lbrlog(write(tmp_path, clean_log()), CancellationToken(), max_samples=10)
    cancelled = CancellationToken()
    cancelled.cancel()
    with pytest.raises(CancellationRequested):
        prepare_lbrlog(write(tmp_path, clean_log()), cancelled)
    reports: list[tuple[int, int]] = []
    prepare_lbrlog(
        write(tmp_path, clean_log()), CancellationToken(), lambda d, t: reports.append((d, t))
    )
    assert reports[-1] == (len(clean_log()), len(clean_log()))


def test_non_finite_readings_outputs_and_id_collisions(tmp_path: Path) -> None:
    config = b"node=central\n"
    fixed = struct.pack(
        "<HHIIIBBHIQ", 1, 4, 512, CATALOG["catalog_hash"], zlib.crc32(config), 6, 1, 0, 1, 0
    )
    nan = struct.unpack("<I", struct.pack("<f", float("inf")))[0]
    data = b"".join(
        [
            record(0x01, fixed + config),
            record(0x02, struct.pack("<HBBIQI", 0x0504, 4, 0x31, 1, 1_000, nan)),
            record(0x20, struct.pack("<HBB", 1, 1, 6) + b"CVT/Ratio"),
            record(0x20, struct.pack("<HBB", 2, 4, 0) + b"Cvt/Ratio"),
            record(0x21, struct.pack("<QHBI", 2_000, 1, 0, 0x40200000)),
            record(0x21, struct.pack("<QHBI", 3_000, 2, 0, 1)),
            record(0x21, struct.pack("<QHBI", 4_000, 1, 8, 0)),
        ]
    )
    path = write(tmp_path, data)
    prepared = prepare_lbrlog(path, CancellationToken())
    samples = dict.fromkeys(prepared.identifiers)
    delivered: dict[str, list[SignalSample]] = {}
    for identifier, sample in prepared.records():
        delivered.setdefault(identifier, []).append(sample)
    assert set(samples) == {
        "temperature.temp_cvt",
        "central.output.cvt.ratio",
        "central.output.cvt.ratio-2",
    }
    infinite = delivered["temperature.temp_cvt"][0]
    assert not infinite.available and infinite.value is None
    assert infinite.quality == "non_finite" and infinite.extra["reading"] == "inf"
    ratio = delivered["central.output.cvt.ratio"]
    assert [s.value for s in ratio] == [2.5, None] and ratio[1].quality == "STALE"
    assert ratio[0].unit == "%"
    assert [s.value for s in delivered["central.output.cvt.ratio-2"]] == [True]
    by_id = {d.identifier: d for d in prepared.schema.signals}
    assert by_id["central.output.cvt.ratio-2"].data_type == "boolean"
    assert by_id["central.output.cvt.ratio"].path == ("central", "output")
