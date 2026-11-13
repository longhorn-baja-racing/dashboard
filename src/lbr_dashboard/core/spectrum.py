"""One-sided amplitude spectrum and PSD of one uniformly sampled numeric signal.

Inputs are plain parallel sequences: ``times_ns`` (integer nanoseconds) and
``values`` (floats, with ``None`` or NaN meaning missing). The transform is
``numpy.fft.rfft``. See docs/spectrum.md for the scaling and sampling rules.

The FFT assumes equally spaced, gap-free samples. Input that is not is
rejected with :class:`SamplingError`; it is never resampled here. Callers that
want a spectrum of such a signal must resample it explicitly first.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np
import numpy.typing as npt

Window = Literal["none", "hann"]
Detrend = Literal["none", "mean", "linear"]

WINDOWS: tuple[str, ...] = ("none", "hann")
DETRENDS: tuple[str, ...] = ("none", "mean", "linear")
MIN_SAMPLES = 8
DEFAULT_MAX_JITTER = 0.01
SCALING = (
    "amplitude: one-sided peak amplitude in signal units, divided by the window sum "
    "(coherent gain corrected); psd: one-sided power spectral density in unit^2/Hz, "
    "divided by sample rate times the sum of squared window values (noise power corrected)"
)
# Bins taken up by the DC main lobe of each window; the peak search skips them.
DC_LOBE_BINS = {"none": 1, "hann": 2}
RESAMPLE_HINT = "resample the signal to a uniform rate explicitly before computing a spectrum"

FloatArray = npt.NDArray[np.float64]


class SamplingError(ValueError):
    """The samples are not uniform and gap-free, so no spectrum is computed."""


@dataclass(frozen=True, slots=True)
class SpectrumPeak:
    """The largest bin outside the DC main lobe: frequency, amplitude and PSD."""

    frequency_hz: float
    amplitude: float
    psd: float


@dataclass(frozen=True, slots=True, eq=False)
class Spectrum:
    """Spectrum of one signal plus everything needed to read it correctly.

    ``frequencies_hz``, ``amplitude`` and ``psd`` are read-only arrays of the
    same length (``sample_count // 2 + 1``), from 0 Hz to the last bin at or
    below Nyquist. ``amplitude`` is in ``unit``; ``psd`` is in ``psd_unit``.
    """

    frequencies_hz: FloatArray
    amplitude: FloatArray
    psd: FloatArray
    unit: str
    sample_rate_hz: float
    sample_count: int
    window: str
    detrend: str
    coherent_gain: float
    noise_bandwidth_bins: float
    max_jitter: float
    start_ns: int
    end_ns: int
    scaling: str = SCALING

    @property
    def resolution_hz(self) -> float:
        """Bin spacing: sample rate divided by the number of samples."""

        return self.sample_rate_hz / self.sample_count

    @property
    def nyquist_hz(self) -> float:
        """Half the sample rate; the highest frequency the samples can represent."""

        return self.sample_rate_hz / 2.0

    @property
    def amplitude_unit(self) -> str:
        """Unit of ``amplitude`` (the signal unit)."""

        return self.unit

    @property
    def psd_unit(self) -> str:
        """Unit of ``psd``, for example ``V^2/Hz`` or ``(m/s^2)^2/Hz``."""

        if not self.unit:
            return "1/Hz"
        if any(char in self.unit for char in "/*^ "):
            return f"({self.unit})^2/Hz"
        return f"{self.unit}^2/Hz"

    @property
    def peak(self) -> SpectrumPeak:
        """The bin with the largest amplitude outside the DC main lobe.

        The search skips bin 0 without a window and bins 0 and 1 with Hann,
        where an un-removed DC offset leaks. The frequency is the bin centre,
        so it is only as exact as ``resolution_hz``. No interpolation is applied.
        """

        first = DC_LOBE_BINS[self.window]
        index = first + int(np.argmax(self.amplitude[first:]))
        return SpectrumPeak(
            frequency_hz=float(self.frequencies_hz[index]),
            amplitude=float(self.amplitude[index]),
            psd=float(self.psd[index]),
        )


def spectrum(
    times_ns: Sequence[int],
    values: Sequence[float | None],
    *,
    unit: str = "",
    window: Window = "hann",
    detrend: Detrend = "mean",
    max_jitter: float = DEFAULT_MAX_JITTER,
) -> Spectrum:
    """Compute the one-sided amplitude spectrum and PSD of a uniform signal.

    ``detrend`` ("none", "mean" or "linear") is applied first, then ``window``
    ("none" or a periodic "hann"), then ``numpy.fft.rfft``.

    Raises :class:`SamplingError` (a ``ValueError``) when there are fewer than
    ``MIN_SAMPLES`` samples, any value is missing or non-finite, timestamps are
    not strictly increasing, or any sample spacing differs from the mean
    spacing by more than ``max_jitter`` (a fraction of the mean spacing).
    Raises ``ValueError`` for bad options or mismatched lengths.
    """

    if window not in WINDOWS:
        raise ValueError(f"window must be one of {WINDOWS}, got {window!r}")
    if detrend not in DETRENDS:
        raise ValueError(f"detrend must be one of {DETRENDS}, got {detrend!r}")
    if not 0.0 <= max_jitter < 0.5:
        raise ValueError(f"max_jitter must be in [0, 0.5), got {max_jitter!r}")
    if len(times_ns) != len(values):
        raise ValueError(
            f"times_ns and values must have the same length ({len(times_ns)} != {len(values)})"
        )

    count = len(values)
    if count < MIN_SAMPLES:
        raise SamplingError(f"need at least {MIN_SAMPLES} samples for a spectrum, got {count}")

    samples = np.asarray(values, dtype=np.float64)
    bad = np.flatnonzero(~np.isfinite(samples))
    if bad.size:
        raise SamplingError(
            f"{bad.size} missing or non-finite value(s), first at index {int(bad[0])}; "
            f"fill or drop them and {RESAMPLE_HINT}"
        )

    times = np.asarray(times_ns, dtype=np.int64)
    steps = np.diff(times)
    not_increasing = np.flatnonzero(steps <= 0)
    if not_increasing.size:
        index = int(not_increasing[0]) + 1
        kind = "duplicate" if steps[index - 1] == 0 else "decreasing"
        raise SamplingError(
            f"timestamps must be strictly increasing; {kind} timestamp at index {index}; "
            f"sort and de-duplicate the samples and {RESAMPLE_HINT}"
        )

    span_ns = int(times[-1] - times[0])
    mean_step = span_ns / (count - 1)
    jitter = float(np.max(np.abs(steps - mean_step)) / mean_step)
    if jitter > max_jitter:
        worst = int(np.argmax(np.abs(steps - mean_step))) + 1
        raise SamplingError(
            f"sample spacing is not uniform: spacing before index {worst} differs from the "
            f"mean spacing by {jitter:.3%} (tolerance {max_jitter:.3%}), which can mean a "
            f"gap or irregular timing; {RESAMPLE_HINT}"
        )

    sample_rate = 1e9 / mean_step
    detrended = _detrend(samples, detrend)
    weights = _window(count, window)
    window_sum = float(np.sum(weights))
    window_power = float(np.sum(weights * weights))

    transform = np.fft.rfft(detrended * weights)
    magnitude = np.abs(transform)
    # Double every bin except DC and, for an even count, the Nyquist bin: those
    # two have no mirrored negative-frequency partner.
    one_sided = np.full(magnitude.shape, 2.0)
    one_sided[0] = 1.0
    if count % 2 == 0:
        one_sided[-1] = 1.0

    amplitude = magnitude * one_sided / window_sum
    psd = magnitude**2 * one_sided / (sample_rate * window_power)
    frequencies = np.fft.rfftfreq(count, d=1.0 / sample_rate).astype(np.float64)
    for array in (frequencies, amplitude, psd):
        array.setflags(write=False)

    return Spectrum(
        frequencies_hz=frequencies,
        amplitude=amplitude,
        psd=psd,
        unit=unit,
        sample_rate_hz=sample_rate,
        sample_count=count,
        window=window,
        detrend=detrend,
        coherent_gain=window_sum / count,
        noise_bandwidth_bins=count * window_power / window_sum**2,
        max_jitter=jitter,
        start_ns=int(times[0]),
        end_ns=int(times[-1]),
    )


def _detrend(samples: FloatArray, detrend: str) -> FloatArray:
    if detrend == "mean":
        return samples - np.mean(samples)
    if detrend == "linear":
        index = np.arange(samples.size, dtype=np.float64)
        slope, intercept = np.polyfit(index, samples, 1)
        return samples - (slope * index + intercept)
    return samples


def _window(count: int, window: str) -> FloatArray:
    if window == "hann":
        # Periodic Hann (the DFT-even form used for spectral analysis), not the
        # symmetric numpy.hanning used for filter design.
        return 0.5 - 0.5 * np.cos(2.0 * np.pi * np.arange(count) / count)
    return np.ones(count)
