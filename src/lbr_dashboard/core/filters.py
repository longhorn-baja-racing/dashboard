"""Named smoothing filters for one numeric signal.

Only a moving average exists so far. Inputs are plain parallel sequences:
``times_ns`` (integer nanoseconds, strictly increasing) and ``values`` (floats,
with ``None``, NaN or +/-inf meaning "no usable sample"). The raw inputs are
copied, never modified, and the result is aligned sample for sample with them.

Rules, in short (docs/filters.md has the details):

- The window is a number of samples, not a duration. Sampling must therefore
  be regular: every spacing must be within ``jitter_tolerance`` of the nominal
  period (the median spacing), or :class:`IrregularSamplingError` is raised.
- A missing value never enters an average. It splits the signal into separate
  segments, and so does any spacing of at least ``gap_ns`` when that is given.
- No partial windows: a sample whose full window does not fit inside its
  segment gets no output (NaN in ``values``, ``None`` from ``values_or_none``).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import numpy.typing as npt

MOVING_AVERAGE = "moving_average"
#: Bumped whenever the output for the same inputs and parameters would change.
MOVING_AVERAGE_VERSION = 1
ALIGNMENTS = ("centered", "trailing")
DEFAULT_JITTER_TOLERANCE = 0.1


class IrregularSamplingError(ValueError):
    """Sample spacing varies too much for a window counted in samples."""


@dataclass(frozen=True, slots=True)
class MovingAverageParams:
    """Everything needed to reproduce a moving average from the same raw input.

    ``window_samples`` samples are averaged with equal weight. ``alignment`` is
    ``"centered"`` (output at the middle sample of the window, no delay; the
    window must be odd) or ``"trailing"`` (output at the last sample of the
    window, causal, delayed by ``(window_samples - 1) / 2`` periods).
    ``jitter_tolerance`` is the allowed relative deviation of each spacing from
    the nominal period. ``gap_ns``, when set, is the smallest spacing treated as
    a time gap that splits segments instead of being checked for jitter.
    """

    window_samples: int
    alignment: str = "centered"
    jitter_tolerance: float = DEFAULT_JITTER_TOLERANCE
    gap_ns: int | None = None

    def as_dict(self) -> dict[str, Any]:
        """Keyword arguments that reproduce this filter with :func:`moving_average`."""

        return asdict(self)


@dataclass(frozen=True, eq=False)
class FilteredSignal:
    """Filter output aligned with the raw input timestamps, plus its provenance.

    ``times_ns`` is a copy of the input timestamps and ``values`` holds one
    output per input sample, NaN where there is no output (missing input or a
    window that does not fit). Both arrays are read-only. ``nominal_period_ns``
    is the median spacing used for the regularity check, or ``None`` with fewer
    than two samples. ``missing_count`` counts inputs that were None, NaN or
    infinite; ``edge_count`` counts valid inputs that got no output because
    their window did not fit inside their segment.
    """

    filter_name: str
    version: int
    params: MovingAverageParams
    input_count: int
    times_ns: npt.NDArray[np.int64]
    values: npt.NDArray[np.float64]
    nominal_period_ns: float | None
    segment_count: int
    missing_count: int
    edge_count: int
    signal: str = ""
    unit: str = ""

    @property
    def output_count(self) -> int:
        """Number of samples that have a filtered value."""

        return self.input_count - self.missing_count - self.edge_count

    @property
    def window_span_ns(self) -> float | None:
        """Time covered by one window at the nominal period."""

        if self.nominal_period_ns is None:
            return None
        return self.params.window_samples * self.nominal_period_ns

    @property
    def delay_ns(self) -> float | None:
        """Group delay of the output relative to the input (0 when centered)."""

        if self.params.alignment == "centered":
            return 0.0
        if self.nominal_period_ns is None:
            return None
        return (self.params.window_samples - 1) / 2 * self.nominal_period_ns

    def values_or_none(self) -> list[float | None]:
        """Output values as plain floats, with ``None`` where there is no output."""

        return [None if np.isnan(value) else float(value) for value in self.values]

    def provenance(self) -> dict[str, Any]:
        """Plain description of how this output was made, for display or export."""

        return {
            "filter": self.filter_name,
            "version": self.version,
            "params": self.params.as_dict(),
            "input_count": self.input_count,
            "signal": self.signal,
            "unit": self.unit,
        }


def moving_average(
    times_ns: Sequence[int],
    values: Sequence[float | None],
    *,
    window_samples: int,
    alignment: str = "centered",
    jitter_tolerance: float = DEFAULT_JITTER_TOLERANCE,
    gap_ns: int | None = None,
    signal: str = "",
    unit: str = "",
) -> FilteredSignal:
    """Equal-weight moving average over ``window_samples`` consecutive samples.

    Raises ``ValueError`` for invalid parameters, mismatched lengths and
    timestamps that are not strictly increasing integers, and
    :class:`IrregularSamplingError` (a ``ValueError``) when a spacing differs
    from the nominal period by more than ``jitter_tolerance`` of it.
    ``signal`` and ``unit`` are carried through unchanged; the output has the
    same unit as the input.
    """

    params = _checked_params(window_samples, alignment, jitter_tolerance, gap_ns)
    if len(times_ns) != len(values):
        raise ValueError(
            f"times_ns and values must have the same length (got {len(times_ns)} and {len(values)})"
        )

    times = _checked_times(times_ns)
    raw = np.array(values, dtype=np.float64)
    valid = np.isfinite(raw)
    spacing = np.diff(times)
    is_time_gap = (
        spacing >= params.gap_ns if params.gap_ns is not None else np.zeros(spacing.shape, bool)
    )
    period = _nominal_period(spacing, is_time_gap, params.jitter_tolerance)
    if period is not None and params.gap_ns is not None:
        if params.gap_ns <= period * (1.0 + params.jitter_tolerance):
            raise ValueError(
                f"gap_ns ({params.gap_ns}) must be longer than the nominal period "
                f"({period:g} ns) plus its jitter tolerance, or regular spacings "
                "would count as gaps"
            )

    output = np.full(raw.shape, np.nan)
    window = params.window_samples
    kernel = np.ones(window)
    offset = window - 1 if params.alignment == "trailing" else (window - 1) // 2
    segments = 0
    for start, stop in _segments(valid, is_time_gap):
        segments += 1
        if stop - start >= window:
            means = np.convolve(raw[start:stop], kernel, mode="valid") / window
            output[start + offset : start + offset + means.size] = means

    times.setflags(write=False)
    output.setflags(write=False)
    missing = int(raw.size - np.count_nonzero(valid))
    return FilteredSignal(
        filter_name=MOVING_AVERAGE,
        version=MOVING_AVERAGE_VERSION,
        params=params,
        input_count=int(raw.size),
        times_ns=times,
        values=output,
        nominal_period_ns=period,
        segment_count=segments,
        missing_count=missing,
        edge_count=int(np.count_nonzero(valid & np.isnan(output))),
        signal=signal,
        unit=unit,
    )


def _checked_params(
    window_samples: int, alignment: str, jitter_tolerance: float, gap_ns: int | None
) -> MovingAverageParams:
    if not _is_int(window_samples) or window_samples < 1:
        raise ValueError(f"window_samples must be a positive integer, got {window_samples!r}")
    if alignment not in ALIGNMENTS:
        raise ValueError(f"alignment must be one of {ALIGNMENTS}, got {alignment!r}")
    if alignment == "centered" and window_samples % 2 == 0:
        raise ValueError(
            f"a centered window needs an odd number of samples, got {window_samples}; "
            "use an odd window or alignment='trailing'"
        )
    if (
        isinstance(jitter_tolerance, bool)
        or not isinstance(jitter_tolerance, (int, float))
        or not 0.0 <= jitter_tolerance < 1.0
    ):
        raise ValueError(f"jitter_tolerance must be a number in [0, 1), got {jitter_tolerance!r}")
    if gap_ns is not None and (not _is_int(gap_ns) or gap_ns < 1):
        raise ValueError(f"gap_ns must be a positive integer or None, got {gap_ns!r}")
    return MovingAverageParams(
        window_samples=int(window_samples),
        alignment=alignment,
        jitter_tolerance=float(jitter_tolerance),
        gap_ns=None if gap_ns is None else int(gap_ns),
    )


def _is_int(value: object) -> bool:
    return isinstance(value, (int, np.integer)) and not isinstance(value, bool)


def _checked_times(times_ns: Sequence[int]) -> npt.NDArray[np.int64]:
    times = np.array(times_ns)
    if times.size == 0:
        return np.zeros(0, dtype=np.int64)
    if times.dtype.kind not in "iu":
        raise ValueError(f"times_ns must be integer nanoseconds, got {times.dtype} values")
    times = times.astype(np.int64)
    backwards = np.flatnonzero(np.diff(times) <= 0)
    if backwards.size:
        index = int(backwards[0]) + 1
        raise ValueError(
            f"times_ns must be strictly increasing: sample {index} at {times[index]} ns "
            f"follows {times[index - 1]} ns"
        )
    return times


def _nominal_period(
    spacing: npt.NDArray[np.int64], is_time_gap: npt.NDArray[np.bool_], tolerance: float
) -> float | None:
    """Median spacing outside time gaps; raise if any such spacing strays too far."""

    checked = spacing[~is_time_gap]
    if checked.size == 0:
        return None
    period = float(np.median(checked))
    deviation = np.abs(spacing - period) > tolerance * period
    bad = np.flatnonzero(deviation & ~is_time_gap)
    if bad.size:
        index = int(bad[0])
        raise IrregularSamplingError(
            f"sample spacing {int(spacing[index])} ns between samples {index} and "
            f"{index + 1} differs from the nominal period {period:g} ns by more than "
            f"{tolerance:g} of it ({bad.size} spacing(s) affected); resample first, "
            "raise jitter_tolerance, or set gap_ns to split at long gaps"
        )
    return period


def _segments(
    valid: npt.NDArray[np.bool_], is_time_gap: npt.NDArray[np.bool_]
) -> list[tuple[int, int]]:
    """Half-open index ranges of consecutive valid samples not split by a time gap."""

    if valid.size == 0:
        return []
    starts_new = np.ones(valid.size, dtype=bool)
    starts_new[1:] = ~valid[:-1] | is_time_gap
    starts = np.flatnonzero(valid & starts_new)
    ends_here = np.ones(valid.size, dtype=bool)
    ends_here[:-1] = ~valid[1:] | is_time_gap
    stops = np.flatnonzero(valid & ends_here) + 1
    return list(zip(starts.tolist(), stops.tolist()))
