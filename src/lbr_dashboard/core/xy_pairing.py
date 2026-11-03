"""Pair two numeric signals sampled at different times for an XY plot.

Inputs are plain parallel sequences: integer nanosecond timestamps (non-decreasing)
and values where ``None`` or any non-finite float (NaN, +/-inf) means "missing".

One signal is the *driver* (X unless ``driver="y"``). Every driver sample inside
the closed interval ``[start_ns, end_ns]`` produces at most one pair; the other
signal is looked up at the driver timestamp with an :class:`AlignmentPolicy`.
Values are never interpolated, held past the tolerance, or invented: a driver
sample that cannot be paired is excluded and counted by reason.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import Enum
from itertools import pairwise
from math import isfinite
from typing import Literal

Driver = Literal["x", "y"]


class AlignmentPolicy(Enum):
    """How the other signal's sample is chosen for each driver sample.

    With other-signal samples at t=0 (value 1.0) and t=10 (value 2.0), a driver
    sample at t=4 and ``tolerance_ns=5``:

    - ``NEAREST`` pairs with 1.0 (|4-0| = 4 < |4-10| = 6). At t=5 both are 5 ns
      away; ties pick the earlier sample, so 1.0 again.
    - ``PREVIOUS`` pairs with 1.0 (latest sample at or before t=4, 4 ns old).
      At t=8 the held sample is 8 ns old, beyond tolerance, so no pair; it never
      looks ahead to the sample at t=10.

    Linear interpolation is a possible later policy; it is not offered yet.
    """

    NEAREST = "nearest"
    PREVIOUS = "previous"


@dataclass(frozen=True, slots=True)
class ExclusionCounts:
    """Driver samples in the interval that produced no pair, by first failing reason.

    Reasons are checked in this order: ``x_missing`` (the X value is missing),
    ``y_missing`` (the selected Y value is missing), ``no_match`` (the other
    signal has no sample within tolerance of the driver time).
    """

    x_missing: int = 0
    y_missing: int = 0
    no_match: int = 0

    @property
    def total(self) -> int:
        return self.x_missing + self.y_missing + self.no_match


@dataclass(frozen=True, slots=True)
class PairedSeries:
    """Aligned (x, y) pairs and the rules that produced them.

    ``times_ns[i]`` is the driver timestamp of pair ``i``. A sample of the other
    signal may appear in several pairs when the driver is sampled faster.
    """

    times_ns: tuple[int, ...]
    x: tuple[float, ...]
    y: tuple[float, ...]
    policy: AlignmentPolicy
    tolerance_ns: int
    driver: Driver = "x"
    excluded: ExclusionCounts = ExclusionCounts()
    x_unit: str | None = None
    y_unit: str | None = None

    def __len__(self) -> int:
        return len(self.times_ns)


def _check_signal(name: str, times_ns: Sequence[int], values: Sequence[float | None]) -> None:
    if len(times_ns) != len(values):
        raise ValueError(
            f"{name} has {len(times_ns)} timestamps but {len(values)} values; "
            "they must be the same length"
        )
    for index, (earlier, later) in enumerate(pairwise(times_ns)):
        if later < earlier:
            raise ValueError(
                f"{name} timestamps must be non-decreasing; "
                f"index {index + 1} ({later}) is before index {index} ({earlier})"
            )


def _lookup(
    times_ns: Sequence[int], t_ns: int, policy: AlignmentPolicy, tolerance_ns: int, lo: int
) -> int | None:
    """Return the index of the other-signal sample for driver time ``t_ns``.

    Among samples that share a timestamp, the last one in input order is used.
    """

    after = bisect_right(times_ns, t_ns, lo)
    before = after - 1
    candidate: int | None = before if before >= 0 else None
    if policy is AlignmentPolicy.NEAREST and after < len(times_ns):
        if candidate is None or times_ns[after] - t_ns < t_ns - times_ns[candidate]:
            candidate = bisect_right(times_ns, times_ns[after], after) - 1
    if candidate is None or abs(times_ns[candidate] - t_ns) > tolerance_ns:
        return None
    return candidate


def _pair(
    driver_times: Sequence[int],
    driver_values: Sequence[float | None],
    other_times: Sequence[int],
    other_values: Sequence[float | None],
    policy: AlignmentPolicy,
    tolerance_ns: int,
    start_ns: int | None,
    end_ns: int | None,
) -> tuple[list[int], list[float], list[float], int, int, int]:
    first = 0 if start_ns is None else bisect_left(driver_times, start_ns)
    stop = len(driver_times) if end_ns is None else bisect_right(driver_times, end_ns)
    times: list[int] = []
    driven: list[float] = []
    matched: list[float] = []
    driver_missing = other_missing = no_match = 0
    lo = 0
    for index in range(first, stop):
        t_ns = driver_times[index]
        value = driver_values[index]
        if value is None or not isfinite(value):
            driver_missing += 1
            continue
        lo = bisect_left(other_times, t_ns - tolerance_ns, lo)
        found = _lookup(other_times, t_ns, policy, tolerance_ns, lo)
        if found is None:
            no_match += 1
            continue
        other = other_values[found]
        if other is None or not isfinite(other):
            other_missing += 1
            continue
        times.append(t_ns)
        driven.append(value)
        matched.append(other)
    return times, driven, matched, driver_missing, other_missing, no_match


def pair_signals(
    x_times_ns: Sequence[int],
    x_values: Sequence[float | None],
    y_times_ns: Sequence[int],
    y_values: Sequence[float | None],
    *,
    policy: AlignmentPolicy,
    tolerance_ns: int,
    start_ns: int | None = None,
    end_ns: int | None = None,
    driver: Driver = "x",
    x_unit: str | None = None,
    y_unit: str | None = None,
) -> PairedSeries:
    """Pair X and Y samples by time using ``policy`` and ``tolerance_ns``.

    Driver samples are kept when ``start_ns <= t <= end_ns`` (closed interval;
    ``None`` leaves that side open). The interval selects driver samples only:
    the other signal may be matched up to ``tolerance_ns`` outside it.

    The driver defaults to X: an XY plot reads "Y as a function of X", and each
    X reading appears at most once. Pass ``driver="y"`` to drive by Y instead
    (for example when Y is the slower, sparser signal); the result still has
    X on ``x`` and Y on ``y``.

    Raises ``ValueError`` for mismatched lengths, decreasing timestamps, a
    negative tolerance, or ``start_ns > end_ns``.
    """

    _check_signal("X", x_times_ns, x_values)
    _check_signal("Y", y_times_ns, y_values)
    if tolerance_ns < 0:
        raise ValueError(f"tolerance_ns must be >= 0, got {tolerance_ns}")
    if start_ns is not None and end_ns is not None and start_ns > end_ns:
        raise ValueError(f"start_ns ({start_ns}) is after end_ns ({end_ns})")
    if driver not in ("x", "y"):
        raise ValueError(f"driver must be 'x' or 'y', got {driver!r}")

    if driver == "x":
        times, xs, ys, x_missing, y_missing, no_match = _pair(
            x_times_ns, x_values, y_times_ns, y_values, policy, tolerance_ns, start_ns, end_ns
        )
    else:
        times, ys, xs, y_missing, x_missing, no_match = _pair(
            y_times_ns, y_values, x_times_ns, x_values, policy, tolerance_ns, start_ns, end_ns
        )
    return PairedSeries(
        times_ns=tuple(times),
        x=tuple(xs),
        y=tuple(ys),
        policy=policy,
        tolerance_ns=tolerance_ns,
        driver=driver,
        excluded=ExclusionCounts(x_missing=x_missing, y_missing=y_missing, no_match=no_match),
        x_unit=x_unit,
        y_unit=y_unit,
    )


def limit_points(series: PairedSeries, max_points: int) -> tuple[PairedSeries, int]:
    """Return at most ``max_points`` pairs and how many pairs were dropped.

    Keeps evenly spaced pairs by index, always including the first and last, so
    the result depends only on the input. Exclusion counts are unchanged; the
    dropped pairs are valid pairs that are just not drawn.
    """

    if max_points < 2:
        raise ValueError(f"max_points must be >= 2, got {max_points}")
    count = len(series)
    if count <= max_points:
        return series, 0
    last = count - 1
    indices = [step * last // (max_points - 1) for step in range(max_points)]
    limited = replace(
        series,
        times_ns=tuple(series.times_ns[index] for index in indices),
        x=tuple(series.x[index] for index in indices),
        y=tuple(series.y[index] for index in indices),
    )
    return limited, count - max_points
