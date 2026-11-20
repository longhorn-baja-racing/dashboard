"""The USB serial live source meets the source contract on an in-memory serial line.

Expected values come from the firmware's expected decode of its USB stream
fixture (``tests/fixtures/lbr/stream_usb.json``) and from the packets the tests
build, never from decoding with the adapter under test.
"""

from __future__ import annotations

import json
import struct
from collections.abc import Callable
from pathlib import Path

import pytest

from lbr_dashboard.core import (
    CancellationToken,
    SourceEvent,
    SourceEventKind,
    SourceState,
    TelemetrySession,
)
from lbr_dashboard.importers.lbr_stream import cobs_encode
from lbr_dashboard.importers.lbrlog import REC_CAN_FRAME, REC_KEY, REC_TEXT, REC_VALUE
from lbr_dashboard.importers.serial_source import (
    ERRORS_PER_SECOND,
    OutputKey,
    SerialLiveSource,
    SerialPort,
    SerialPortError,
    prepare_serial,
)

from .checks import AdapterRun, assert_cancelled, assert_closed, assert_controls, until
from .fake_serial import (
    GOLDEN_STREAM,
    FakeLine,
    can_frame,
    drive_stream,
    heartbeat,
    key_record,
    packet,
    value_record,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "lbr"
EXPECTED = json.loads((FIXTURES / "stream_usb.json").read_text())
CATALOG = json.loads((FIXTURES / "catalog.json").read_text())
NODES = {n["id"]: n["name"] for n in CATALOG["nodes"]}
SPEED = OutputKey("Vehicle/SpeedKph", "f32", "", 7)


def make(line: FakeLine, **options) -> tuple[SerialLiveSource, AdapterRun]:
    options.setdefault("read_timeout_s", 0.01)
    source = SerialLiveSource("COM7", line.opener, **options)
    return source, AdapterRun(source, lambda: line.open_ports == 0)


def drain_until(
    source: SerialLiveSource,
    events: list[SourceEvent],
    done: Callable[[list[SourceEvent]], bool],
    message: str,
) -> None:
    def step() -> bool:
        events.extend(source.events.drain())
        return done(events)

    until(step, message)


def readings(events: list[SourceEvent], signal_id: str) -> list:
    return [
        e.sample for e in events if e.kind is SourceEventKind.RECORD and e.signal_id == signal_id
    ]


def errors(events: list[SourceEvent], code: str | None = None) -> list:
    return [
        e.error
        for e in events
        if e.kind is SourceEventKind.ERROR and e.error and (code is None or e.error.code == code)
    ]


def f32(bits: str) -> float:
    return struct.unpack(">f", bytes.fromhex(bits))[0]


def test_golden_usb_stream_reaches_records_with_the_firmware_values() -> None:
    line = FakeLine(GOLDEN_STREAM, chunk_size=7)
    source, run = make(line)
    events: list[SourceEvent] = []
    try:
        assert source.capabilities.is_live
        source.open()
        source.start()
        drain_until(
            source, events, lambda ev: bool(readings(ev, "central.text")), "text never arrived"
        )
        descriptors = tuple(e.descriptor for e in events if e.kind is SourceEventKind.DESCRIPTOR)
        assert descriptors == source.schema.signals
        session = TelemetrySession(source.schema, max_samples_per_signal=1000)
        session.ingest(events)  # Every record validates against its descriptor.
        for wanted in EXPECTED["packets"][0]["records"]:
            for sample in wanted["frame"].get("samples", []):
                node = NODES[sample["source"]]
                clock = "" if sample["synced"] else ".node-clock"
                identifier = f"{node}.{sample['name'].lower()}{clock}"
                [got] = readings(events, identifier)
                assert got.timestamp_ns == sample["t_us"] * 1000
                assert got.value == f32(sample["value_bits"])
                assert got.timestamp_domain == (
                    "lbr_central_us" if sample["synced"] else f"lbr_node_clock.{node}"
                )
                assert got.extra["sequence"] == EXPECTED["packets"][0]["seq"]
            beat = wanted["frame"].get("heartbeat")
            if beat:
                node = NODES[wanted["frame"]["sender"]]
                [state] = readings(events, f"{node}.heartbeat.state")
                assert state.value == beat["state"]
                assert state.timestamp_ns == wanted["frame"]["t_us"] * 1000
        [text] = readings(events, "central.text")
        assert text.value == EXPECTED["packets"][1]["records"][0]["text"]
        seqs = [s.value for s in readings(events, "usb_link.packet_seq")]
        assert seqs == [p["seq"] for p in EXPECTED["packets"]]  # rolls over without loss
        assert [s.value for s in readings(events, "usb_link.lost_packets")] == [0]
        assert [s.value for s in readings(events, "usb_link.rejected_frames")] == [
            EXPECTED["bad_frames"]
        ]
        decode = errors(events, "decode_error")
        assert len(decode) == EXPECTED["bad_frames"] and decode[0].recoverable
        assert decode[0].context["reason"] == "cobs"
        assert any(e.kind is SourceEventKind.HEALTH for e in events), "heartbeats not reported"
        link = source.link_status()
        assert (link.packets, link.lost_packets, link.rejected_frames) == (2, 0, 1)
        assert source.health_config.sequence_bits == 32
        assert source.health_config.reports_decode_errors
        assert source.state is SourceState.RUNNING
        assert_controls(source)
        assert_cancelled(run)
    finally:
        assert_closed(run)


def test_partial_reads_corruption_and_lost_packets_are_observable() -> None:
    packets = drive_stream(100, 5000, 12)
    corrupt = bytearray(packets[4])
    corrupt[20] ^= 0xFF
    del packets[7]  # lost in transit: sequence 107 never arrives
    stream = b"".join(packets[:4]) + bytes(corrupt) + b"".join(packets[5:])
    line = FakeLine(stream, chunk_size=3)
    source, run = make(line, outputs=(SPEED,))
    events: list[SourceEvent] = []
    try:
        source.open()
        source.start()
        drain_until(
            source,
            events,
            lambda ev: len(readings(ev, "usb_link.packet_seq")) == 10,
            "packets never arrived",
        )
        assert [s.value for s in readings(events, "usb_link.packet_seq")] == [
            100, 101, 102, 103, 105, 106, 108, 109, 110, 111
        ]  # fmt: skip
        # The corrupted packet (104) and the missing one (107) were both lost.
        assert [s.value for s in readings(events, "usb_link.lost_packets")] == [0, 1, 2]
        assert [s.value for s in readings(events, "usb_link.rejected_frames")] == [0, 1]
        assert len(errors(events, "decode_error")) == 1
        brake = readings(events, "driver_input.brake_pedal") + readings(
            events, "driver_input.brake_pedal.node-clock"
        )
        ticks = [i for i in range(12) if i not in (4, 7)]
        assert sorted(s.timestamp_ns for s in brake) == [(5000 + i) * 10**7 for i in ticks]
        assert [s.value for s in readings(events, "central.output.vehicle.speedkph")] == [
            20.0 + i for i in ticks
        ]
        times = [s.timestamp_ns for s in readings(events, "central.output.vehicle.speedkph")]
        assert times == sorted(times)
        assert source.link_status().lost_packets == 2
    finally:
        assert_closed(run)


def test_unplug_fails_cleanly_releases_the_port_and_reports_stale_data() -> None:
    line = FakeLine(*drive_stream(1, 100, 3))
    source, run = make(line)
    events: list[SourceEvent] = []
    try:
        source.open()
        source.start()
        drain_until(
            source,
            events,
            lambda ev: len(readings(ev, "usb_link.packet_seq")) == 3,
            "packets never arrived",
        )
        line.unplug()
        until(lambda: source.state is SourceState.FAILED, "unplug was not reported")
        events.extend(source.events.drain())
        error = source.health().last_error
        assert error is not None and error.code == "serial_disconnected"
        assert "Retry" in error.message
        assert line.open_ports == 0, "the port was not released after unplug"
        assert events[-1].kind is SourceEventKind.END_OF_STREAM and events[-1].reason == "failed"
        status = source.link_status()
        assert not status.connected and status.packets == 3
        assert "stale" in status.summary()
    finally:
        assert_closed(run)
    # Retry: a new source on the same port after the cable is back.
    line.replug()
    line.send(b"".join(drive_stream(50, 900, 2)))
    factory = prepare_serial("COM7", line.opener, CancellationToken(), discovery_s=0)
    again = factory()
    events = []
    try:
        again.open()
        again.start()
        drain_until(
            again,
            events,
            lambda ev: len(readings(ev, "usb_link.packet_seq")) == 2,
            "reconnect delivered nothing",
        )
    finally:
        assert_closed(AdapterRun(again, lambda: line.open_ports == 0))


def test_open_failure_is_actionable_and_leaves_nothing_open() -> None:
    line = FakeLine()
    line.fail_open = "access denied; close any other program using the port"
    source, run = make(line)
    try:
        source.open()
        source.start()
        until(lambda: source.state is SourceState.FAILED, "open failure was not reported")
        error = source.health().last_error
        assert error is not None and error.code == "serial_open_failed"
        assert "COM7" in error.message and "access denied" in error.message
        assert line.open_ports == 0
    finally:
        assert_closed(run)
    with pytest.raises(SerialPortError, match="COM7: access denied"):
        prepare_serial("COM7", line.opener, CancellationToken(), discovery_s=0.05)


def test_repeated_open_and_close_releases_the_port_every_time() -> None:
    line = FakeLine()
    source, run = make(line)
    for cycle in range(5):
        line.send(b"".join(drive_stream(cycle * 10, 100 + cycle * 10, 2)))
        source.open()
        source.start()
        until(lambda: line.open_ports == 1, "port did not open")
        until(lambda: source.link_status().packets == 2, "cycle received nothing")
        assert source.close(1.0), "close did not finish within its deadline"
        assert line.open_ports == 0
        assert source.state is SourceState.CLOSED
        source.events.drain()
    assert line.opened == 5 and line.closed == 5
    assert_closed(run)


def test_slow_consumer_loses_bounded_counted_records_while_reading_continues() -> None:
    line = FakeLine(*drive_stream(1, 100, 60))
    source, run = make(line, capacity=64)
    try:
        source.open()
        source.start()
        until(lambda: line.pending == 0, "the reader blocked behind a stalled consumer")
        until(lambda: source.link_status().packets == 60, "the reader stopped decoding")
        assert source.events.stats.buffered <= 64
        assert source.health().dropped_records > 0
        assert_cancelled(run)
        assert source.health().dropped_records > 0, "close erased the recorded loss"
    finally:
        assert_closed(run)


def test_nothing_is_ever_written_to_the_port() -> None:
    assert not hasattr(SerialPort, "write"), "the port interface must stay read-only"
    line = FakeLine(GOLDEN_STREAM, *drive_stream(1, 100, 5))
    source, run = make(line)
    try:
        source.open()
        source.start()
        until(lambda: source.link_status().packets == 7, "stream not read")
        factory = prepare_serial("COM7", line.opener, CancellationToken(), discovery_s=0.05)
        assert factory is not None
    finally:
        assert_closed(run)
    used = set().union(*(port.touched for port in line.ports))
    assert used <= {"name", "open", "read", "close"}, used
    assert not any(hasattr(port, "write") for port in line.ports)


def test_device_restart_ends_the_live_session() -> None:
    restart = packet(0, 2_000_000, [(REC_TEXT, b"I (10) central: boot")])
    line = FakeLine(*drive_stream(500, 9000, 3), restart)
    source, run = make(line)
    try:
        source.open()
        source.start()
        until(lambda: source.state is SourceState.FAILED, "restart was not detected")
        error = source.health().last_error
        assert error is not None and error.code == "device_restarted"
        assert line.open_ports == 0
    finally:
        assert_closed(run)


def test_outputs_found_while_preparing_are_shown_and_late_ones_are_reported() -> None:
    announce = packet(
        1,
        5_000_000,
        [(REC_KEY, key_record(7, "Vehicle/SpeedKph")), (REC_KEY, key_record(9, "CVT/Ratio"))],
    )
    line = FakeLine(announce)
    factory = prepare_serial("COM7", line.opener, CancellationToken(), discovery_s=0.2)
    assert line.open_ports == 0, "preparation left the port open"
    source = factory()
    ids = {d.identifier for d in source.schema.signals}
    assert {"central.output.vehicle.speedkph", "central.output.cvt.ratio"} <= ids
    run = AdapterRun(source, lambda: line.open_ports == 0)
    late = [
        (REC_KEY, key_record(11, "Brakes/HottestC", 1, 3)),
        (REC_VALUE, value_record(5_100_000, 11, 80.0)),
        (REC_VALUE, value_record(5_100_000, 7, 33.0)),
    ]
    line.send(packet(2, 5_100_100, late) + packet(3, 5_110_100, late[1:]))
    events: list[SourceEvent] = []
    try:
        source.open()
        source.start()
        drain_until(
            source,
            events,
            lambda ev: len(readings(ev, "central.output.vehicle.speedkph")) == 2,
            "outputs never arrived",
        )
        hidden = errors(events, "channel_not_shown")
        assert len(hidden) == 1 and "Brakes/HottestC" in hidden[0].message
        assert hidden[0].recoverable
        assert source.link_status().hidden_readings == 2
    finally:
        assert_closed(run)


def test_catalog_mismatch_and_malformed_records_are_reported() -> None:
    page2 = can_frame(1_000, 0x700 + 16 * 3 + 2, struct.pack("<II", 12345, 0))
    bad_heartbeat = can_frame(1_100, 0x700 + 3, b"\x01")  # dlc 1: kept as a raw frame
    short = (REC_CAN_FRAME, b"\x00" * 5)  # a CAN_FRAME record must be 22 bytes
    line = FakeLine(
        packet(1, 2_000, [(REC_CAN_FRAME, page2), (REC_CAN_FRAME, bad_heartbeat), short])
    )
    source, run = make(line)
    events: list[SourceEvent] = []
    try:
        source.open()
        source.start()
        drain_until(
            source,
            events,
            lambda ev: (
                bool(readings(ev, "usb_link.packet_seq"))
                and bool(readings(ev, "driver_input.can_raw"))
            ),
            "packet not decoded",
        )
        [mismatch] = errors(events, "catalog_mismatch")
        assert "12345" in mismatch.message and mismatch.recoverable
        assert "malformed record" in errors(events, "decode_error")[0].message
        assert readings(events, "driver_input.health.catalog_hash")[0].value == 12345
        assert readings(events, "driver_input.can_raw")
    finally:
        assert_closed(run)


def test_decode_error_events_are_rate_limited_but_counted() -> None:
    now = [100.0]
    junk = b"".join(cobs_encode(b"\x01" * 30) + b"\0" for _ in range(40))
    line = FakeLine(junk)
    source, run = make(line, clock=lambda: now[0])
    events: list[SourceEvent] = []
    try:
        source.open()
        source.start()
        until(lambda: source.link_status().rejected_frames == 40, "junk not read")
        events.extend(source.events.drain())
        assert len(errors(events, "decode_error")) == ERRORS_PER_SECOND
        now[0] += 1.5
        line.send(b"\x07junk\0")
        drain_until(
            source,
            events,
            lambda ev: any("more frames" in e.message for e in errors(ev, "decode_error")),
            "suppressed errors never summarized",
        )
        summary = next(e for e in errors(events) if "more frames" in e.message)
        assert summary.context["frames"] == 40 - ERRORS_PER_SECOND
    finally:
        assert_closed(run)


def test_heartbeats_publish_health_and_link_freshness() -> None:
    line = FakeLine(packet(1, 1_000, [(REC_CAN_FRAME, heartbeat(900, 3))]))
    now = [50.0]
    source, run = make(line, clock=lambda: now[0])
    events: list[SourceEvent] = []
    try:
        source.open()
        source.start()
        drain_until(
            source,
            events,
            lambda ev: any(e.kind is SourceEventKind.HEALTH for e in ev),
            "heartbeat health not published",
        )
        now[0] += 5
        assert "no packets for 5.0 s - data is stale" in source.link_status().summary()
    finally:
        assert_closed(run)


def test_every_channel_of_the_firmware_fixtures_is_declared() -> None:
    """The CAN capture and session log cover every frame kind; none may be hidden live."""

    capture = bytes.fromhex((FIXTURES / "can_capture.bin.hex").read_text())
    frames = [(REC_CAN_FRAME, capture[i : i + 22]) for i in range(0, len(capture), 22)]
    line = FakeLine(*(packet(n, 50_400_000, frames[n : n + 5]) for n in range(0, 30, 5)))
    source, run = make(line)
    events: list[SourceEvent] = []
    try:
        source.open()
        source.start()
        until(lambda: source.link_status().packets == 6, "capture not read")
        events.extend(source.events.drain())
        assert not errors(events, "channel_not_shown")
        assert source.link_status().hidden_readings == 0
        assert len([e for e in events if e.kind is SourceEventKind.RECORD]) > 6 + 40
    finally:
        assert_closed(run)
