"""In-memory serial line and firmware stream builders for the serial source tests.

``FakeLine`` is the far end of a cable: tests push bytes (in any chunking),
unplug it or make the next open fail. Every ``FakePort`` opened on it reads
from the same script. A port has ``open``, ``read`` and ``close`` only and
records every attribute the source touches, so tests can prove nothing tries
to write.
"""

from __future__ import annotations

import struct
from collections import deque
from pathlib import Path
from threading import Condition

from lbr_dashboard.importers.lbr_stream import build_packet, cobs_encode
from lbr_dashboard.importers.lbrlog import REC_CAN_FRAME, REC_KEY, REC_TEXT, REC_VALUE
from lbr_dashboard.importers.serial_source import SerialDisconnected, SerialPortError

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "lbr"
GOLDEN_STREAM = bytes.fromhex((FIXTURES / "stream_usb.bin.hex").read_text())
CENTRAL = 6
USB = 1


class FakeLine:
    """Bytes the device sends, shared by every port opened on this line."""

    def __init__(self, *chunks: bytes, chunk_size: int | None = None) -> None:
        self._condition = Condition()
        self._pending: deque[bytes] = deque(chunk for chunk in chunks if chunk)
        self.chunk_size = chunk_size
        self.unplugged = False
        self.fail_open: str | None = None
        self.opened = 0
        self.closed = 0
        self.ports: list[FakePort] = []

    @property
    def open_ports(self) -> int:
        return self.opened - self.closed

    @property
    def pending(self) -> int:
        with self._condition:
            return sum(len(chunk) for chunk in self._pending)

    def send(self, data: bytes) -> None:
        with self._condition:
            self._pending.append(data)
            self._condition.notify_all()

    def unplug(self) -> None:
        with self._condition:
            self.unplugged = True
            self._condition.notify_all()

    def replug(self) -> None:
        with self._condition:
            self.unplugged = False

    def opener(self, name: str) -> FakePort:
        port = FakePort(name, self)
        self.ports.append(port)
        return port

    def _read(self, max_bytes: int, timeout_s: float) -> bytes:
        with self._condition:
            if not self._pending and not self.unplugged:
                self._condition.wait(timeout_s)
            if self.unplugged:
                raise SerialDisconnected("device unplugged")
            if not self._pending:
                return b""
            limit = max_bytes if self.chunk_size is None else min(max_bytes, self.chunk_size)
            chunk = self._pending.popleft()
            if len(chunk) > limit:
                self._pending.appendleft(chunk[limit:])
                chunk = chunk[:limit]
            return chunk


class FakePort:
    """Read-only fake port; ``touched`` lists every attribute used on it."""

    def __init__(self, name: str, line: FakeLine) -> None:
        self._name = name
        self._line = line
        self._open = False
        self.touched: set[str] = set()

    def __getattribute__(self, attribute: str):
        if not attribute.startswith("_") and attribute != "touched":
            object.__getattribute__(self, "touched").add(attribute)
        return object.__getattribute__(self, attribute)

    @property
    def name(self) -> str:
        return self._name

    def open(self) -> None:
        line = self._line
        if line.fail_open is not None:
            raise SerialPortError(line.fail_open)
        if line.unplugged:
            raise SerialPortError("the port does not exist")
        self._open = True
        line.opened += 1

    def read(self, max_bytes: int, timeout_s: float) -> bytes:
        if not self._open:
            raise SerialPortError("the port is not open")
        return self._line._read(max_bytes, timeout_s)

    def close(self) -> None:
        if self._open:
            self._open = False
            self._line.closed += 1


# -- firmware stream builders -------------------------------------------------


def can_frame(t_us: int, can_id: int, data: bytes, flags: int = 0) -> bytes:
    """A 22-byte CAN_FRAME record payload."""

    return struct.pack("<QIBB", t_us, can_id, flags, len(data)) + data.ljust(8, b"\0")


def heartbeat(t_us: int, node: int, state: int = 1, err_flags: int = 0) -> bytes:
    return can_frame(t_us, 0x700 + node, bytes((state, err_flags)))


def driver_inputs(k: int, brake: float, gas: float, steer: float) -> bytes:
    """DRIVER_INPUTS (0x100, node 3, 100 Hz) for grid index ``k``; time follows from ``k``."""

    data = struct.pack(
        "<hhhH", round(brake / 0.01), round(gas / 0.01), round(steer / 0.1), k & 0xFFFF
    )
    return can_frame(k * 10_000 + 450, 0x100, data)


def key_record(key_id: int, name: str, value_type: int = 1, unit: int = 0) -> bytes:
    return struct.pack("<HBB", key_id, value_type, unit) + name.encode("ascii")


def value_record(t_us: int, key_id: int, value: float, status: int = 0) -> bytes:
    bits = 0 if status else struct.unpack("<I", struct.pack("<f", value))[0]
    return struct.pack("<QHBI", t_us, key_id, status, bits)


def packet(seq: int, t_us: int, records: list[tuple[int, bytes]], source: int = CENTRAL) -> bytes:
    """One packet as it appears on the USB byte stream: COBS frame and 0x00."""

    return cobs_encode(build_packet(USB, source, seq, t_us, records)) + b"\0"


def drive_stream(
    first_seq: int,
    first_k: int,
    count: int,
    *,
    with_heartbeat: bool = True,
    output: bool = True,
) -> list[bytes]:
    """``count`` packets of a running car: pedals each tick, heartbeats, an output and text."""

    packets = []
    for i in range(count):
        k = first_k + i
        records = [(REC_CAN_FRAME, driver_inputs(k, 10 + i * 0.5, 50 - i * 0.25, -3 + i * 0.1))]
        if with_heartbeat and i % 5 == 0:
            records.append((REC_CAN_FRAME, heartbeat(k * 10_000 + 600, 3)))
        if output:
            if i % 4 == 0:
                records.append((REC_KEY, key_record(7, "Vehicle/SpeedKph")))
            records.append((REC_VALUE, value_record(k * 10_000 + 700, 7, 20.0 + i)))
        if i % 7 == 3:
            records.append((REC_TEXT, f"I ({k}) central: tick {i}".encode("ascii")))
        packets.append(packet(first_seq + i, k * 10_000 + 900, records))
    return packets
