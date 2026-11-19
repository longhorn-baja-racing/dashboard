"""Generate deterministic, mixed-rate lbr-csv sessions for performance work.

The data is synthetic but shaped like a real DAQ export: 24 IMU signals at
100 Hz on four corner nodes, 9 temperature/GPS signals at 10 Hz and 2 signals at
1 Hz, with per-sample timing jitter (irregular, slightly out of order across
channels), periodic SENSOR_FAULT gaps on one node, heartbeats, and one
single-sample spike for checking that plot decimation keeps narrow extrema.

    python scripts/make_bench_session.py --preset standard build/bench/standard.csv

Presets: smoke (30 s, used by tests), standard (10 min), long (2 h). The same
preset always produces the same bytes.
"""

from __future__ import annotations

import argparse
import math
from collections.abc import Sequence
from pathlib import Path

PRESETS = {"smoke": 30, "standard": 600, "long": 7200}
HEADER = "t_s,kind,node,name,value,unit,status,synced,seq\n"
START_US = 1_000_000
JITTER_US = 300
SPIKE_SIGNAL = ("corner_fl", "IMU_FL_ACCEL_Z")
SPIKE_VALUE = 150.0
FAULT_NODE = "corner_br"
FAULT_EVERY_S = 60
FAULT_FOR_S = 2
FAULT_AT_S = 20

CORNERS = ("fl", "fr", "bl", "br")
FAST = [
    (f"corner_{corner}", f"IMU_{corner.upper()}_{kind}_{axis}", unit)
    for corner in CORNERS
    for kind, unit in (("ACCEL", "m/s^2"), ("GYRO", "rad/s"))
    for axis in "XYZ"
]
MEDIUM = [
    ("temperature", "TEMP_BRAKE_FL", "degC"),
    ("temperature", "TEMP_BRAKE_FR", "degC"),
    ("temperature", "TEMP_BRAKE_BL", "degC"),
    ("temperature", "TEMP_BRAKE_BR", "degC"),
    ("temperature", "TEMP_CVT", "degC"),
    ("central", "GPS_LATITUDE", "deg*1e-7"),
    ("central", "GPS_LONGITUDE", "deg*1e-7"),
    ("central", "GPS_SPEED", "m/s"),
    ("central", "GPS_SATELLITES", "count"),
]
SLOW = [("powertrain", "FUEL_LEVEL", "%"), ("central", "COCKPIT_BATTERY", "%")]


def _time(t_us: int) -> str:
    seconds, micros = divmod(t_us, 1_000_000)
    return f"{seconds}.{micros:06d}"


def _jitter(seed: int) -> int:
    """Cheap deterministic jitter in [0, JITTER_US) from an integer seed."""

    return (seed * 2654435761) % 4294967296 % JITTER_US


def spike_time_us(seconds: int) -> int:
    tick = (seconds * 100) // 2
    return START_US + tick * 10_000 + _jitter(tick * 31 + FAST.index((*SPIKE_SIGNAL, "m/s^2")))


def generate(path: Path, seconds: int) -> int:
    """Write the session and return the number of data rows (header excluded)."""

    wave = [math.sin(i * 2 * math.pi / 997) for i in range(997)]
    rows = 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="ascii", newline="\n") as out:
        write = out.write
        write(HEADER)
        write("0.000000,format,,lbr_csv_version,1,,,,\n")
        for name, value in (("protocol_version", 4), ("format_version", 1), ("reset_reason", 1)):
            write(f"{_time(START_US)},session,central,{name},{value},,,,\n")
        rows += 4
        spike_tick = (seconds * 100) // 2
        for tick in range(seconds * 100):
            base = START_US + tick * 10_000
            second = tick // 100
            faulted = FAULT_AT_S <= second % FAULT_EVERY_S < FAULT_AT_S + FAULT_FOR_S
            for index, (node, name, unit) in enumerate(FAST):
                t = base + _jitter(tick * 31 + index)
                if node == FAULT_NODE and faulted:
                    write(f"{_time(t)},sample,{node},{name},,{unit},SENSOR_FAULT,1,{tick}\n")
                    continue
                value = 4.0 * wave[(tick * (index + 3)) % 997] + 0.01 * index
                if (node, name) == SPIKE_SIGNAL and tick == spike_tick:
                    value = SPIKE_VALUE
                write(f"{_time(t)},sample,{node},{name},{value:.6g},{unit},ok,1,{tick}\n")
            rows += len(FAST)
            if tick % 10 == 0:
                for index, (node, name, unit) in enumerate(MEDIUM):
                    t = base + 500 + _jitter(tick * 17 + index)
                    value = 40.0 + 10.0 * wave[(tick // 10 + index * 50) % 997]
                    if unit == "deg*1e-7":
                        value = 302849870 + int(1000 * value)
                    write(
                        f"{_time(t)},sample,{node},{name},{value:.10g},{unit},ok,1,{tick // 10}\n"
                    )
                rows += len(MEDIUM)
            if tick % 100 == 0:
                for node, name, unit in SLOW:
                    t = base + 900
                    level = 90 - second * 0.01
                    write(f"{_time(t)},sample,{node},{name},{level:.6g},{unit},ok,1,{second}\n")
                for node in (
                    "corner_fl",
                    "corner_fr",
                    "corner_bl",
                    "corner_br",
                    "temperature",
                    "central",
                ):
                    t = base + 950
                    flags, status = (2, "SENSOR") if node == FAULT_NODE and faulted else (0, "ok")
                    write(f"{_time(t)},heartbeat,{node},state,{2 if flags else 1},,,,\n")
                    write(f"{_time(t)},heartbeat,{node},err_flags,{flags},bitmask,{status},,\n")
                rows += len(SLOW) + 12
        end = _time(START_US + seconds * 1_000_000)
        write(f"{end},end,central,records_written,{rows},,,,\n")
        write(f"{end},integrity,central,records,{rows},,,,\n")
        rows += 2
    return rows


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a benchmark lbr-csv session.")
    parser.add_argument("--preset", choices=sorted(PRESETS), default="standard")
    parser.add_argument("output", type=Path)
    options = parser.parse_args(argv)
    rows = generate(options.output, PRESETS[options.preset])
    size = options.output.stat().st_size
    print(f"wrote {options.output}: {rows} rows, {size / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
