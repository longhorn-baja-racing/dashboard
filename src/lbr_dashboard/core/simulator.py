"""Deterministic sparse simulator records through the ordinary source contract."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, replace
from fractions import Fraction
from heapq import heapify, heappop, heappush
from math import isfinite
from random import Random
from threading import Lock
from time import monotonic_ns
from typing import Any, cast

from .contracts import SourceError, SourceEvent, SourceEventKind, SourceHealth, SourceState
from .lifecycle import CancellationToken
from .signals import SignalDescriptor, SignalSample, SignalSchema
from .sources import IteratorSource


def _integer(value: object, name: str, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


def _intervals(values: object) -> tuple[tuple[int, int], ...]:
    try:
        result = tuple(tuple(pair) for pair in cast(Any, values))
    except TypeError as error:
        raise ValueError("sample intervals must contain (start, end) pairs") from error
    end = -1
    for pair in result:
        if len(pair) != 2:
            raise ValueError("sample intervals must contain (start, end) pairs")
        start, stop = pair
        _integer(start, "interval start", 0)
        _integer(stop, "interval end", 1)
        if start >= stop or start < end:
            raise ValueError("sample intervals must be sorted, disjoint and nonempty")
        end = stop
    return result


def _contains(intervals: tuple[tuple[int, int], ...], index: int) -> bool:
    return any(start <= index < stop for start, stop in intervals)


@dataclass(frozen=True, slots=True)
class SimulatedSignal:
    """One descriptor, repeating value cycle and explicit scheduled fault windows.

    Windows use per-signal sample indices [start, stop), not synchronized rows.
    Loss/stale omit records; unavailable emits an explicit null measurement.
    """

    descriptor: SignalDescriptor
    values: tuple[Any, ...]
    noise_amplitude: float = 0.0
    loss: tuple[tuple[int, int], ...] = ()
    unavailable: tuple[tuple[int, int], ...] = ()
    stale: tuple[tuple[int, int], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.descriptor, SignalDescriptor):
            raise TypeError("descriptor must be SignalDescriptor")
        rate = self.descriptor.nominal_rate_hz
        if rate is None or rate > 1_000_000_000:
            raise ValueError("simulator requires a nominal rate at most 1 GHz")
        if self.descriptor.timestamp_domain == "unspecified":
            raise ValueError("simulator requires an explicit timestamp domain")
        if not isinstance(self.values, Sequence) or isinstance(self.values, (str, bytes)):
            raise ValueError("values must be a nonempty cycle")
        values = tuple(self.values)
        if not values:
            raise ValueError("values must be a nonempty cycle")
        frozen = []
        for value in values:
            sample = SignalSample(
                0,
                value,
                source_id=self.descriptor.source_id,
                timestamp_domain=self.descriptor.timestamp_domain,
            )
            self.descriptor.validate_sample(sample)
            frozen.append(sample.value)
        object.__setattr__(self, "values", tuple(frozen))
        amplitude = self.noise_amplitude
        if isinstance(amplitude, bool) or not isinstance(amplitude, (int, float)):
            raise ValueError("noise_amplitude must be finite and nonnegative")
        try:
            valid = isfinite(amplitude) and amplitude >= 0
        except OverflowError:
            valid = False
        if not valid or (amplitude and self.descriptor.data_type != "numeric"):
            raise ValueError("noise requires numeric signals and finite nonnegative amplitude")
        for field in ("loss", "unavailable", "stale"):
            object.__setattr__(self, field, _intervals(getattr(self, field)))
        combined = sorted(
            (start, stop)
            for field in (self.loss, self.unavailable, self.stale)
            for start, stop in field
        )
        if any(left[1] > right[0] for left, right in zip(combined, combined[1:])):
            raise ValueError("loss, unavailable and stale windows must not overlap")


@dataclass(frozen=True, slots=True)
class SimulatorStats:
    attempted: int = 0
    emitted: int = 0
    lost: int = 0
    unavailable: int = 0
    stale: int = 0


class SimulatorSource(IteratorSource):
    """Seeded finite/live signals with exact scheduled measurement timestamps.

    Finite accelerated mode has no wall-clock waits. Live real-time mode uses
    cooperative bounded waits, independently of the selected-time replay clock.
    """

    def __init__(
        self,
        signals: tuple[SimulatedSignal, ...],
        *,
        seed: int = 0,
        start_ns: int = 0,
        duration_ns: int | None = 1_000_000_000,
        realtime: bool = False,
        heartbeat_ns: int = 1_000_000_000,
        disconnect_after_ns: int | None = None,
        capacity: int = 256,
        publish_timeout: float = 0.01,
        now_ns: Callable[[], int] = monotonic_ns,
        wait: Callable[[CancellationToken, int], None] | None = None,
    ) -> None:
        self._signals = tuple(signals)
        if not self._signals or any(
            not isinstance(item, SimulatedSignal) for item in self._signals
        ):
            raise ValueError("signals must contain at least one SimulatedSignal")
        self._signals = tuple(sorted(self._signals, key=lambda item: item.descriptor.identifier))
        source_id = self._signals[0].descriptor.source_id
        if any(item.descriptor.source_id != source_id for item in self._signals):
            raise ValueError("one simulator must have one source identity")
        self._seed = _integer(seed, "seed")
        self._start = _integer(start_ns, "start_ns")
        self._duration = None if duration_ns is None else _integer(duration_ns, "duration_ns", 0)
        self._heartbeat = _integer(heartbeat_ns, "heartbeat_ns", 1)
        self._disconnect = (
            None
            if disconnect_after_ns is None
            else _integer(disconnect_after_ns, "disconnect_after_ns", 0)
        )
        if not isinstance(realtime, bool):
            raise ValueError("realtime must be boolean")
        if not callable(now_ns) or (wait is not None and not callable(wait)):
            raise TypeError("now_ns and wait must be callable")
        self._realtime, self._now, self._wait = realtime, now_ns, wait
        self._stats_lock = Lock()
        self._sim_stats = SimulatorStats()
        schema = SignalSchema(
            1,
            tuple(item.descriptor for item in self._signals),
            metadata={"simulated": True, "seed": seed},
        )
        super().__init__(
            source_id,
            schema,
            self._generate,
            is_live=duration_ns is None,
            capacity=capacity,
            publish_timeout=publish_timeout,
        )

    @property
    def simulator_stats(self) -> SimulatorStats:
        with self._stats_lock:
            return self._sim_stats

    def _count(self, **changes: int) -> None:
        with self._stats_lock:
            values = {key: getattr(self._sim_stats, key) + value for key, value in changes.items()}
            self._sim_stats = replace(self._sim_stats, **values)

    def health(self) -> SourceHealth:
        health = super().health()
        return replace(health, dropped_records=health.dropped_records + self.simulator_stats.lost)

    def _pace(self, token: CancellationToken, origin: int, offset: int, previous: int) -> int:
        if not self._realtime:
            return previous
        while True:
            token.raise_if_cancelled()
            now = _integer(self._now(), "monotonic clock")
            if now < previous:
                raise ValueError("simulator monotonic clock moved backwards")
            previous = now
            remaining = origin + offset - now
            if remaining <= 0:
                return now
            step = min(remaining, 1_000_000_000)
            if self._wait is None:
                token.wait(step / 1_000_000_000)
            else:
                self._wait(token, step)

    def _generate(self, token: CancellationToken) -> Iterator[SourceEvent]:
        with self._stats_lock:
            self._sim_stats = SimulatorStats()
        origin = _integer(self._now(), "monotonic clock") if self._realtime else 0
        randoms = [Random(f"{self._seed}:{item.descriptor.identifier}") for item in self._signals]
        periods = [
            Fraction(1_000_000_000) / Fraction(str(item.descriptor.nominal_rate_hz))
            for item in self._signals
        ]
        # Heap contains one upcoming item per signal plus heartbeat/disconnect.
        schedule = [(0, index, 0) for index in range(len(self._signals))]
        heapify(schedule)
        heartbeat_index = len(self._signals)
        heappush(schedule, (self._heartbeat, heartbeat_index, 0))
        if self._disconnect is not None:
            heappush(schedule, (self._disconnect, -1, 0))
        previous = origin
        while schedule:
            token.raise_if_cancelled()
            offset, index, sample_index = heappop(schedule)
            if self._duration is not None and offset > self._duration:
                self._pace(token, origin, self._duration, previous)
                return
            previous = self._pace(token, origin, offset, previous)
            token.raise_if_cancelled()
            if index == -1:
                yield SourceEvent(
                    SourceEventKind.ERROR,
                    self.source_id,
                    error=SourceError(
                        "simulated_disconnect",
                        "Simulated source disconnected",
                        context={"timestamp_ns": self._start + offset},
                    ),
                )
                return
            if index == heartbeat_index:
                yield SourceEvent(
                    SourceEventKind.HEALTH, self.source_id, health=self.health_for_heartbeat()
                )
                heappush(schedule, (offset + self._heartbeat, index, 0))
                continue
            signal = self._signals[index]
            descriptor = signal.descriptor
            value = signal.values[sample_index % len(signal.values)]
            if signal.noise_amplitude:
                value += randoms[index].uniform(-signal.noise_amplitude, signal.noise_amplitude)
            self._count(attempted=1)
            lost = _contains(signal.loss, sample_index)
            stale = _contains(signal.stale, sample_index)
            missing = _contains(signal.unavailable, sample_index)
            if lost or stale:
                self._count(**({"lost": 1} if lost else {"stale": 1}))
                windows = signal.loss if lost else signal.stale
                if any(start == sample_index for start, _ in windows):
                    yield SourceEvent(
                        SourceEventKind.ERROR,
                        self.source_id,
                        error=SourceError(
                            "simulated_loss" if lost else "simulated_stale",
                            "Scheduled measurements lost" if lost else "Signal intentionally stale",
                            True,
                            {
                                "signal_id": descriptor.identifier,
                                "timestamp_ns": self._start + offset,
                            },
                        ),
                    )
            else:
                sample = SignalSample(
                    self._start + offset,
                    None if missing else value,
                    available=not missing,
                    quality="simulated_unavailable" if missing else "simulated",
                    source_id=self.source_id,
                    unit=descriptor.canonical_unit,
                    timestamp_domain=descriptor.timestamp_domain,
                )
                descriptor.validate_sample(sample)
                self._count(emitted=1, unavailable=int(missing))
                yield SourceEvent(
                    SourceEventKind.RECORD,
                    self.source_id,
                    signal_id=descriptor.identifier,
                    sample=sample,
                )
            next_index = sample_index + 1
            next_offset = (next_index * periods[index]).numerator // (
                next_index * periods[index]
            ).denominator
            heappush(schedule, (next_offset, index, next_index))

    def health_for_heartbeat(self) -> SourceHealth:
        """A scheduled heartbeat; persistent health separately includes queue stats."""
        return SourceHealth(SourceState.RUNNING, dropped_records=self.simulator_stats.lost)


def example_signals(
    family: str = "process", *, source_id: str = "simulator"
) -> tuple[SimulatedSignal, ...]:
    """Two fictional, unrelated examples; these are not application-required channels."""

    def model(
        identifier: str, kind: str, unit: str | None, rate: float, shape: tuple[int, ...] = ()
    ):
        return SignalDescriptor(
            identifier,
            identifier.rsplit(".", 1)[-1],
            (family,),
            kind,
            source_id,
            canonical_unit=unit,
            nominal_rate_hz=rate,
            shape=shape,
            timestamp_domain="relative",
        )

    if family == "process":
        return (
            SimulatedSignal(
                model("process.pressure", "numeric", "Pa", 100),
                (0, 5, 8, 3),
                loss=((20, 25),),
                unavailable=((50, 55),),
                stale=((70, 75),),
            ),
            SimulatedSignal(
                model("process.temperature", "numeric", "°C", 1), (20, 21, 19), noise_amplitude=0.1
            ),
            SimulatedSignal(
                model("process.status", "status", None, 2), ({"state": "idle"}, {"state": "ready"})
            ),
        )
    if family == "motion":
        return (
            SimulatedSignal(
                model("motion.vector", "vector", "m/s²", 10, (3,)), ([0, 1, 2], [3, 2, 1])
            ),
            SimulatedSignal(
                model("motion.speed", "numeric", "rad/s", 3), (0, 4, 8), noise_amplitude=0.2
            ),
            SimulatedSignal(model("motion.enabled", "boolean", None, 1), (False, True)),
            SimulatedSignal(model("motion.notice", "event", None, 1), ({"code": "tick"},)),
        )
    raise ValueError("unknown example family; choose process or motion")
