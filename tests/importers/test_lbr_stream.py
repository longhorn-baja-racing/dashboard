"""COBS framing and transport packets of the firmware's live links (docs/protocol.md)."""

from __future__ import annotations

import hashlib
import json
import random
import struct
import zlib
from pathlib import Path

import pytest

from lbr_dashboard.importers.lbr_stream import (
    MAX_FRAME,
    FrameSplitter,
    LinkSequence,
    PacketError,
    SeqStep,
    build_packet,
    cobs_decode,
    cobs_encode,
    parse_packet,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "lbr"
STREAM = bytes.fromhex((FIXTURES / "stream_usb.bin.hex").read_text())
EXPECTED = json.loads((FIXTURES / "stream_usb.json").read_text())


def decode_all(data: bytes, chunks: list[int] | None = None):
    splitter = FrameSplitter()
    packets, bad = [], []
    pieces = [data] if chunks is None else _split(data, chunks)
    for piece in pieces:
        for frame in splitter.feed(piece):
            try:
                packets.append(parse_packet(cobs_decode(frame)))
            except PacketError as error:
                bad.append(error.reason)
    return packets, bad, splitter


def _split(data: bytes, sizes: list[int]) -> list[bytes]:
    out, pos, index = [], 0, 0
    while pos < len(data):
        size = sizes[index % len(sizes)]
        out.append(data[pos : pos + size])
        pos += size
        index += 1
    return out


def test_fixture_is_the_firmware_capture() -> None:
    digest = hashlib.sha256(STREAM).hexdigest()
    assert digest == "ffccd937c65006dd22863f256228f17bde595ad0528ed7d2cf2b7adb25bb5473"


def test_golden_usb_stream_matches_the_firmware_decode() -> None:
    packets, bad, _ = decode_all(STREAM)
    assert len(bad) == EXPECTED["bad_frames"] == 1  # the ESP32 boot ROM banner
    assert len(packets) == len(EXPECTED["packets"])
    for actual, wanted in zip(packets, EXPECTED["packets"]):
        assert (actual.link, actual.source, actual.seq, actual.t_us) == (
            wanted["link"],
            wanted["source"],
            wanted["seq"],
            wanted["t_us"],
        )
        assert [(t, len(p)) for t, p in actual.records] == [
            (r["type"], r["len"]) for r in wanted["records"]
        ]
    text = packets[1].records[0][1].decode("ascii")
    assert text == EXPECTED["packets"][1]["records"][0]["text"]


@pytest.mark.parametrize("sizes", [[1], [2, 3], [7], [64], [5, 1, 300]])
def test_any_chunking_gives_the_same_packets(sizes: list[int]) -> None:
    whole, bad_whole, _ = decode_all(STREAM)
    parts, bad_parts, splitter = decode_all(STREAM, sizes)
    assert parts == whole and bad_parts == bad_whole
    assert splitter.pending == 0


def test_cobs_round_trip_including_long_runs_and_zeros() -> None:
    rng = random.Random(4)
    samples = [b"", b"\0", b"\0\0", bytes(range(1, 255)), bytes(range(1, 256)) * 3]
    samples += [bytes(rng.randrange(256) for _ in range(rng.randrange(1, 1100))) for _ in range(50)]
    for data in samples:
        encoded = cobs_encode(data)
        assert 0 not in encoded
        assert cobs_decode(encoded) == data


def test_corruption_is_rejected_and_the_next_frame_resynchronizes() -> None:
    good = cobs_encode(build_packet(1, 6, 10, 1000, [(6, b"hello")])) + b"\0"
    nxt = cobs_encode(build_packet(1, 6, 11, 1001, [(6, b"next")])) + b"\0"
    flipped = bytearray(good)
    flipped[10] ^= 0x40
    torn = good[: len(good) // 2] + b"\0"
    stream = b"garbage text\r\n" + bytes(flipped) + torn + good + nxt
    packets, bad, _ = decode_all(stream)
    assert [p.seq for p in packets] == [10, 11]
    # Text has no delimiter, so it and the bit-flipped frame are one bad frame;
    # the torn frame is the other. Both good packets after them survive.
    assert len(bad) == 2


def test_packet_checks_name_the_reason() -> None:
    body = build_packet(1, 6, 1, 5, [(6, b"x")])
    with pytest.raises(PacketError) as short:
        parse_packet(body[:10])
    assert short.value.reason == "short"
    with pytest.raises(PacketError) as version:
        wrong = b"\x02" + body[1:-4]
        parse_packet(wrong + struct.pack("<I", zlib.crc32(wrong)))
    assert version.value.reason == "version"
    with pytest.raises(PacketError) as crc:
        parse_packet(body[:-1] + bytes((body[-1] ^ 1,)))
    assert crc.value.reason == "crc"
    lying = bytearray(body[:-4])
    lying[3] = 2  # claims two records
    with pytest.raises(PacketError) as count:
        parse_packet(bytes(lying) + struct.pack("<I", zlib.crc32(lying)))
    assert count.value.reason == "count"
    overrun = bytearray(body[:-4])
    overrun[17] = 200  # record length past the end
    with pytest.raises(PacketError) as framing:
        parse_packet(bytes(overrun) + struct.pack("<I", zlib.crc32(overrun)))
    assert framing.value.reason == "framing"
    with pytest.raises(PacketError) as cobs:
        cobs_decode(b"\x05ab")
    assert cobs.value.reason == "cobs"


def test_frames_without_a_delimiter_are_bounded_and_counted_once() -> None:
    splitter = FrameSplitter()
    good = cobs_encode(build_packet(1, 6, 1, 5, [(6, b"x")])) + b"\0"
    noise = b"\x01" * (MAX_FRAME * 3)
    frames = []
    for start in range(0, len(noise), 500):
        frames += splitter.feed(noise[start : start + 500])
        assert splitter.pending <= MAX_FRAME
    frames += splitter.feed(b"\0" + good)
    assert splitter.oversize == 1
    assert splitter.discarded_bytes == len(noise)
    assert [parse_packet(cobs_decode(f)).seq for f in frames] == [1]


def test_link_sequence_counts_gaps_duplicates_rollover_and_restarts() -> None:
    seq = LinkSequence()
    steps = [seq.observe(n) for n in (0xFFFFFFFE, 0xFFFFFFFF, 0, 0, 4, 2)]
    assert [s.step for s in steps] == [
        SeqStep.FIRST,
        SeqStep.NEXT,
        SeqStep.NEXT,
        SeqStep.DUPLICATE,
        SeqStep.GAP,
        SeqStep.BACKWARD,
    ]
    assert steps[4].lost == 3
