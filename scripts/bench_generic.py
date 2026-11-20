"""Deterministic fixtures and measurements for the generic session stack (#27).

The generic stack is what the source picker uses: a ``TelemetrySession`` fed
``SourceEvent`` batches, immutable snapshots with windowed range queries, the
``TimePlot`` with its decimation, and replay seeking through the session clock.

The fixture is a mixed-rate source shaped like the car's DAQ (see ``MIX``):
one 1 kHz strain signal, 24 IMU signals at 100 Hz, 9 temperatures at 10 Hz
and 2 battery levels at 1 Hz, 3492 samples per second in total. Timestamps
carry seeded jitter of up to 30 % of a period, so they are irregular and
interleaved across signals. ``GAP_SIGNALS`` report unavailable readings for
one window and ``SPIKE_SIGNAL`` has one single-sample spike halfway through.
The same seed and duration always produce the same events.

``measure(workload)`` runs every generic measurement in this process and
returns a flat dict of metrics; ``scripts/benchmark.py`` calls it in a fresh
child process and checks the results against the budgets.
"""

from __future__ import annotations

import gc
import math
import random
import statistics
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import asdict, dataclass
from pathlib import Path

import bench_system

from lbr_dashboard.core import (
    SessionSnapshot,
    SignalDescriptor,
    SignalSample,
    SignalSchema,
    SourceEvent,
    SourceEventKind,
    TelemetrySession,
)

SEED = 27
SOURCE_ID = "bench"
DOMAIN = "relative"
START_NS = 1_000_000_000
JITTER = 0.3
#: The source controller drains at most this many events per poll ...
BATCH = 512
#: ... every this many nanoseconds.
POLL_NS = 30_000_000
#: The source controller's ``max_samples_per_signal`` for live sources.
LIVE_CAP = 1000
SPIKE_SIGNAL = "bench.strain"
SPIKE_VALUE = 5000.0
GAP_SIGNALS = ("bench.strain", "bench.imu.23")
GAP_QUALITY = "SENSOR_FAULT"
RANGE_SIGNAL = "bench.imu.00"
RANGE_WINDOW_NS = 10_000_000_000
PLOT_SIZE = (1200, 500)


@dataclass(frozen=True)
class SignalGroup:
    name: str
    count: int
    rate_hz: int
    unit: str
    amplitude: float
    offset: float


MIX = (
    SignalGroup("strain", 1, 1000, "ue", 400.0, 0.0),
    SignalGroup("imu", 24, 100, "m/s^2", 4.0, 0.0),
    SignalGroup("temp", 9, 10, "degC", 10.0, 40.0),
    SignalGroup("battery", 2, 1, "%", 5.0, 85.0),
)


@dataclass(frozen=True)
class Workload:
    """Sizes for one benchmark mode; rates come from ``MIX``."""

    replay_s: int
    live_s: int
    live_warmup_s: int
    csv_rows: int
    csv_columns: int
    seeks: int
    plot_repeats: int


WORKLOADS = {
    # Seconds-scale sanity run for the test suite.
    "smoke": Workload(2, 3, 1, 200, 4, 5, 2),
    # CI: well under a minute per run on a hosted runner.
    "quick": Workload(20, 30, 10, 1000, 6, 40, 3),
    # Reference measurement: a 10 minute session (2.1M samples) and 10 minutes of live data.
    "full": Workload(600, 600, 60, 5000, 10, 200, 5),
}


def rate_per_second(mix: Iterable[SignalGroup] = MIX) -> int:
    return sum(group.count * group.rate_hz for group in mix)


def describe(name: str) -> dict[str, object]:
    """Stated rates, signal counts and durations, recorded with every result."""

    workload = WORKLOADS[name]
    return {
        "name": name,
        **asdict(workload),
        "seed": SEED,
        "signals": sum(group.count for group in MIX),
        "samples_per_s": rate_per_second(),
        "replay_samples": rate_per_second() * workload.replay_s,
        "live_samples": rate_per_second() * workload.live_s,
        "live_cap_per_signal": LIVE_CAP,
        "batch": BATCH,
        "poll_ms": POLL_NS / 1e6,
        "mix": [asdict(group) for group in MIX],
    }


def descriptors(mix: Iterable[SignalGroup] = MIX) -> tuple[SignalDescriptor, ...]:
    items = []
    for group in mix:
        for index in range(group.count):
            identifier = (
                f"{SOURCE_ID}.{group.name}"
                if group.count == 1
                else f"{SOURCE_ID}.{group.name}.{index:02d}"
            )
            items.append(
                SignalDescriptor(
                    identifier,
                    identifier.split(".", 1)[1].replace(".", " "),
                    (SOURCE_ID, group.name),
                    "numeric",
                    SOURCE_ID,
                    canonical_unit=group.unit,
                    nominal_rate_hz=float(group.rate_hz),
                    timestamp_domain=DOMAIN,
                )
            )
    return tuple(items)


def schema() -> SignalSchema:
    return SignalSchema(1, descriptors(), metadata={"benchmark": True, "seed": SEED})


def gap_window_ns(seconds: int) -> tuple[int, int]:
    """[start, end) of the unavailable window on ``GAP_SIGNALS``."""

    duration = seconds * 1_000_000_000
    start = START_NS + duration // 4
    return start, start + max(duration // 100, 50_000_000)


def spike_time_ns(seconds: int) -> int:
    rate = next(g.rate_hz for g in MIX if f"{SOURCE_ID}.{g.name}" == SPIKE_SIGNAL)
    return START_NS + (seconds * rate // 2) * (1_000_000_000 // rate)


def events(seconds: int, *, seed: int = SEED) -> Iterator[SourceEvent]:
    """Every record of the fixture in measurement-time order, one second at a time.

    Each signal draws from its own generator seeded with ``seed`` and its
    identifier, so a longer fixture starts with the same samples as a shorter one.
    """

    plans = []
    rates = {f"{SOURCE_ID}.{g.name}": g for g in MIX}
    for order, descriptor in enumerate(descriptors()):
        group = rates.get(descriptor.identifier) or rates[descriptor.identifier.rsplit(".", 1)[0]]
        plans.append(
            (
                order,
                descriptor.identifier,
                group,
                1_000_000_000 // group.rate_hz,
                random.Random(f"{seed}:{descriptor.identifier}"),
                order * 0.7,
            )
        )
    gap_start, gap_end = gap_window_ns(seconds)
    spike = spike_time_ns(seconds)
    for second in range(seconds):
        chunk: list[tuple[int, int, str, SignalSample]] = []
        for order, identifier, group, period, rng, phase in plans:
            for index in range(second * group.rate_hz, (second + 1) * group.rate_hz):
                nominal = START_NS + index * period
                jitter = int(rng.random() * JITTER * period)
                noise = (rng.random() - 0.5) * 0.02 * group.amplitude
                if identifier == SPIKE_SIGNAL and nominal == spike:
                    sample = SignalSample(nominal, SPIKE_VALUE, timestamp_domain=DOMAIN)
                elif identifier in GAP_SIGNALS and gap_start <= nominal < gap_end:
                    sample = SignalSample(
                        nominal + jitter,
                        None,
                        available=False,
                        quality=GAP_QUALITY,
                        timestamp_domain=DOMAIN,
                    )
                else:
                    wave = math.sin(2 * math.pi * 0.2 * (nominal - START_NS) / 1e9 + phase)
                    value = round(group.offset + group.amplitude * wave + noise, 4)
                    sample = SignalSample(nominal + jitter, value, timestamp_domain=DOMAIN)
                chunk.append((sample.timestamp_ns, order, identifier, sample))
        chunk.sort(key=lambda item: (item[0], item[1]))
        for _, _, identifier, sample in chunk:
            yield SourceEvent(
                SourceEventKind.RECORD, SOURCE_ID, signal_id=identifier, sample=sample
            )


def batches(items: Iterable[SourceEvent], size: int = BATCH) -> Iterator[list[SourceEvent]]:
    batch: list[SourceEvent] = []
    for item in items:
        batch.append(item)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch


def write_csv(path: Path, rows: int, columns: int, *, seed: int = SEED) -> int:
    """A ``timestamp_ms`` CSV for the source picker; returns the record count.

    Rows are 10 ms apart with up to 2 ms of jitter. Every seventh cell is
    blank, which the CSV source turns into an unavailable record.
    """

    rng = random.Random(f"{seed}:csv")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="ascii", newline="\n") as out:
        out.write(",".join(["timestamp_ms", *(f"channel_{c}" for c in range(columns))]) + "\n")
        for row in range(rows):
            cells = [f"{row * 10 + int(rng.random() * 3)}"]
            for column in range(columns):
                value = 20 * math.sin(row / 50 + column) + rng.random()
                cells.append("" if (row + column) % 7 == 0 else f"{value:.4f}")
            out.write(",".join(cells) + "\n")
    return rows * columns


def _median_ms(values: list[float]) -> float:
    return statistics.median(values) * 1000


def _p95_ms(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)] * 1000


def _timed(action: Callable[[], object]) -> float:
    began = time.perf_counter()
    action()
    return time.perf_counter() - began


def measure_replay(workload: Workload) -> tuple[TelemetrySession, dict[str, float]]:
    """Ingest a whole recording in source-controller sized batches, then query it."""

    session = TelemetrySession(schema())
    gc.collect()
    before = bench_system.current_memory_mb()
    busy = 0.0
    count = 0
    for batch in batches(events(workload.replay_s)):
        busy += _timed(lambda batch=batch: session.ingest(batch))
        count += len(batch)
    gc.collect()
    retained = bench_system.current_memory_mb() - before
    snapshot_times = []
    snapshot = session.snapshot()
    for _ in range(5):
        snapshot_times.append(_timed(session.snapshot))
    rng = random.Random(SEED)
    first, last = snapshot.bounds(RANGE_SIGNAL) or (START_NS, START_NS)
    windows = []
    sizes = []
    for _ in range(200):
        start = first + int(rng.random() * max(0, last - first - RANGE_WINDOW_NS))
        began = time.perf_counter()
        found = snapshot.range(RANGE_SIGNAL, start, start + RANGE_WINDOW_NS, neighbors=True)
        windows.append(time.perf_counter() - began)
        sizes.append(len(found))
    return session, {
        "replay_samples": float(count),
        "ingest_samples_per_s": count / busy,
        "snapshot_ms": _median_ms(snapshot_times),
        "range_ms": _median_ms(windows),
        "range_records": statistics.median(sizes),
        "session_mb_per_million_samples": retained / (count / 1e6),
    }


def measure_plot(snapshot: SessionSnapshot, repeats: int) -> dict[str, float]:
    """Refresh the generic time plot on the largest trace, whole and zoomed."""

    import pyqtgraph as pg

    from lbr_dashboard.ui.time_plot import NANOSECONDS, TimePlot

    widget = pg.PlotWidget()
    widget.resize(*PLOT_SIZE)
    widget.show()
    plot = TimePlot(widget)
    bounds = snapshot.bounds(SPIKE_SIGNAL)
    assert bounds is not None

    def query(start: int, end: int):
        return snapshot.range(SPIKE_SIGNAL, start, end, neighbors=True)

    plot.set_trace((snapshot, SPIKE_SIGNAL), "strain", "ue", bounds, query)
    widget.grab()

    def refresh() -> None:
        plot.refresh()
        widget.grab()

    whole = [_timed(refresh) for _ in range(repeats)]
    curve = plot.curve
    assert curve is not None
    xs = curve.getData()[0]
    drawn = 0 if xs is None else len(xs)
    middle = (bounds[0] + bounds[1]) / 2 - plot.origin_ns
    half = RANGE_WINDOW_NS / 2
    plot.view.setXRange((middle - half) / NANOSECONDS, (middle + half) / NANOSECONDS, padding=0)
    plot.timer.stop()
    zoomed = [_timed(refresh) for _ in range(repeats)]
    plot.clear()
    widget.close()
    widget.deleteLater()
    return {
        "plot_trace_points": float(len(snapshot.series[SPIKE_SIGNAL])),
        "plot_drawn_points": float(drawn),
        "plot_full_ms": _median_ms(whole),
        "plot_zoom_ms": _median_ms(zoomed),
    }


def measure_live(workload: Workload) -> dict[str, float]:
    """Feed the stated rate poll by poll into a bounded session, as the app does live.

    Measurement time is simulated, so the run takes as long as the work, not
    as long as ``live_s``. Each poll ingests what arrived in the last 30 ms
    and takes a snapshot, like ``SourceController._poll``. Only that work is
    timed; building the fixture events is the source's cost, not the app's.
    """

    session = TelemetrySession(schema(), max_samples_per_signal=LIVE_CAP)
    warmup_ns = START_NS + workload.live_warmup_s * 1_000_000_000
    polls: list[float] = []
    pending: list[SourceEvent] = []
    memory: list[float] = []
    warm: float | None = None
    next_poll = START_NS + POLL_NS

    def flush() -> None:
        began = time.perf_counter()
        for batch in batches(pending):
            session.ingest(batch)
        session.snapshot()
        polls.append(time.perf_counter() - began)
        pending.clear()

    count = 0
    for event in events(workload.live_s, seed=SEED + 1):
        assert event.sample is not None
        while event.sample.timestamp_ns >= next_poll:
            flush()
            next_poll += POLL_NS
            if next_poll >= warmup_ns:
                if warm is None:
                    gc.collect()
                    warm = bench_system.current_memory_mb()
                elif len(polls) % 100 == 0:
                    memory.append(bench_system.current_memory_mb())
        pending.append(event)
        count += 1
    flush()
    gc.collect()
    memory.append(bench_system.current_memory_mb())
    snapshot = session.snapshot()
    longest = max(len(records) for records in snapshot.series.values())
    if longest > LIVE_CAP:
        raise RuntimeError(f"live retention exceeded {LIVE_CAP} samples per signal: {longest}")
    busy = sum(polls)
    growth = max(memory) - (warm if warm is not None else memory[0])
    minutes = max(workload.live_s - workload.live_warmup_s, 1) / 60
    return {
        "live_samples": float(count),
        "live_cpu_pct": 100 * busy / workload.live_s,
        "live_samples_per_s": count / busy,
        "live_poll_p95_ms": _p95_ms(polls),
        "live_retained_max": float(longest),
        "live_evicted": float(sum(snapshot.evicted.values())),
        "live_rss_growth_mb": growth,
        "live_rss_mb_per_min": growth / minutes,
    }


def measure_app(workload: Workload, folder: Path, application) -> dict[str, float]:
    """Open a timestamped CSV through the source picker path, then seek the replay."""

    from lbr_dashboard.app import create_main_window
    from lbr_dashboard.source_controller import SourceController
    from lbr_dashboard.ui.replay_bar import SLIDER_STEPS

    path = folder / f"generic-{workload.csv_rows}x{workload.csv_columns}.csv"
    samples = write_csv(path, workload.csv_rows, workload.csv_columns)
    window = create_main_window()
    window.resize(1400, 850)
    window.show()
    application.processEvents()
    owner = window.source_owner
    assert isinstance(owner, SourceController)
    began = time.perf_counter()
    owner.open_csv(path)
    deadline = began + 600
    while not window.replay_bar.slider.isEnabled():
        if time.perf_counter() > deadline:
            status = window.source_picker.status.text()
            raise RuntimeError(f"{path} did not finish loading: {status}")
        application.processEvents()
        time.sleep(0.001)
    opened = time.perf_counter() - began
    rng = random.Random(SEED)
    seeks = []
    for _ in range(workload.seeks):
        position = int(rng.random() * (SLIDER_STEPS + 1))
        began = time.perf_counter()
        owner.replay.seek_slider(position)
        application.processEvents()
        seeks.append(time.perf_counter() - began)
    window.close()
    application.processEvents()
    return {
        "app_open_samples": float(samples),
        "app_open_s": opened,
        "app_open_samples_per_s": samples / opened,
        "seek_ms": _median_ms(seeks),
        "seek_p95_ms": _p95_ms(seeks),
    }


def measure(name: str, folder: Path) -> dict[str, float]:
    """Every generic metric in this process; ``folder`` holds the CSV fixture."""

    from lbr_dashboard.app import create_application

    workload = WORKLOADS[name]
    application = create_application([])
    session, result = measure_replay(workload)
    result.update(measure_plot(session.snapshot(), workload.plot_repeats))
    del session
    gc.collect()
    result.update(measure_live(workload))
    result.update(measure_app(workload, folder, application))
    result["generic_peak_mb"] = bench_system.peak_memory_mb()
    return result
