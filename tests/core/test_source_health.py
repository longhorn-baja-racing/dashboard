"""Known-stream checks for source freshness, loss and health metadata."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from lbr_dashboard.core.source_health import (
    MS,
    ConnectionState,
    Freshness,
    HeartbeatState,
    LinkStatus,
    S,
    SequenceStep,
    SequenceTracker,
    SourceHealthConfig,
    SourceHealthTracker,
    format_duration,
    format_source_health,
    status_text,
)


class FakeClock:
    """Monotonic nanosecond clock that only moves when a test says so."""

    def __init__(self, start_ns: int = 0) -> None:
        self.now = start_ns

    def __call__(self) -> int:
        return self.now

    def advance(self, ns: int) -> int:
        self.now += ns
        return self.now


def _tracker(**config: object) -> tuple[SourceHealthTracker, FakeClock]:
    clock = FakeClock(1_000 * S)
    tracker = SourceHealthTracker("usb0", SourceHealthConfig(**config), clock=clock)  # type: ignore[arg-type]
    return tracker, clock


def _feed(tracker: SequenceTracker, values: list[int]) -> list[SequenceStep]:
    return [tracker.observe(value) for value in values]


# --- Sequence streams -------------------------------------------------------


def test_clean_stream_has_no_loss() -> None:
    seq = SequenceTracker(16)
    steps = _feed(seq, list(range(10)))
    assert steps[0] is SequenceStep.BASELINE
    assert set(steps[1:]) == {SequenceStep.IN_ORDER}
    counts = seq.counts()
    assert (counts.packets, counts.lost, counts.duplicates, counts.out_of_order) == (10, 0, 0, 0)
    assert counts.loss_fraction == 0.0


@pytest.mark.parametrize(
    ("bits", "values", "lost", "rollovers"),
    [
        pytest.param(8, [253, 254, 255, 0, 1, 2], 0, 1, id="u8-clean"),
        pytest.param(8, [254, 255, 2, 3], 2, 1, id="u8-gap-across"),
        pytest.param(4, [14, 15, *range(16), 0], 0, 2, id="u4-two-wraps"),
        pytest.param(16, [65534, 65535, 0], 0, 1, id="u16-clean"),
        pytest.param(16, [65535, 1], 1, 1, id="u16-gap-across"),
    ],
)
def test_rollover_at_declared_width(
    bits: int, values: list[int], lost: int, rollovers: int
) -> None:
    seq = SequenceTracker(bits)
    _feed(seq, values)
    counts = seq.counts()
    assert (counts.lost, counts.rollovers, counts.resets) == (lost, rollovers, 0)


def test_same_jump_is_rollover_or_reset_depending_on_width() -> None:
    # 255 -> 0 is the next number for an 8-bit counter but a step back for 16 bits.
    narrow = SequenceTracker(8)
    _feed(narrow, [254, 255, 0])
    wide = SequenceTracker(16)
    steps = _feed(wide, [254, 255, 0])
    assert (narrow.counts().rollovers, narrow.counts().resets) == (1, 0)
    assert steps[-1] is SequenceStep.RESET
    assert (wide.counts().rollovers, wide.counts().resets, wide.counts().lost) == (0, 1, 0)


def test_gap_counts_skipped_numbers() -> None:
    seq = SequenceTracker(16)
    steps = _feed(seq, [0, 1, 5, 6])
    assert steps[2] is SequenceStep.GAP
    counts = seq.counts()
    assert counts.lost == 3
    assert counts.loss_fraction == pytest.approx(3 / 7)


@pytest.mark.parametrize(
    ("values", "duplicates", "out_of_order", "lost"),
    [
        pytest.param([0, 1, 1, 2], 1, 0, 0, id="repeat-newest"),
        pytest.param([0, 1, 2, 3, 1], 1, 0, 0, id="repeat-older"),
        pytest.param([0, 1, 3, 2, 4], 0, 1, 0, id="swap"),
        pytest.param([0, 1, 4, 2, 3, 5], 0, 2, 0, id="two-late"),
        pytest.param([0, 1, 4, 2, 2, 5], 1, 1, 1, id="late-then-dup"),
        pytest.param([10, 9, 11], 0, 1, 0, id="before-run-start"),
    ],
)
def test_duplicates_and_out_of_order(
    values: list[int], duplicates: int, out_of_order: int, lost: int
) -> None:
    seq = SequenceTracker(16)
    _feed(seq, values)
    counts = seq.counts()
    assert (counts.duplicates, counts.out_of_order, counts.lost) == (duplicates, out_of_order, lost)
    assert counts.resets == 0


def test_late_packet_across_rollover_takes_back_loss() -> None:
    seq = SequenceTracker(8)
    steps = _feed(seq, [254, 0, 255, 1])
    assert steps[2] is SequenceStep.OUT_OF_ORDER
    counts = seq.counts()
    assert (counts.lost, counts.out_of_order, counts.rollovers) == (0, 1, 1)


def test_reorder_window_limits_late_packets() -> None:
    seq = SequenceTracker(16, reorder_window=4)
    # 100 -> 110 loses 101..109; only 106..109 are close enough to arrive late.
    steps = _feed(seq, [100, 110, 106, 105])
    assert steps[2] is SequenceStep.OUT_OF_ORDER
    assert steps[3] is SequenceStep.RESET  # Five behind is beyond the window.
    counts = seq.counts()
    assert (counts.lost, counts.out_of_order, counts.resets) == (8, 1, 1)


def test_mid_stream_reset_starts_new_run() -> None:
    seq = SequenceTracker(16)
    steps = _feed(seq, [5000, 5001, 0, 1, 2])
    assert steps[2] is SequenceStep.RESET
    counts = seq.counts()
    assert (counts.lost, counts.resets, counts.rollovers) == (0, 1, 0)


def test_half_range_step_is_a_reset() -> None:
    seq = SequenceTracker(4)
    assert _feed(seq, [0, 8]) == [SequenceStep.BASELINE, SequenceStep.RESET]


@pytest.mark.parametrize(
    ("after", "step", "resets"),
    [
        pytest.param(0, SequenceStep.RESET, 1, id="back-to-zero"),
        pytest.param(41, SequenceStep.RESET, 1, id="repeat-last"),
        pytest.param(60, SequenceStep.BASELINE, 0, id="continues-ahead"),
    ],
)
def test_restart_after_reconnect(after: int, step: SequenceStep, resets: int) -> None:
    seq = SequenceTracker(16)
    _feed(seq, [39, 40, 41])
    seq.restart()
    assert seq.observe(after) is step
    seq.observe((after + 1) % seq.modulus)
    counts = seq.counts()
    # Numbers skipped while disconnected are not counted as transport loss.
    assert (counts.resets, counts.lost) == (resets, 0)


def test_sequence_tracker_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="at least 2"):
        SequenceTracker(1)
    with pytest.raises(ValueError, match="at most 64"):
        SequenceTracker(65)
    with pytest.raises(TypeError, match="integer"):
        SequenceTracker(True)
    with pytest.raises(ValueError, match="half"):
        SequenceTracker(4, reorder_window=8)
    seq = SequenceTracker(8)
    with pytest.raises(ValueError, match="does not fit"):
        seq.observe(256)
    with pytest.raises(ValueError, match="at least 0"):
        seq.observe(-1)
    assert SequenceTracker(2).counts().reorder_window == 1
    assert SequenceTracker(64).counts().reorder_window == 32


def test_long_rolling_stream_with_known_loss() -> None:
    seq = SequenceTracker(8)
    dropped = {n for n in range(5000) if n % 97 == 3}
    _feed(seq, [n % 256 for n in range(5000) if n not in dropped])
    counts = seq.counts()
    assert counts.lost == len(dropped)
    assert counts.rollovers == 4999 // 256
    assert counts.resets == 0


# --- Config -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("config", "error", "match"),
    [
        pytest.param({"warn_after_ns": 0}, ValueError, "at least 1", id="warn-zero"),
        pytest.param({"warn_after_ns": 2 * S}, ValueError, "greater", id="warn-eq-stale"),
        pytest.param({"stale_after_ns": 1.5}, TypeError, "integer", id="stale-float"),
        pytest.param({"rate_window_ns": -1}, ValueError, "at least 1", id="window"),
        pytest.param({"reorder_window": 4}, ValueError, "requires", id="window-no-seq"),
        pytest.param({"sequence_bits": 70}, ValueError, "at most", id="bits"),
        pytest.param({"heartbeat_timeout_ns": 0}, ValueError, "at least 1", id="hb"),
        pytest.param({"reports_overflow": 1}, TypeError, "boolean", id="flag"),
    ],
)
def test_config_is_validated(config: dict[str, object], error: type[Exception], match: str) -> None:
    with pytest.raises(error, match=match):
        SourceHealthConfig(**config)  # type: ignore[arg-type]


# --- Freshness --------------------------------------------------------------


@pytest.mark.parametrize(
    ("age_ns", "freshness"),
    [
        pytest.param(0, Freshness.FRESH, id="now"),
        pytest.param(500 * MS, Freshness.FRESH, id="at-warn"),
        pytest.param(500 * MS + 1, Freshness.WARN, id="past-warn"),
        pytest.param(2 * S, Freshness.WARN, id="at-stale"),
        pytest.param(2 * S + 1, Freshness.STALE, id="past-stale"),
    ],
)
def test_threshold_boundaries(age_ns: int, freshness: Freshness) -> None:
    tracker, clock = _tracker()
    tracker.observe_packet()
    clock.advance(age_ns)
    snap = tracker.snapshot()
    assert (snap.freshness, snap.age_ns) == (freshness, age_ns)


def test_connected_but_stale_differs_from_disconnected() -> None:
    tracker, clock = _tracker()
    tracker.observe_connect()
    tracker.observe_packet()
    clock.advance(3 * S)
    stale = tracker.snapshot()
    tracker.observe_disconnect(reason="cable unplugged")
    gone = tracker.snapshot()
    assert (stale.connection, stale.status) == (ConnectionState.CONNECTED, LinkStatus.STALE)
    assert (gone.connection, gone.status) == (ConnectionState.DISCONNECTED, LinkStatus.DISCONNECTED)
    assert gone.freshness is Freshness.STALE
    assert status_text(stale) == "Connected but stale"
    assert status_text(gone) == "Disconnected (cable unplugged)"


def test_connected_silence_becomes_stale_not_waiting() -> None:
    tracker, clock = _tracker()
    tracker.observe_connect()
    clock.advance(2 * S)
    assert tracker.snapshot().status is LinkStatus.WAITING
    clock.advance(1)
    snap = tracker.snapshot()
    assert (snap.status, snap.age_ns) == (LinkStatus.STALE, None)


def test_decode_errors_count_but_do_not_refresh() -> None:
    tracker, clock = _tracker(sequence_bits=8)
    tracker.observe_packet(sequence=0)
    clock.advance(1 * S)
    tracker.observe_decode_error(message="bad checksum")
    tracker.observe_decode_error(message="short frame")
    snap = tracker.snapshot()
    assert (snap.decode_errors, snap.last_decode_error) == (2, "short frame")
    assert (snap.freshness, snap.age_ns, snap.packets) == (Freshness.WARN, 1 * S, 1)
    # Corruption is not loss: the next good packet continues the sequence.
    tracker.observe_packet(sequence=1)
    assert tracker.snapshot().sequence is not None
    assert tracker.snapshot().sequence.lost == 0  # type: ignore[union-attr]


# --- Rate -------------------------------------------------------------------


def test_rate_needs_a_full_window() -> None:
    tracker, clock = _tracker(rate_window_ns=1 * S)
    for _ in range(5):
        tracker.observe_packet()
        clock.advance(100 * MS)
    assert tracker.snapshot().rate_hz is None
    for _ in range(5):
        tracker.observe_packet()
        clock.advance(100 * MS)
    # Packets at 0.0 .. 0.9 s, snapshot at 1.0 s: the window (0, 1 s] holds 9.
    assert tracker.snapshot().rate_hz == pytest.approx(9.0)
    clock.advance(5 * S)
    assert tracker.snapshot().rate_hz == 0.0


def test_mixed_rate_window() -> None:
    tracker, clock = _tracker(rate_window_ns=2 * S)
    start = clock.now
    for index in range(20):  # 20 Hz for one second, then 5 Hz.
        tracker.observe_packet(start + index * 50 * MS)
    for index in range(5):
        tracker.observe_packet(start + S + index * 200 * MS)
    snap = tracker.snapshot(start + 2 * S)
    assert snap.rate_hz == pytest.approx((20 - 1 + 5) / 2)


# --- Heartbeat --------------------------------------------------------------


def test_heartbeat_timeout_and_recovery() -> None:
    tracker, clock = _tracker(heartbeat_timeout_ns=1 * S)
    assert tracker.snapshot().heartbeat is HeartbeatState.WAITING
    tracker.observe_connect()
    tracker.observe_heartbeat()
    clock.advance(1 * S)
    assert tracker.snapshot().heartbeat is HeartbeatState.ALIVE  # Exactly at the timeout.
    clock.advance(1)
    timed_out = tracker.snapshot()
    assert (timed_out.heartbeat, timed_out.heartbeat_age_ns) == (HeartbeatState.TIMED_OUT, S + 1)
    assert timed_out.heartbeat_timeouts == 0
    tracker.observe_heartbeat()
    recovered = tracker.snapshot()
    assert (recovered.heartbeat, recovered.heartbeat_timeouts) == (HeartbeatState.ALIVE, 1)


def test_heartbeat_missing_after_connect_times_out() -> None:
    tracker, clock = _tracker(heartbeat_timeout_ns=1 * S)
    tracker.observe_connect()
    clock.advance(1 * S)
    assert tracker.snapshot().heartbeat is HeartbeatState.WAITING
    clock.advance(1)
    snap = tracker.snapshot()
    assert (snap.heartbeat, snap.heartbeat_age_ns) == (HeartbeatState.TIMED_OUT, None)


def test_heartbeat_without_connect_events() -> None:
    tracker, clock = _tracker(heartbeat_timeout_ns=1 * S)
    tracker.observe_heartbeat()
    clock.advance(1 * S)
    assert tracker.snapshot().heartbeat is HeartbeatState.ALIVE
    clock.advance(1 * S)
    tracker.observe_heartbeat()
    assert tracker.snapshot().heartbeat_timeouts == 1


# --- Reconnect --------------------------------------------------------------


def test_reconnect_with_sequence_reset() -> None:
    tracker, clock = _tracker(sequence_bits=16, heartbeat_timeout_ns=1 * S)
    tracker.observe_connect()
    for number in range(100, 110):
        tracker.observe_packet(sequence=number, device_time_ns=number * MS)
        tracker.observe_heartbeat()
        clock.advance(10 * MS)
    tracker.observe_disconnect(reason="usb reset")
    clock.advance(5 * S)
    down = tracker.snapshot()
    assert down.status is LinkStatus.DISCONNECTED
    assert down.connected_since_ns is None
    assert down.heartbeat is HeartbeatState.TIMED_OUT

    tracker.observe_connect()
    waiting = tracker.snapshot()
    assert (waiting.status, waiting.age_ns, waiting.rate_hz) == (LinkStatus.WAITING, None, None)
    assert waiting.disconnect_reason is None
    assert waiting.heartbeat is HeartbeatState.WAITING
    # The device rebooted: counter and device clock start over.
    for number in range(3):
        assert tracker.observe_packet(sequence=number, device_time_ns=number * MS) in (
            SequenceStep.RESET,
            SequenceStep.IN_ORDER,
        )
        tracker.observe_heartbeat()
        clock.advance(10 * MS)
    back = tracker.snapshot()
    assert back.status is LinkStatus.LIVE
    assert (back.connects, back.disconnects, back.packets) == (2, 1, 13)
    assert back.sequence is not None
    assert (back.sequence.resets, back.sequence.lost, back.sequence.duplicates) == (1, 0, 0)
    assert back.heartbeat is HeartbeatState.ALIVE
    assert back.heartbeat_timeouts == 0  # The outage is a disconnect, not a heartbeat timeout.
    assert (back.device_time_ns, back.device_time_regressions) == (2 * MS, 0)


def test_device_time_regression_within_a_connection() -> None:
    tracker, _ = _tracker()
    assert tracker.snapshot().device_time_regressions is None
    tracker.observe_packet(device_time_ns=5 * MS)
    tracker.observe_packet(device_time_ns=4 * MS)
    tracker.observe_packet()  # No device time on this packet.
    snap = tracker.snapshot()
    assert (snap.device_time_ns, snap.device_time_regressions) == (4 * MS, 1)


# --- Unknown metadata -------------------------------------------------------


def test_absent_metadata_is_unknown_not_zero() -> None:
    tracker, _ = _tracker(reports_decode_errors=False)
    tracker.observe_packet()
    snap = tracker.snapshot()
    assert snap.connection is ConnectionState.UNKNOWN
    assert snap.sequence is None
    assert snap.decode_errors is None
    assert snap.overflow_dropped is None
    assert (snap.heartbeat, snap.heartbeat_timeouts) == (HeartbeatState.UNKNOWN, None)
    assert snap.heartbeat_timeout_ns is None
    assert (snap.device_time_ns, snap.device_time_regressions) == (None, None)
    assert snap.reported is None
    assert snap.status is LinkStatus.LIVE
    rows = dict(format_source_health(snap))
    assert rows["Lost packets"] == "unknown (no sequence numbers)"
    assert rows["Duplicates / out of order"] == "unknown (no sequence numbers)"
    assert rows["Decode errors"] == "unknown (not reported)"
    assert rows["Dashboard drops"] == "unknown (not reported)"
    assert rows["Heartbeat"] == "unknown (source has no heartbeat)"
    assert rows["Device time"] == "unknown (no device timestamps)"
    assert rows["Reported status"] == "unknown (nothing reported by source)"
    assert rows["Connection"] == "unknown (source reports no connection events)"
    assert "Sequence" not in rows


def test_empty_tracker_snapshot() -> None:
    tracker, _ = _tracker()
    snap = tracker.snapshot()
    assert (snap.freshness, snap.status, snap.packets) == (Freshness.NO_DATA, LinkStatus.WAITING, 0)
    assert (snap.age_ns, snap.rate_hz) == (None, None)
    assert snap.decode_errors == 0  # Declared by default, and none seen.
    rows = dict(format_source_health(snap))
    assert rows["Status"] == "Waiting for data"
    assert rows["Last packet"].startswith("none received (no data;")


@pytest.mark.parametrize(
    ("method", "match"),
    [
        pytest.param("sequence", "sequence_bits", id="sequence"),
        pytest.param("heartbeat", "heartbeat_timeout_ns", id="heartbeat"),
        pytest.param("decode", "decode errors", id="decode"),
        pytest.param("overflow", "overflow", id="overflow"),
    ],
)
def test_undeclared_metadata_is_rejected(method: str, match: str) -> None:
    tracker, _ = _tracker(reports_decode_errors=False)
    with pytest.raises(ValueError, match=match):
        if method == "sequence":
            tracker.observe_packet(sequence=1)
        elif method == "heartbeat":
            tracker.observe_heartbeat()
        elif method == "decode":
            tracker.observe_decode_error()
        else:
            tracker.observe_overflow(1)


# --- Overflow and reported status ------------------------------------------


def test_overflow_is_separate_from_transport_loss() -> None:
    tracker, _ = _tracker(sequence_bits=8, reports_overflow=True)
    for number in range(4):
        tracker.observe_packet(sequence=number)
    tracker.observe_overflow(25)
    tracker.observe_overflow(5)
    snap = tracker.snapshot()
    assert snap.overflow_dropped == 30
    assert snap.sequence is not None and snap.sequence.lost == 0
    with pytest.raises(ValueError, match="at least 1"):
        tracker.observe_overflow(0)


def test_reported_status_is_kept_apart_from_inference() -> None:
    tracker, clock = _tracker()
    tracker.observe_reported_status({"sd_ok": True, "sd_dropped": 4, "fw": "1.2"})
    clock.advance(250 * MS)
    tracker.observe_reported_status({"sd_ok": False, "fw": "1.2"})
    clock.advance(250 * MS)
    snap = tracker.snapshot()
    assert snap.reported is not None
    assert snap.reported.fields == (("fw", "1.2"), ("sd_ok", False))  # Replaced, not merged.
    assert snap.overflow_dropped is None  # Reported counts never feed inferred ones.
    rows = format_source_health(snap)
    assert ("Reported status", "received 0.250 s ago") in rows
    assert rows[-2:] == [("Reported: fw", "1.2"), ("Reported: sd_ok", "False")]


@pytest.mark.parametrize(
    ("fields", "error"),
    [
        pytest.param({"": 1}, ValueError, id="empty-name"),
        pytest.param({"x": None}, TypeError, id="none-value"),
        pytest.param({"x": [1]}, TypeError, id="list-value"),
    ],
)
def test_reported_status_is_validated(fields: dict[str, object], error: type[Exception]) -> None:
    tracker, _ = _tracker()
    with pytest.raises(error):
        tracker.observe_reported_status(fields)  # type: ignore[arg-type]


# --- Clock and immutability -------------------------------------------------


def test_arrival_times_must_not_go_backwards() -> None:
    tracker, _ = _tracker()
    tracker.observe_packet(10 * S)
    with pytest.raises(ValueError, match="backwards"):
        tracker.observe_packet(9 * S)
    with pytest.raises(ValueError, match="earlier"):
        tracker.snapshot(9 * S)
    with pytest.raises(TypeError, match="integer"):
        tracker.observe_packet(device_time_ns=1.5)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="integer"):
        tracker.observe_packet(True)


def test_default_clock_and_source_id() -> None:
    tracker = SourceHealthTracker("live")
    tracker.observe_packet()
    snap = tracker.snapshot()
    assert snap.source_id == tracker.source_id == "live"
    assert tracker.config == SourceHealthConfig()
    assert snap.age_ns is not None and snap.age_ns >= 0
    with pytest.raises(ValueError, match="source_id"):
        SourceHealthTracker(" ")


def test_snapshot_is_frozen_and_stable() -> None:
    tracker, clock = _tracker(sequence_bits=8)
    tracker.observe_packet(sequence=0)
    snap = tracker.snapshot()
    tracker.observe_packet(sequence=5)
    clock.advance(S)
    assert snap.packets == 1
    assert snap.sequence is not None and snap.sequence.lost == 0
    with pytest.raises(FrozenInstanceError):
        snap.packets = 2  # type: ignore[misc]


# --- Text -------------------------------------------------------------------


def test_format_rows_for_a_live_source() -> None:
    tracker, clock = _tracker(sequence_bits=8, heartbeat_timeout_ns=S, reports_overflow=True)
    tracker.observe_connect()
    for number in (0, 1, 2, 4, 4):
        clock.advance(250 * MS)
        tracker.observe_packet(sequence=number, device_time_ns=1_500 * MS)
        tracker.observe_heartbeat()
    tracker.observe_decode_error(message="bad crc")
    rows = format_source_health(tracker.snapshot())
    assert rows == [
        ("Status", "Connected, live"),
        ("Connection", "connected for 1.250 s (1 connects, 0 disconnects)"),
        (
            "Last packet",
            "0.000 s ago (fresh; late after 0.500 s, stale after 2.000 s)",
        ),
        ("Packet rate", "4.0 packets/s over 1.000 s"),
        ("Packets received", "5"),
        ("Lost packets", "1 (20.00% of expected)"),
        ("Duplicates / out of order", "1 duplicates, 0 out of order"),
        ("Sequence", "0 rollovers, 0 resets (8-bit counter, 5 numbered packets)"),
        ("Decode errors", "1 (last: bad crc)"),
        ("Dashboard drops", "0"),
        (
            "Heartbeat",
            "alive: last 0.000 s ago (timeout 1.000 s, 0 recovered timeouts)",
        ),
        ("Device time", "1.500 s (0 regressions)"),
        ("Reported status", "unknown (nothing reported by source)"),
    ]


@pytest.mark.parametrize(
    ("connect", "advance", "expected"),
    [
        pytest.param(True, 1 * S, "Connected, data late", id="late-connected"),
        pytest.param(False, 1 * S, "Data late (connection unknown)", id="late-unknown"),
        pytest.param(False, 3 * S, "Stale (connection unknown)", id="stale-unknown"),
        pytest.param(False, 0, "Live (connection unknown)", id="live-unknown"),
    ],
)
def test_status_text_names_every_state(connect: bool, advance: int, expected: str) -> None:
    tracker, clock = _tracker()
    if connect:
        tracker.observe_connect()
    tracker.observe_packet()
    clock.advance(advance)
    assert status_text(tracker.snapshot()) == expected


def test_format_after_reconnect_and_disconnect() -> None:
    tracker, clock = _tracker(heartbeat_timeout_ns=S)
    tracker.observe_connect()
    tracker.observe_packet()
    tracker.observe_disconnect()
    tracker.observe_connect()
    rows = dict(format_source_health(tracker.snapshot()))
    assert rows["Status"] == "Connected, waiting for data"
    assert rows["Last packet"].startswith("none since connect (no data;")
    assert rows["Packet rate"] == "measuring (needs a full 1.000 s window)"
    assert rows["Heartbeat"].startswith("waiting: none yet")
    tracker.observe_disconnect()
    clock.advance(S)
    rows = dict(format_source_health(tracker.snapshot()))
    assert rows["Status"] == "Disconnected"
    assert rows["Connection"] == "disconnected (2 connects, 2 disconnects)"


@pytest.mark.parametrize(
    ("ns", "text"),
    [
        pytest.param(0, "0.000 s", id="zero"),
        pytest.param(1_250 * MS, "1.250 s", id="ms"),
        pytest.param(90 * S, "90.000 s", id="long"),
    ],
)
def test_format_duration(ns: int, text: str) -> None:
    assert format_duration(ns) == text


def test_all_text_is_ascii() -> None:
    tracker, _ = _tracker(sequence_bits=8, heartbeat_timeout_ns=S)
    tracker.observe_connect()
    tracker.observe_packet(sequence=1)
    for label, value in format_source_health(tracker.snapshot()):
        assert (label + value).isascii()
