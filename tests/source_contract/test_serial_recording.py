"""Recording the live serial source to .lbrlog and replaying it through the log reader."""

from __future__ import annotations

import errno
import io
import time
from pathlib import Path

import pytest

from lbr_dashboard.core import CancellationToken, SourceEvent, SourceEventKind, SourceState
from lbr_dashboard.importers.lbrlog import read_lbrlog
from lbr_dashboard.importers.lbrlog_source import prepare_lbrlog
from lbr_dashboard.importers.lbrlog_writer import LiveRecorder, RecordingError, encode_record
from lbr_dashboard.importers.serial_source import OutputKey, SerialLiveSource

from .checks import AdapterRun, assert_closed, until
from .fake_serial import GOLDEN_STREAM, FakeLine, drive_stream

SPEED = OutputKey("Vehicle/SpeedKph", "f32", "", 7)
HOST_CONFIG = {"host_capture": "usb_serial_live_stream", "host_port": "COM7"}


def make(line: FakeLine) -> tuple[SerialLiveSource, AdapterRun]:
    source = SerialLiveSource("COM7", line.opener, outputs=(SPEED,), read_timeout_s=0.01)
    return source, AdapterRun(source, lambda: line.open_ports == 0)


def recorder(path: Path, source: SerialLiveSource, **options) -> LiveRecorder:
    rec = LiveRecorder(path, catalog_hash=source.catalog.hash, config=HOST_CONFIG, **options)
    rec.start()
    return rec


def received(source: SerialLiveSource, events: list[SourceEvent], packets: int) -> None:
    def done() -> bool:
        events.extend(source.events.drain())
        return source.link_status().packets >= packets

    until(done, "the stream was not received")
    time.sleep(0.02)
    events.extend(source.events.drain())


def comparable(signal_id: str, sample) -> tuple:
    return (
        signal_id,
        sample.timestamp_ns,
        sample.value,
        sample.available,
        sample.quality,
        sample.unit,
        sample.timestamp_domain,
    )


def live_readings(events: list[SourceEvent]) -> list[tuple]:
    return [
        comparable(e.signal_id, e.sample)
        for e in events
        if e.kind is SourceEventKind.RECORD
        and e.signal_id is not None
        and e.sample is not None
        and not e.signal_id.startswith("usb_link.")
    ]


def replayed(path: Path) -> list[tuple]:
    prepared = prepare_lbrlog(path, CancellationToken())
    return [comparable(identifier, sample) for identifier, sample in prepared.records()]


def test_recording_replays_with_identical_ids_values_and_times(tmp_path: Path) -> None:
    line = FakeLine()
    source, run = make(line)
    path = tmp_path / "capture.lbrlog"
    events: list[SourceEvent] = []
    try:
        source.open()
        source.start()
        until(lambda: line.open_ports == 1, "port not opened")
        rec = recorder(path, source)
        source.attach_recorder(rec)
        line.send(GOLDEN_STREAM)
        for chunk in drive_stream(1, 50_340, 40):
            line.send(chunk)
        received(source, events, 42)
        source.attach_recorder(None)
        assert rec.stop()
        status = rec.status()
        assert status.state == "finished" and status.error is None
        assert status.bytes_written == path.stat().st_size
        assert not status.has_loss
    finally:
        assert_closed(run)
    live = live_readings(events)
    assert len(live) > 150
    assert replayed(path) == live
    log = read_lbrlog(path)
    assert log.integrity.problems() == ()  # SESSION checked, END present, nothing skipped
    assert log.integrity.records_written == log.integrity.records
    assert log.session is not None and log.session.logger == "central"
    assert dict(log.session.config)["host_capture"] == "usb_serial_live_stream"
    # Descriptors of the replay are the live ones (apart from the source ID).
    prepared = prepare_lbrlog(path, CancellationToken())
    live_descriptors = {d.identifier: d for d in source.schema.signals}
    for descriptor in prepared.schema.signals:
        twin = live_descriptors[descriptor.identifier]
        assert (twin.display_name, twin.data_type, twin.canonical_unit, twin.path) == (
            descriptor.display_name,
            descriptor.data_type,
            descriptor.canonical_unit,
            descriptor.path,
        )


def test_recording_started_mid_stream_keeps_clock_sync_and_output_names(tmp_path: Path) -> None:
    line = FakeLine(*drive_stream(1, 50_000, 20))
    source, run = make(line)
    path = tmp_path / "late.lbrlog"
    before: list[SourceEvent] = []
    after: list[SourceEvent] = []
    try:
        source.open()
        source.start()
        received(source, before, 20)
        rec = recorder(path, source)
        source.attach_recorder(rec)
        # No heartbeat and no KEY record in the recorded part: both come from before.
        for chunk in drive_stream(21, 50_020, 10, with_heartbeat=False):
            line.send(chunk)
        received(source, after, 30)
        source.attach_recorder(None)
        assert rec.stop()
    finally:
        assert_closed(run)
    live = live_readings(after)
    replay = replayed(path)
    carried = [r for r in replay if ".heartbeat." in r[0]]
    assert carried and all(r[1] < live[0][1] for r in carried)  # earlier, not invented
    assert [r for r in replay if ".heartbeat." not in r[0]] == live
    assert any(r[0] == "driver_input.brake_pedal" for r in replay)
    assert not any(r[0].endswith(".node-clock") for r in replay)
    assert any(r[0] == "central.output.vehicle.speedkph" for r in replay)


def test_link_losses_are_marked_in_the_file(tmp_path: Path) -> None:
    packets = drive_stream(1, 60_000, 12)
    del packets[5]
    packets[8] = packets[8][:10] + b"\xff" + packets[8][11:]
    line = FakeLine()
    source, run = make(line)
    path = tmp_path / "lossy.lbrlog"
    events: list[SourceEvent] = []
    try:
        source.open()
        source.start()
        until(lambda: line.open_ports == 1, "port not opened")
        source.attach_recorder(rec := recorder(path, source))
        line.send(b"".join(packets))
        received(source, events, 10)
        source.attach_recorder(None)
        assert rec.stop()
        status = rec.status()
        assert status.lost_packets == 2 and status.rejected_bytes > 0 and status.has_loss
    finally:
        assert_closed(run)
    log = read_lbrlog(path)
    assert log.integrity.dropped_records == 2
    assert any("dropped 2 record" in problem for problem in log.integrity.problems())
    texts = log.channel("central", "text", "").texts
    assert any(t.startswith("host: 1 packet(s) lost on the live link") for t in texts), texts
    assert any("byte(s) of undecodable frames rejected" in t for t in texts)
    losses = log.channel("central", "loss", "dropped_records")
    assert sorted(losses.values.tolist()) == [1.0, 1.0]
    # Every reading that arrived is in the file, with its device time.
    live = live_readings(events)
    replay = [r for r in replayed(path) if not r[0].startswith("central.loss.")]
    assert [r for r in replay if not r[0] == "central.text"] == [
        r for r in live if not r[0] == "central.text"
    ]


class FailingStream(io.BytesIO):
    """A file that fills up after ``limit`` bytes."""

    def __init__(self, limit: int) -> None:
        super().__init__()
        self.limit = limit

    def write(self, data) -> int:  # type: ignore[override]
        if self.tell() + len(data) > self.limit:
            raise OSError(errno.ENOSPC, "No space left on device")
        return super().write(data)


def test_disk_full_stops_the_recording_but_not_reception(tmp_path: Path) -> None:
    line = FakeLine()
    source, run = make(line)
    stream = FailingStream(2_000)
    events: list[SourceEvent] = []
    try:
        source.open()
        source.start()
        until(lambda: line.open_ports == 1, "port not opened")
        rec = LiveRecorder(
            tmp_path / "full.lbrlog",
            catalog_hash=source.catalog.hash,
            config=HOST_CONFIG,
            opener=lambda path: stream,
        )
        rec.start()
        source.attach_recorder(rec)
        for chunk in drive_stream(1, 70_000, 30):
            line.send(chunk)
        until(lambda: rec.status().state == "failed", "write failure not reported")
        assert "No space left on device" in (rec.status().error or "")
        for chunk in drive_stream(31, 70_030, 5):
            line.send(chunk)
        received(source, events, 35)
        assert source.state is SourceState.RUNNING  # live view continues
        assert rec.stop()
        assert stream.closed
    finally:
        assert_closed(run)


def test_unwritable_or_existing_destinations_are_refused(tmp_path: Path) -> None:
    existing = tmp_path / "keep.lbrlog"
    existing.write_bytes(b"precious")
    with pytest.raises(RecordingError, match="already exists"):
        LiveRecorder(existing, catalog_hash=1, config={}).start()
    assert existing.read_bytes() == b"precious"
    with pytest.raises(RecordingError, match="Cannot create"):
        LiveRecorder(tmp_path / "missing" / "x.lbrlog", catalog_hash=1, config={}).start()


def test_interrupted_recording_stays_readable(tmp_path: Path) -> None:
    line = FakeLine()
    source, run = make(line)
    path = tmp_path / "crash.lbrlog"
    events: list[SourceEvent] = []
    try:
        source.open()
        source.start()
        until(lambda: line.open_ports == 1, "port not opened")
        source.attach_recorder(rec := recorder(path, source))
        line.send(b"".join(drive_stream(1, 80_000, 15)))
        received(source, events, 15)
        until(lambda: rec.status().records_written > 15, "nothing recorded")
        until(lambda: rec.status().buffered_bytes == 0, "recorder did not catch up")
        until(lambda: path.stat().st_size == rec.status().bytes_written, "not flushed")
        # Power loss now: the file has no END and its last record is cut short.
        snapshot = path.read_bytes() + encode_record(6, b"half written")[:9]
        source.attach_recorder(None)
        rec.stop()
    finally:
        assert_closed(run)
    crashed = tmp_path / "crashed.lbrlog"
    crashed.write_bytes(snapshot)
    log = read_lbrlog(crashed)
    assert log.integrity.records_written is None
    assert log.integrity.truncated_tail == 9
    assert any("not closed cleanly" in p for p in log.integrity.problems())
    assert replayed(crashed) == live_readings(events)


class SlowStream(io.BytesIO):
    def write(self, data) -> int:  # type: ignore[override]
        time.sleep(0.002)
        return super().write(data)


def test_bounded_buffer_drops_are_counted_and_marked(tmp_path: Path) -> None:
    line = FakeLine()
    source, run = make(line)
    stream = SlowStream()
    stream.close = lambda: None  # type: ignore[method-assign]  # keep bytes readable
    try:
        source.open()
        source.start()
        until(lambda: line.open_ports == 1, "port not opened")
        rec = LiveRecorder(
            tmp_path / "slow.lbrlog",
            catalog_hash=source.catalog.hash,
            config=HOST_CONFIG,
            buffer_bytes=600,
            opener=lambda path: stream,
        )
        rec.start()
        source.attach_recorder(rec)
        line.send(b"".join(drive_stream(1, 90_000, 200)))
        until(lambda: source.link_status().packets == 200, "stream not read")
        source.attach_recorder(None)
        assert rec.stop(5.0)
        status = rec.status()
        assert status.dropped_records > 0
    finally:
        assert_closed(run)
    data = stream.getvalue()
    log_path = tmp_path / "slow-copy.lbrlog"
    log_path.write_bytes(data)
    log = read_lbrlog(log_path)
    assert log.integrity.dropped_records == status.dropped_records
    assert log.integrity.dropped_bytes == status.dropped_bytes
    texts = log.channel("central", "text", "").texts
    assert any("dropped because the recorder fell behind" in t for t in texts)


def test_stop_before_any_packet_writes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "empty.lbrlog"
    rec = LiveRecorder(path, catalog_hash=1, config={})
    rec.start()
    assert rec.offer(5, ((6, b"x"),)) is False  # not begun: nothing to attach it to
    assert rec.stop()
    assert rec.status().bytes_written == 0 and path.stat().st_size == 0
