"""Amplitude spectrum and PSD: scaling, windows, detrend and sampling rejection."""

from array import array
from dataclasses import FrozenInstanceError
from math import inf, nan

import numpy as np
import pytest

from lbr_dashboard.core.spectrum import (
    DEFAULT_MAX_JITTER,
    MIN_SAMPLES,
    SCALING,
    SamplingError,
    Window,
    spectrum,
)

# On-bin sines: amplitude is exact up to float rounding for both windows.
ON_BIN_REL = 1e-9
# Half-bin offset (worst case): the peak bin loses up to 36.3 % without a window
# (scalloping loss -3.92 dB) and up to 15.1 % with Hann (-1.42 dB).
HANN_SCALLOP_REL = 0.16
RECT_SCALLOP_REL = 0.37


def _times(count: int, rate_hz: float, start_ns: int = 0) -> list[int]:
    step = 1e9 / rate_hz
    return [start_ns + round(index * step) for index in range(count)]


def _sine(
    count: int, rate_hz: float, freq_hz: float, amp: float, offset: float = 0.0
) -> list[float]:
    t = np.arange(count) / rate_hz
    return list(offset + amp * np.sin(2 * np.pi * freq_hz * t))


@pytest.mark.parametrize(
    "window", [pytest.param("none", id="rect"), pytest.param("hann", id="hann")]
)
def test_on_bin_sine_frequency_and_amplitude_are_exact(window: Window) -> None:
    rate, count = 1000.0, 1000
    result = spectrum(_times(count, rate), _sine(count, rate, 50.0, 2.5), unit="V", window=window)
    peak = result.peak
    assert peak.frequency_hz == pytest.approx(50.0, abs=result.resolution_hz / 2)
    assert peak.amplitude == pytest.approx(2.5, rel=ON_BIN_REL)
    assert result.unit == result.amplitude_unit == "V"
    assert result.psd_unit == "V^2/Hz"


@pytest.mark.parametrize(
    ("window", "tolerance"),
    [
        pytest.param("none", RECT_SCALLOP_REL, id="rect"),
        pytest.param("hann", HANN_SCALLOP_REL, id="hann"),
    ],
)
def test_half_bin_sine_is_within_documented_scalloping_loss(
    window: Window, tolerance: float
) -> None:
    rate, count = 1000.0, 1000  # resolution 1 Hz, so 50.5 Hz is half way between bins
    result = spectrum(_times(count, rate), _sine(count, rate, 50.5, 1.0), window=window)
    peak = result.peak
    assert abs(peak.frequency_hz - 50.5) <= result.resolution_hz
    assert 1.0 - tolerance <= peak.amplitude <= 1.0


def test_hann_scalloping_is_smaller_than_rect() -> None:
    rate, count = 1000.0, 1000
    times, values = _times(count, rate), _sine(count, rate, 50.5, 1.0)
    rect = spectrum(times, values, window="none").peak.amplitude
    hann = spectrum(times, values, window="hann").peak.amplitude
    assert rect == pytest.approx(2 / np.pi, rel=0.01)
    assert hann == pytest.approx(0.8488, rel=0.01)


@pytest.mark.parametrize(
    "window", [pytest.param("none", id="rect"), pytest.param("hann", id="hann")]
)
def test_dc_level_is_the_zero_hertz_amplitude(window: Window) -> None:
    times = _times(64, 100.0)
    result = spectrum(times, [3.0] * 64, window=window, detrend="none")
    assert result.frequencies_hz[0] == 0.0
    assert result.amplitude[0] == pytest.approx(3.0, rel=ON_BIN_REL)
    # Hann spreads DC into bin 1 as well; the peak search skips that lobe.
    lobe = 1 if window == "none" else 2
    assert float(np.max(result.amplitude[lobe:])) < 1e-12
    assert result.peak.amplitude < 1e-12


def test_mean_detrend_removes_dc_and_keeps_the_sine() -> None:
    rate, count = 200.0, 400
    result = spectrum(_times(count, rate), _sine(count, rate, 10.0, 1.5, offset=40.0))
    assert result.detrend == "mean"
    assert result.amplitude[0] == pytest.approx(0.0, abs=1e-9)
    assert result.peak.frequency_hz == pytest.approx(10.0)
    assert result.peak.amplitude == pytest.approx(1.5, rel=1e-6)


def test_linear_detrend_removes_a_ramp() -> None:
    rate, count = 200.0, 400
    ramp = np.arange(count) * 0.25
    values = list(np.array(_sine(count, rate, 20.0, 1.0)) + ramp)
    with_ramp = spectrum(_times(count, rate), values, detrend="mean", window="none")
    without = spectrum(_times(count, rate), values, detrend="linear", window="none")
    # A ramp leaks into the lowest bins and can outshout the sine.
    assert with_ramp.peak.frequency_hz < 2.0
    assert without.peak.frequency_hz == pytest.approx(20.0)
    assert without.peak.amplitude == pytest.approx(1.0, rel=0.05)


def test_parseval_holds_exactly_without_a_window() -> None:
    rng = np.random.default_rng(68)
    for count in (1024, 1025):
        values = list(rng.normal(0.2, 1.3, count))
        result = spectrum(_times(count, 500.0), values, window="none", detrend="none")
        mean_square = float(np.mean(np.square(values)))
        total_power = float(np.sum(result.psd)) * result.resolution_hz
        assert total_power == pytest.approx(mean_square, rel=1e-9)


@pytest.mark.parametrize(
    "window", [pytest.param("none", id="rect"), pytest.param("hann", id="hann")]
)
def test_white_noise_psd_level_matches_its_variance(window: Window) -> None:
    rng = np.random.default_rng(42)
    rate, count, sigma = 2000.0, 2**16, 0.5
    result = spectrum(_times(count, rate), list(rng.normal(0.0, sigma, count)), window=window)
    # One-sided white-noise density is 2 * sigma^2 / fs.
    level = float(np.mean(result.psd[1:-1]))
    assert level == pytest.approx(2 * sigma**2 / rate, rel=0.03)


def test_nyquist_bin_is_not_doubled_for_even_counts() -> None:
    count = 32
    values = [2.0 * (-1) ** index for index in range(count)]
    result = spectrum(_times(count, 100.0), values, window="none", detrend="none")
    assert result.frequencies_hz[-1] == pytest.approx(result.nyquist_hz)
    assert result.amplitude[-1] == pytest.approx(2.0)


def test_odd_count_sine_and_bins() -> None:
    rate, count = 999.0, 999
    result = spectrum(_times(count, rate), _sine(count, rate, 100.0, 0.75))
    assert len(result.frequencies_hz) == count // 2 + 1
    assert result.frequencies_hz[-1] < result.nyquist_hz
    assert result.peak.amplitude == pytest.approx(0.75, rel=1e-6)


def test_metadata_is_explicit() -> None:
    result = spectrum(_times(1000, 250.0, start_ns=-2_000_000_000), [0.0] * 1000, unit="kPa")
    assert result.sample_rate_hz == pytest.approx(250.0)
    assert result.resolution_hz == pytest.approx(0.25)
    assert result.nyquist_hz == pytest.approx(125.0)
    assert result.sample_count == 1000
    assert (result.window, result.detrend) == ("hann", "mean")
    assert result.coherent_gain == pytest.approx(0.5)
    assert result.noise_bandwidth_bins == pytest.approx(1.5)
    assert result.max_jitter == 0.0
    assert result.start_ns == -2_000_000_000
    assert result.end_ns == -2_000_000_000 + 999 * 4_000_000
    assert result.scaling == SCALING
    assert len(result.frequencies_hz) == len(result.amplitude) == len(result.psd) == 501
    rect = spectrum(_times(16, 10.0), [1.0] * 16, window="none")
    assert (rect.coherent_gain, rect.noise_bandwidth_bins) == (1.0, 1.0)


def test_two_unrelated_signals_keep_their_own_identity_and_units() -> None:
    wheel = spectrum(_times(500, 100.0), _sine(500, 100.0, 7.0, 120.0, offset=900.0), unit="rpm")
    accel = spectrum(
        _times(4000, 1000.0, start_ns=5_000_000_000),
        _sine(4000, 1000.0, 120.0, 0.8),
        unit="m/s^2",
        window="none",
        detrend="linear",
    )
    assert (wheel.peak.frequency_hz, accel.peak.frequency_hz) == (
        pytest.approx(7.0),
        pytest.approx(120.0),
    )
    assert wheel.peak.amplitude == pytest.approx(120.0, rel=1e-6)
    assert accel.peak.amplitude == pytest.approx(0.8, rel=1e-5)
    assert (wheel.unit, wheel.psd_unit) == ("rpm", "rpm^2/Hz")
    assert (accel.unit, accel.psd_unit) == ("m/s^2", "(m/s^2)^2/Hz")
    assert (wheel.sample_rate_hz, accel.sample_rate_hz) == (
        pytest.approx(100.0),
        pytest.approx(1000.0),
    )
    assert accel.start_ns == 5_000_000_000
    assert spectrum(_times(8, 1.0), [0.0] * 8).psd_unit == "1/Hz"


def test_array_inputs_and_integer_nanosecond_rounding() -> None:
    rate, count = 3000.0, 3000  # 333333.33 ns spacing, rounded to whole ns
    times = array("q", _times(count, rate))
    values = array("d", _sine(count, rate, 300.0, 1.0))
    result = spectrum(times, values)
    assert result.max_jitter < 1e-5
    assert result.sample_rate_hz == pytest.approx(rate, rel=1e-6)
    assert result.peak.frequency_hz == pytest.approx(300.0, rel=1e-6)


def test_result_is_immutable() -> None:
    result = spectrum(_times(16, 10.0), [1.0] * 16)
    with pytest.raises(FrozenInstanceError):
        result.unit = "V"  # type: ignore[misc]
    with pytest.raises(ValueError):
        result.amplitude[0] = 1.0


def _with(values: list, index: int, value: object) -> list:
    copy = list(values)
    copy[index] = value
    return copy


_T = _times(100, 100.0)
_V = [0.0] * 100


@pytest.mark.parametrize(
    ("times", "values", "match"),
    [
        pytest.param(_T, _with(_V, 5, None), "missing or non-finite.*index 5", id="none-gap"),
        pytest.param(_T, _with(_V, 7, nan), "missing or non-finite.*index 7", id="nan-gap"),
        pytest.param(_T, _with(_V, 9, inf), "missing or non-finite.*index 9", id="inf"),
        pytest.param(_with(_T, 3, _T[2]), _V, "duplicate timestamp at index 3", id="duplicate"),
        pytest.param(_with(_T, 4, _T[2]), _V, "decreasing timestamp at index 4", id="decreasing"),
        pytest.param(_T[:50] + _T[51:], _V[:99], "not uniform.*index 50", id="dropped-sample"),
        pytest.param(
            _with(_T, 10, _T[10] + 200_000), _V, "not uniform.*tolerance 1.000%", id="jitter-2pct"
        ),
        pytest.param(_T[: MIN_SAMPLES - 1], _V[: MIN_SAMPLES - 1], "at least 8", id="too-few"),
        pytest.param([], [], "at least 8", id="empty"),
    ],
)
def test_invalid_sampling_is_rejected(times: list[int], values: list, match: str) -> None:
    with pytest.raises(SamplingError, match=match) as raised:
        spectrum(times, values)
    assert isinstance(raised.value, ValueError)
    if "at least" not in match:
        assert "resample" in str(raised.value)


def test_jitter_within_tolerance_is_accepted_and_reported() -> None:
    times = _with(_T, 10, _T[10] + 50_000)  # 0.5 % of the 10 ms spacing
    result = spectrum(times, _V)
    assert 0.0 < result.max_jitter <= DEFAULT_MAX_JITTER
    loose = spectrum(_with(_T, 10, _T[10] + 200_000), _V, max_jitter=0.05)
    assert loose.max_jitter == pytest.approx(0.02, rel=0.01)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        pytest.param({"window": "hamming"}, "window must be", id="window"),
        pytest.param({"detrend": "quadratic"}, "detrend must be", id="detrend"),
        pytest.param({"max_jitter": -0.1}, "max_jitter", id="jitter-negative"),
        pytest.param({"max_jitter": 0.5}, "max_jitter", id="jitter-large"),
    ],
)
def test_bad_options_are_rejected(kwargs: dict, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        spectrum(_T, _V, **kwargs)


def test_mismatched_lengths_are_a_plain_value_error() -> None:
    with pytest.raises(ValueError, match="same length") as raised:
        spectrum(_T, _V[:-1])
    assert not isinstance(raised.value, SamplingError)
