"""Sample-count histograms for one numeric signal over a closed time interval.

Inputs are plain parallel sequences: ``times_ns`` (integer nanoseconds,
non-decreasing) and ``values`` (floats, with ``None`` or NaN meaning missing).
See docs/histogram.md for the full rules.

Bin edge rule (the same as ``numpy.histogram``): every bin is half-open
``[a, b)`` except the last, which is closed ``[a, b]``.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from math import isfinite, isnan

DEFAULT_BINS = 10
EDGE_RULE = "Bins are half-open [a, b) except the last bin, which is closed [a, b]."


@dataclass(frozen=True, slots=True)
class HistogramRow:
    """One exportable bin: its edges, sample count and which sides are closed."""

    bin_start: float
    bin_end: float
    count: int
    closed: str  # "left" for [a, b), "both" for the last bin [a, b]


@dataclass(frozen=True, slots=True)
class Histogram:
    """Sample-weighted bin counts plus everything that was left out of the bins.

    ``edges`` has one more entry than ``counts``. Both are empty when there were
    no valid samples and no explicit edges or range to build bins from.
    ``start_ns``/``end_ns`` are the requested closed interval; ``None`` means
    unbounded on that side.
    """

    edges: tuple[float, ...]
    counts: tuple[int, ...]
    below_range: int
    above_range: int
    missing: int
    non_finite: int
    unit: str
    start_ns: int | None
    end_ns: int | None
    edge_rule: str = EDGE_RULE

    @property
    def valid(self) -> int:
        """Number of samples counted in a bin."""

        return sum(self.counts)

    @property
    def total(self) -> int:
        """Number of samples inside the time interval, counted or not."""

        return self.valid + self.below_range + self.above_range + self.missing + self.non_finite

    @property
    def is_empty(self) -> bool:
        """True when no sample landed in any bin."""

        return self.valid == 0

    def to_rows(self) -> tuple[HistogramRow, ...]:
        """Return one row per bin, in edge order, for display or export."""

        last = len(self.counts) - 1
        return tuple(
            HistogramRow(
                bin_start=self.edges[index],
                bin_end=self.edges[index + 1],
                count=count,
                closed="both" if index == last else "left",
            )
            for index, count in enumerate(self.counts)
        )


def histogram(
    times_ns: Sequence[int],
    values: Sequence[float | None],
    *,
    bins: int | None = None,
    edges: Sequence[float] | None = None,
    value_range: tuple[float, float] | None = None,
    start_ns: int | None = None,
    end_ns: int | None = None,
    unit: str = "",
) -> Histogram:
    """Count the samples of one signal inside ``[start_ns, end_ns]`` per value bin.

    The time interval is closed: samples at exactly ``start_ns`` or ``end_ns``
    are included. ``times_ns`` must be non-decreasing (it is searched with
    bisect, not checked).

    Bins come either from ``edges`` (finite, strictly increasing, at least two)
    or from ``bins`` equal-width bins (default 10) over ``value_range``, which
    defaults to the min and max of the valid values in the interval. A range
    whose ends are equal is widened to ``value - 0.5 .. value + 0.5``.

    Each valid sample counts once, however long it lasted (no time weighting).
    ``None`` and NaN are counted as missing, +/-inf as non-finite, and finite
    values outside the bins as below or above range; none of them enter a bin.
    """

    if len(times_ns) != len(values):
        raise ValueError("times_ns and values must have the same length")
    if start_ns is not None and end_ns is not None and start_ns > end_ns:
        raise ValueError("start_ns must not be after end_ns")
    if edges is not None and (bins is not None or value_range is not None):
        raise ValueError("Give either edges or bins/value_range, not both")

    first = 0 if start_ns is None else bisect_left(times_ns, start_ns)
    last = len(times_ns) if end_ns is None else bisect_right(times_ns, end_ns)

    finite: list[float] = []
    missing = 0
    non_finite = 0
    for index in range(first, last):
        value = values[index]
        if value is None or isnan(value):
            missing += 1
        elif isfinite(value):
            finite.append(float(value))
        else:
            non_finite += 1

    if edges is not None:
        bin_edges = _checked_edges(edges)
    else:
        count = DEFAULT_BINS if bins is None else bins
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError("bins must be a positive integer")
        if value_range is not None:
            bin_edges = _equal_width_edges(*value_range, count)
        elif finite:
            bin_edges = _equal_width_edges(min(finite), max(finite), count)
        else:
            bin_edges = ()

    counts = [0] * max(len(bin_edges) - 1, 0)
    below = above = 0
    if bin_edges:
        low, high = bin_edges[0], bin_edges[-1]
        top = len(counts) - 1
        for value in finite:
            if value < low:
                below += 1
            elif value > high:
                above += 1
            elif value == high:
                counts[top] += 1
            else:
                counts[bisect_right(bin_edges, value) - 1] += 1

    return Histogram(
        edges=bin_edges,
        counts=tuple(counts),
        below_range=below,
        above_range=above,
        missing=missing,
        non_finite=non_finite,
        unit=unit,
        start_ns=start_ns,
        end_ns=end_ns,
    )


def _checked_edges(edges: Sequence[float]) -> tuple[float, ...]:
    result = tuple(float(edge) for edge in edges)
    if len(result) < 2:
        raise ValueError("edges needs at least two values")
    if not all(isfinite(edge) for edge in result):
        raise ValueError("edges must be finite")
    if any(left >= right for left, right in zip(result, result[1:])):
        raise ValueError("edges must be strictly increasing")
    return result


def _equal_width_edges(low: float, high: float, bins: int) -> tuple[float, ...]:
    """Edges ``low + i * step`` with the last edge exactly ``high``, like numpy."""

    low, high = float(low), float(high)
    if not (isfinite(low) and isfinite(high)):
        raise ValueError("value_range must be finite")
    if low > high:
        raise ValueError("value_range must be (low, high) with low <= high")
    if low == high:
        low, high = low - 0.5, high + 0.5
    step = (high - low) / bins
    if not isfinite(step):
        raise ValueError("value range is too wide to split into bins")
    result = (*(low + index * step for index in range(bins)), high)
    if any(left >= right for left, right in zip(result, result[1:])):
        raise ValueError("value range is too narrow for this many bins")
    return result
