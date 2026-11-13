"""Moving average: reference outputs, sampling checks, gaps, edges and provenance."""

from __future__ import annotations

from array import array
from collections.abc import Sequence
from dataclasses import FrozenInstanceError
from math import inf, isfinite, nan

import numpy as np
import pytest

from lbr_dashboard.core.filters import (
    MOVING_AVERAGE,
    MOVING_AVERAGE_VERSION,
    FilteredSignal,
    IrregularSamplingError,
    MovingAverageParams,
    moving_average,
)

MS = 1_000_000


def _times(count: int, step_ns: int = 10 * MS, start_ns: int = 0) -> list[int]:
    return [start_ns + index * step_ns for index in range(count)]


def _reference(
    times: Sequence[int],
    values: Sequence[float | None],
    window: int,
    alignment: str,
    gap_ns: int | None = None,
) -> list[float | None]:
    """Plain loop: average a full window of valid, gap-free samples, else None."""

    def usable(index: int) -> bool:
        value = values[index]
        return value is not None and isfinite(value)

    half = (window - 1) // 2
    out: list[float | None] = []
    for index in range(len(values)):
        first, last = (
            (index - window + 1, index)
            if alignment == "trailing"
            else (
                index - half,
                index + half,
            )
        )
        if first < 0 or last >= len(values) or not usable(index):
            out.append(None)
            continue
        members = range(first, last + 1)
        split = gap_ns is not None and any(
            times[k + 1] - times[k] >= gap_ns for k in range(first, last)
        )
        if split or not all(usable(k) for k in members):
            out.append(None)
            continue
        total = 0.0
        for k in members:
            total += values[k]  # type: ignore[operator]
        out.append(total / window)
    return out


def _assert_matches(actual: list[float | None], expected: list[float | None]) -> None:
    assert [value is None for value in actual] == [value is None for value in expected]
    for got, want in zip(actual, expected):
        if want is not None:
            assert got == pytest.approx(want, rel=1e-12, abs=1e-12)


# A deterministic, non-trivial signal with missing samples of every kind.
SIGNAL: list[float | None] = [
    3.0, 1.5, -2.0, 4.25, None, 7.0, 6.5, 6.0, 5.5, nan, 2.0, 2.5,
    3.5, 10.0, -1.0, 0.0, inf, 8.0, 8.5, 9.0, 9.5, 10.0, -inf, 1.0,
]  # fmt: skip


def test_hand_computed_centered_and_trailing_outputs() -> None:
    times = _times(6)
    values = [1.0, 2.0, 4.0, 8.0, 16.0, 32.0]
    centered = moving_average(times, values, window_samples=3)
    assert centered.values_or_none() == [None, 7 / 3, 14 / 3, 28 / 3, 56 / 3, None]
    trailing = moving_average(times, values, window_samples=3, alignment="trailing")
    assert trailing.values_or_none() == [None, None, 7 / 3, 14 / 3, 28 / 3, 56 / 3]
    assert centered.edge_count == 2 and trailing.edge_count == 2
    assert centered.output_count == trailing.output_count == 4


@pytest.mark.parametrize(
    ("window", "alignment"),
    [
        pytest.param(1, "centered", id="w1-centered"),
        pytest.param(3, "centered", id="w3-centered"),
        pytest.param(5, "centered", id="w5-centered"),
        pytest.param(1, "trailing", id="w1-trailing"),
        pytest.param(2, "trailing", id="w2-trailing"),
        pytest.param(4, "trailing", id="w4-trailing"),
    ],
)
def test_matches_reference_loop_with_missing_samples(window: int, alignment: str) -> None:
    times = _times(len(SIGNAL))
    result = moving_average(times, SIGNAL, window_samples=window, alignment=alignment)
    _assert_matches(result.values_or_none(), _reference(times, SIGNAL, window, alignment))
    assert result.missing_count == 4
    assert result.segment_count == 5


def test_window_of_one_returns_valid_values_unchanged() -> None:
    result = moving_average(_times(len(SIGNAL)), SIGNAL, window_samples=1)
    expected = [v if v is not None and isfinite(v) else None for v in SIGNAL]
    assert result.values_or_none() == expected
    assert result.edge_count == 0


def test_constant_signal_and_ramp_phase() -> None:
    times = _times(9)
    constant = moving_average(times, [4.5] * 9, window_samples=5)
    assert constant.values_or_none()[2:7] == [4.5] * 5

    ramp = [float(index) for index in range(9)]
    centered = moving_average(times, ramp, window_samples=5)
    # Centered averaging of a straight line reproduces it: no delay.
    assert centered.values_or_none()[2:7] == ramp[2:7]
    assert centered.delay_ns == 0.0
    trailing = moving_average(times, ramp, window_samples=5, alignment="trailing")
    # Trailing output lags by (window - 1) / 2 samples.
    assert trailing.values_or_none()[4:] == [value - 2.0 for value in ramp[4:]]
    assert trailing.delay_ns == 2 * 10 * MS
    assert trailing.window_span_ns == 5 * 10 * MS


def test_missing_values_split_segments_and_are_never_averaged() -> None:
    values = [1.0, 2.0, 3.0, None, 5.0, 6.0, 7.0, nan, 9.0]
    result = moving_average(_times(len(values)), values, window_samples=3)
    assert result.values_or_none() == [None, 2.0, None, None, None, 6.0, None, None, None]
    assert result.segment_count == 3
    assert result.missing_count == 2
    assert result.edge_count == 5  # 1, 3, 5, 7 and the lone 9


def test_segment_shorter_than_window_gets_no_output() -> None:
    values = [1.0, 2.0, None, 3.0, 4.0, 5.0, 6.0]
    result = moving_average(_times(len(values)), values, window_samples=5)
    assert result.values_or_none() == [None] * 7
    assert result.output_count == 0


def test_all_missing_input() -> None:
    result = moving_average(_times(3), [None, nan, inf], window_samples=1)
    assert result.values_or_none() == [None, None, None]
    assert (result.segment_count, result.missing_count, result.edge_count) == (0, 3, 0)


def test_empty_and_single_sample_inputs() -> None:
    empty = moving_average([], [], window_samples=3)
    assert empty.input_count == 0 and empty.values.size == 0
    assert empty.nominal_period_ns is None
    assert empty.window_span_ns is None
    assert empty.delay_ns == 0.0

    single = moving_average([5], [2.0], window_samples=1, alignment="trailing")
    assert single.values_or_none() == [2.0]
    assert single.nominal_period_ns is None
    assert single.delay_ns is None


def test_jitter_within_tolerance_is_accepted_with_equal_weights() -> None:
    offsets = [0, 3, -2, 3, -3, 1, 0, -3, 2, 1]
    times = [index * 100 + offset for index, offset in enumerate(offsets)]
    values = [float(index * index) for index in range(len(times))]
    result = moving_average(times, values, window_samples=3, jitter_tolerance=0.1)
    assert result.nominal_period_ns is not None
    assert abs(result.nominal_period_ns - 100) <= 10
    _assert_matches(result.values_or_none(), _reference(times, values, 3, "centered"))


@pytest.mark.parametrize(
    "times",
    [
        pytest.param([0, 100, 200, 330, 400, 500], id="late-sample"),
        pytest.param([0, 100, 200, 260, 400, 500], id="early-sample"),
        pytest.param([0, 100, 200, 300, 600, 700], id="dropout-no-gap-ns"),
    ],
)
def test_irregular_sampling_is_rejected(times: list[int]) -> None:
    with pytest.raises(IrregularSamplingError, match="nominal period 100 ns") as caught:
        moving_average(times, [1.0] * len(times), window_samples=3)
    assert isinstance(caught.value, ValueError)


def test_zero_tolerance_requires_exact_spacing() -> None:
    moving_average([0, 10, 20], [1.0, 2.0, 3.0], window_samples=1, jitter_tolerance=0)
    with pytest.raises(IrregularSamplingError):
        moving_average([0, 10, 21], [1.0, 2.0, 3.0], window_samples=1, jitter_tolerance=0)


def test_time_gaps_split_segments_when_gap_ns_is_set() -> None:
    times = [0, 100, 200, 300, 1000, 1100, 1200, 5000, 5100, 5200, 5300]
    values = [float(index) for index in range(len(times))]
    result = moving_average(times, values, window_samples=3, gap_ns=500)
    expected = _reference(times, values, 3, "centered", gap_ns=500)
    _assert_matches(result.values_or_none(), expected)
    assert result.values_or_none() == [None, 1.0, 2.0, None, None, 5.0, None, None, 8.0, 9.0, None]
    assert result.segment_count == 3
    assert result.nominal_period_ns == 100.0


def test_gap_ns_too_close_to_the_period_is_rejected() -> None:
    with pytest.raises(ValueError, match="gap_ns"):
        moving_average(_times(5, 100), [1.0] * 5, window_samples=3, gap_ns=105)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        pytest.param({"window_samples": 0}, "positive integer", id="window-zero"),
        pytest.param({"window_samples": -3}, "positive integer", id="window-negative"),
        pytest.param({"window_samples": True}, "positive integer", id="window-bool"),
        pytest.param({"window_samples": 3.0}, "positive integer", id="window-float"),
        pytest.param({"window_samples": 4}, "odd number", id="window-even-centered"),
        pytest.param({"window_samples": 3, "alignment": "leading"}, "alignment", id="align"),
        pytest.param({"window_samples": 3, "jitter_tolerance": -0.1}, "jitter", id="tol-neg"),
        pytest.param({"window_samples": 3, "jitter_tolerance": 1.0}, "jitter", id="tol-one"),
        pytest.param({"window_samples": 3, "jitter_tolerance": nan}, "jitter", id="tol-nan"),
        pytest.param({"window_samples": 3, "jitter_tolerance": True}, "jitter", id="tol-bool"),
        pytest.param({"window_samples": 3, "jitter_tolerance": "0.1"}, "jitter", id="tol-str"),
        pytest.param({"window_samples": 3, "gap_ns": 0}, "gap_ns", id="gap-zero"),
        pytest.param({"window_samples": 3, "gap_ns": 1.5}, "gap_ns", id="gap-float"),
    ],
)
def test_invalid_parameters_are_rejected(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        moving_average(_times(5), [1.0] * 5, **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("times", "values", "message"),
    [
        pytest.param([0, 10], [1.0], "same length", id="length"),
        pytest.param([0, 10, 10], [1.0] * 3, "strictly increasing", id="duplicate"),
        pytest.param([0, 20, 10], [1.0] * 3, "sample 2 at 10 ns", id="backwards"),
        pytest.param([0.0, 0.5, 1.0], [1.0] * 3, "integer nanoseconds", id="float-times"),
    ],
)
def test_invalid_inputs_are_rejected(times: list[int], values: list[float], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        moving_average(times, values, window_samples=1)


def test_even_window_is_fine_when_trailing_and_numpy_ints_are_accepted() -> None:
    result = moving_average(
        np.arange(4, dtype=np.uint32) * 10,  # type: ignore[arg-type]
        [2.0, 4.0, 6.0, 8.0],
        window_samples=np.int64(2),  # type: ignore[arg-type]
        alignment="trailing",
        gap_ns=np.int32(1000),  # type: ignore[arg-type]
    )
    assert result.values_or_none() == [None, 3.0, 5.0, 7.0]
    assert type(result.params.window_samples) is int
    assert type(result.params.gap_ns) is int


def test_two_unrelated_signals_keep_their_identity_units_and_time() -> None:
    # Engine speed: 100 Hz, absolute epoch nanoseconds.
    rpm_times = _times(8, 10 * MS, start_ns=1_700_000_000_000_000_000)
    rpm_values = [3000.0, 3100.0, 3050.0, None, 3200.0, 3300.0, 3250.0, 3400.0]
    # Coolant temperature: 1 Hz, starting before the time origin.
    temp_times = _times(6, 1000 * MS, start_ns=-2_000 * MS)
    temp_values = [80.0, 80.5, 81.0, 81.5, 82.0, 82.5]

    rpm = moving_average(rpm_times, rpm_values, window_samples=3, signal="engine.rpm", unit="rpm")
    temp = moving_average(
        temp_times,
        temp_values,
        window_samples=2,
        alignment="trailing",
        signal="coolant.temperature",
        unit="deg C",
    )

    assert (rpm.signal, rpm.unit, temp.signal, temp.unit) == (
        "engine.rpm",
        "rpm",
        "coolant.temperature",
        "deg C",
    )
    assert rpm.times_ns.tolist() == rpm_times
    assert temp.times_ns.tolist() == temp_times
    assert rpm.nominal_period_ns == 10 * MS
    assert temp.nominal_period_ns == 1000 * MS
    _assert_matches(rpm.values_or_none(), _reference(rpm_times, rpm_values, 3, "centered"))
    assert temp.values_or_none() == [None, 80.25, 80.75, 81.25, 81.75, 82.25]


def test_raw_input_is_never_changed_and_output_is_read_only() -> None:
    times = _times(6)
    values: list[float | None] = [1.0, None, 3.0, 4.0, nan, 6.0]
    times_before, values_before = list(times), list(values)
    packed_times = array("q", times)
    packed_values = array("d", [1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
    packed_before = packed_values.tolist()

    result = moving_average(times, values, window_samples=1)
    packed = moving_average(packed_times, packed_values, window_samples=3)

    assert times == times_before
    assert values[:4] == values_before[:4] and values[5] == values_before[5]
    assert values[4] is values_before[4]
    assert packed_values.tolist() == packed_before
    times[0] = 999
    packed_values[0] = 100.0
    assert result.times_ns[0] == 0
    assert packed.values_or_none()[1] == 2.0
    with pytest.raises(ValueError):
        result.values[0] = 0.0
    with pytest.raises(ValueError):
        result.times_ns[0] = 0
    with pytest.raises(FrozenInstanceError):
        result.signal = "other"  # type: ignore[misc]


def test_provenance_reproduces_the_same_output() -> None:
    times = [0, 100, 200, 300, 900, 1000, 1100, 1200]
    values = [5.0, 7.0, 6.0, 8.0, 1.0, 2.0, 3.0, 4.0]
    first = moving_average(
        times,
        values,
        window_samples=2,
        alignment="trailing",
        jitter_tolerance=0.05,
        gap_ns=400,
        signal="shock.front_left",
        unit="mm",
    )
    assert isinstance(first, FilteredSignal)
    assert first.provenance() == {
        "filter": MOVING_AVERAGE,
        "version": MOVING_AVERAGE_VERSION,
        "params": {
            "window_samples": 2,
            "alignment": "trailing",
            "jitter_tolerance": 0.05,
            "gap_ns": 400,
        },
        "input_count": 8,
        "signal": "shock.front_left",
        "unit": "mm",
    }
    again = moving_average(times, values, **first.params.as_dict())
    assert again.params == first.params == MovingAverageParams(2, "trailing", 0.05, 400)
    np.testing.assert_array_equal(again.values, first.values)
