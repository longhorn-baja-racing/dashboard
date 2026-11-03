"""Write a deterministic, synthetic lbr-csv export for demos and tests.

The file follows the firmware's lbr-csv version 1 format exactly (column
order, exact 6-decimal times, shortest round-trip f32 values, empty values for
unavailable samples) and uses real catalog signal names and frame rates, but
the values are made up. A TEXT row inside the file says so.

    python scripts/make_lbr_demo_csv.py [output.csv]
"""

from __future__ import annotations

import csv
import math
import random
import struct
import sys
import zlib
from pathlib import Path
from typing import TextIO

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "sample_data" / "lbr_demo_session.csv"
COLUMNS = ["t_s", "kind", "node", "name", "value", "unit", "status", "synced", "seq"]

START_US = 1_000_000
DURATION_S = 10
FAULT_START_US = 5_000_000
FAULT_END_US = 6_500_000
LOSS_US = 8_200_000


def csv_time(t_us: int) -> str:
    seconds, micros = divmod(t_us, 1_000_000)
    return f"{seconds}.{micros:06d}"


def f32_bits(value: float) -> int:
    return struct.unpack("<I", struct.pack("<f", value))[0]


def csv_f32(value: float) -> str:
    """Same rule as the firmware exporter: shortest text that reads back as the f32."""

    bits = f32_bits(value)
    single = struct.unpack("<f", struct.pack("<I", bits))[0]
    for precision in range(6, 10):
        text = f"{single:.{precision}g}"
        if f32_bits(float(text)) == bits:
            return text
    return f"{single:.9g}"


class Writer:
    def __init__(self, handle: TextIO) -> None:
        self.out = csv.writer(handle, lineterminator="\n")
        self.out.writerow(COLUMNS)
        self.frames = 0

    def row(
        self,
        t_us: int,
        kind: str,
        node: str,
        name: str,
        value: object = "",
        unit: str = "",
        status: str = "",
        synced: object = "",
        seq: object = "",
    ) -> None:
        self.out.writerow([csv_time(t_us), kind, node, name, value, unit, status, synced, seq])

    def sample(
        self,
        t_us: int,
        node: str,
        name: str,
        value: object,
        unit: str,
        seq: int,
        *,
        status: str = "ok",
        synced: int = 1,
    ) -> None:
        self.row(t_us, "sample", node, name, value, unit, status, synced, seq)


def generate(path: Path) -> None:
    rng = random.Random(2026)
    config = "node=central\nnode_id=6\ncan_bitrate=500000\n"
    with path.open("w", newline="", encoding="ascii") as handle:
        w = Writer(handle)
        w.row(0, "format", "", "lbr_csv_version", 1)
        session = {
            "format_version": 1,
            "protocol_version": 4,
            "fw_version": 512,
            "catalog_hash": 2862307193,
            "config_crc": zlib.crc32(config.encode("ascii")),
            "reset_reason": 1,
            "boot_count": 7,
            "start_time_us": START_US,
        }
        for name, value in session.items():
            w.row(START_US, "session", "central", name, value)
        for line in config.splitlines():
            key, _, value = line.partition("=")
            w.row(START_US, "config", "central", key, value)
        w.row(
            START_US,
            "text",
            "central",
            "text",
            "synthetic demo session from scripts/make_lbr_demo_csv.py - not vehicle data",
        )
        w.row(START_US, "event", "central", "SESSION_START", 0, "", "ok", 1, 0)

        # Stress node: a few samples before it is synchronized (its own clock).
        for index in range(5):
            w.sample(
                12_345_000 + index * 10_000,
                "stress",
                "STRESS_1",
                csv_f32(80.0 + index),
                "ue",
                index,
                synced=0,
            )
        w.row(START_US + 300_000, "event", "stress", "TIME_SYNCED", 0, "", "ok", 1, 1)

        fuel = 92.0
        cvt_temp = 41.0
        brake_temp = [38.0, 39.0, 37.5]
        lat, lon = 302849870, -977354700
        events_seq = 2
        lost = set(range(int(LOSS_US / 10_000), int(LOSS_US / 10_000) + 3))

        for tick in range(DURATION_S * 100 + 1):
            t = START_US + tick * 10_000
            seq = t // 10_000
            seconds = tick / 100.0
            if tick in lost:
                continue  # the logger queue overflowed; the LOSS record follows
            if tick == max(lost) + 1:
                w.row(t, "loss", "central", "dropped_records", 12)
                w.row(t, "loss", "central", "dropped_bytes", 264)
            # Driver inputs and powertrain, 100 Hz.
            gas = max(0.0, min(100.0, 55 + 45 * math.sin(seconds * 0.9) + rng.gauss(0, 1.5)))
            brake = max(0.0, min(100.0, 40 * max(0.0, -math.sin(seconds * 0.9 + 0.4)) - 5))
            steer = 35 * math.sin(seconds * 0.55) + rng.gauss(0, 0.6)
            engine = 1800 + 18 * gas + rng.gauss(0, 25)
            cvt = max(0.0, engine * (0.35 + 0.004 * gas) + rng.gauss(0, 15))
            w.sample(t + 120, "driver_input", "BRAKE_PEDAL", csv_f32(brake), "%", seq)
            w.sample(t + 120, "driver_input", "GAS_PEDAL", csv_f32(gas), "%", seq)
            w.sample(t + 120, "driver_input", "STEERING_ANGLE", csv_f32(steer), "deg", seq)
            w.sample(t + 240, "powertrain", "ENGINE_RPM", csv_f32(engine), "rpm", seq)
            w.sample(t + 240, "powertrain", "CVT_RPM", csv_f32(cvt), "rpm", seq)
            # Chassis IMU, 100 Hz.
            ax = 0.06 * (gas - 50) - 0.12 * brake + rng.gauss(0, 0.25)
            ay = 0.12 * steer * (cvt / 2500.0) + rng.gauss(0, 0.25)
            w.sample(t + 360, "imu", "IMU_CH_ACCEL_X", csv_f32(ax), "m/s^2", seq)
            w.sample(t + 360, "imu", "IMU_CH_ACCEL_Y", csv_f32(ay), "m/s^2", seq)
            w.frames += 4

            if tick % 10 == 0:
                # Temperatures and GPS, 10 Hz.
                cvt_temp += 0.04 + 0.0004 * gas
                w.sample(t + 480, "temperature", "TEMP_CVT", csv_f32(cvt_temp), "degC", seq)
                for corner, name in enumerate(("TEMP_BRAKE_FL", "TEMP_BRAKE_FR", "TEMP_BRAKE_BL")):
                    brake_temp[corner] += 0.02 * brake - 0.03
                    if name == "TEMP_BRAKE_BL" and FAULT_START_US <= t < FAULT_END_US:
                        w.sample(
                            t + 480, "temperature", name, "", "degC", seq, status="SENSOR_FAULT"
                        )
                    else:
                        w.sample(
                            t + 480, "temperature", name, csv_f32(brake_temp[corner]), "degC", seq
                        )
                w.sample(
                    t + 480, "temperature", "TEMP_BRAKE_BR", "", "degC", seq, status="NOT_FITTED"
                )
                speed = cvt / 220.0
                course = (90 + seconds * 12) % 360
                lat += int(speed * math.cos(math.radians(course)) * 9)
                lon += int(speed * math.sin(math.radians(course)) * 10)
                w.sample(t + 600, "central", "GPS_LATITUDE", lat, "deg*1e-7", seq)
                w.sample(t + 600, "central", "GPS_LONGITUDE", lon, "deg*1e-7", seq)
                w.sample(t + 620, "central", "GPS_SPEED", csv_f32(speed), "m/s", seq)
                w.sample(t + 620, "central", "GPS_FIX", 3, "", seq)
                w.sample(t + 620, "central", "GPS_SATELLITES", 10 + (tick // 300) % 2, "count", seq)
                w.frames += 5

            if tick % 100 == 0:
                # Fuel and heartbeats, 1 Hz.
                fuel -= 0.15
                w.sample(t + 700, "powertrain", "FUEL_LEVEL", csv_f32(fuel), "%", seq)
                for node in ("driver_input", "powertrain", "imu", "temperature", "stress"):
                    faulted = node == "temperature" and FAULT_START_US <= t < FAULT_END_US
                    w.row(t + 800, "heartbeat", node, "state", 2 if faulted else 1)
                    w.row(
                        t + 800,
                        "heartbeat",
                        node,
                        "err_flags",
                        2 if faulted else 0,
                        "bitmask",
                        "SENSOR" if faulted else "ok",
                    )
                w.frames += 6

            if t == FAULT_START_US:
                w.row(
                    t + 900, "fault", "temperature", "SENSOR_INIT_FAIL", 2, "", "ok", 1, events_seq
                )
                events_seq += 1
            if t == FAULT_END_US:
                w.row(
                    t + 900, "event", "temperature", "SENSOR_RECOVERED", 2, "", "ok", 1, events_seq
                )
                events_seq += 1

        end_us = START_US + DURATION_S * 1_000_000 + 1_000
        w.row(end_us, "end", "central", "records_written", w.frames)
        integrity = {
            "records": w.frames + 3,
            "corrupt_regions": 0,
            "skipped_bytes": 0,
            "truncated_tail": 0,
            "time_sync_frames": DURATION_S * 10,
            "time_follow_up_frames": DURATION_S * 10,
            "catalog_checked": 1,
        }
        for name, value in integrity.items():
            w.row(end_us, "integrity", "central", name, value)


def main(argv: list[str]) -> int:
    output = Path(argv[1]) if len(argv) > 1 else DEFAULT_OUTPUT
    generate(output)
    print(f"wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
