"""Hand-calculated checks for interval statistics and current-value states."""

import time
from array import array
from dataclasses import FrozenInstanceError

import pytest

from lbr_dashboard.core.statistics import (
    COUNT_DEFINITION_TEXT,
    NO_VALID_TEXT,
    WEIGHTING_TEXT,
    CurrentValue,
    CurrentValueState,
    current_value,
    format_current_value,
    format_number,
    format_seconds,
    format_statistics,
    interval_statistics,
)

NAN = float("nan")
INF = float("inf")
S = 1_000_000_000

# t (s):    0     1     2     3     4    5
# value:    2  -4.5  None  7.25   NaN   -1
TIMES = [0, 1 * S, 2 * S, 3 * S, 4 * S, 5 * S]
VALUES = [2.0, -4.5, None, 7.25, NAN, -1.0]


def test_known_values_with_negatives() -> None:
    stats = interval_statistics(TIMES, VALUES, 0, 5 * S, unit="V")
    # Valid samples: 2, -4.5, 7.25, -1 -> sum 3.75, mean 3.75 / 4 = 0.9375.
    assert stats.valid_count == 4
    assert stats.missing_count == 2
    assert stats.invalid_count == 0
    assert stats.sample_count == 6
    assert stats.minimum == -4.5
    assert stats.maximum == 7.25
    assert stats.mean == 0.9375
    assert stats.first_valid_ns == 0
    assert stats.last_valid_ns == 5 * S
    assert stats.unit == "V"
    assert stats.weighting == "sample"


def test_sub_interval_and_valid_time_range() -> None:
    stats = interval_statistics(TIMES, VALUES, 1 * S, 4 * S)
    # Samples at 1..4 s: -4.5, None, 7.25, NaN -> mean (7.25 - 4.5) / 2 = 1.375.
    assert (stats.valid_count, stats.missing_count) == (2, 2)
    assert (stats.minimum, stats.maximum, stats.mean) == (-4.5, 7.25, 1.375)
    assert (stats.first_valid_ns, stats.last_valid_ns) == (1 * S, 3 * S)


def test_interval_is_closed_at_both_ends() -> None:
    stats = interval_statistics(TIMES, VALUES, 3 * S, 5 * S)
    assert stats.sample_count == 3
    assert (stats.valid_count, stats.missing_count) == (2, 1)
    assert stats.mean == 3.125

    single = interval_statistics(TIMES, VALUES, 3 * S, 3 * S)
    assert (single.valid_count, single.mean) == (1, 7.25)

    inside = interval_statistics(TIMES, VALUES, 3 * S + 1, 5 * S - 1)
    assert (inside.valid_count, inside.missing_count) == (0, 1)


@pytest.mark.parametrize(
    ("times", "values", "start", "end"),
    [
        ([], [], 0, 10),
        (TIMES, VALUES, 6 * S, 7 * S),
        (TIMES, VALUES, -2 * S, -1),
        (TIMES, VALUES, S + 1, 2 * S - 1),
    ],
)
def test_empty_interval_has_no_statistics(
    times: list[int], values: list[float | None], start: int, end: int
) -> None:
    stats = interval_statistics(times, values, start, end)
    assert stats.sample_count == 0
    assert stats.minimum is None
    assert stats.maximum is None
    assert stats.mean is None
    assert stats.first_valid_ns is None
    assert stats.last_valid_ns is None


def test_all_missing_interval_never_reports_zero() -> None:
    stats = interval_statistics([0, 1, 2, 3], [None, NAN, INF, None], 0, 3)
    assert (stats.valid_count, stats.missing_count, stats.invalid_count) == (0, 3, 1)
    assert stats.minimum is None
    assert stats.maximum is None
    assert stats.mean is None


def test_nan_none_and_infinity_are_classified_separately() -> None:
    stats = interval_statistics([0, 1, 2, 3, 4], [None, NAN, INF, -INF, -2.5], 0, 4)
    assert stats.missing_count == 2
    assert stats.invalid_count == 2
    assert stats.valid_count == 1
    assert (stats.minimum, stats.maximum, stats.mean) == (-2.5, -2.5, -2.5)


def test_mixed_rate_signals_have_independent_counts() -> None:
    fast_times = list(range(0, S + 1, S // 100))  # 100 Hz, 101 samples in [0, 1 s]
    fast = interval_statistics(fast_times, [float(i) for i in range(101)], 0, S)
    slow_times = list(range(0, S + 1, S // 10))  # 10 Hz, 11 samples in [0, 1 s]
    slow = interval_statistics(slow_times, [10.0 * i for i in range(11)], 0, S)
    assert (fast.valid_count, fast.mean, fast.maximum) == (101, 50.0, 100.0)
    assert (slow.valid_count, slow.mean, slow.maximum) == (11, 50.0, 100.0)


def test_mean_is_sample_weighted_not_time_weighted() -> None:
    # Two samples of 0 a nanosecond apart, then 10 a second later. Time weighting
    # would give roughly 0; sample weighting gives (0 + 0 + 10) / 3.
    stats = interval_statistics([0, 1, S], [0.0, 0.0, 10.0], 0, S)
    assert stats.mean == 10.0 / 3


def test_duplicate_timestamps_each_count() -> None:
    stats = interval_statistics([0, 5, 5, 10], [1.0, 2.0, 3.0, 4.0], 5, 5)
    assert (stats.valid_count, stats.mean) == (2, 2.5)


def test_mean_survives_a_sum_beyond_float_range() -> None:
    stats = interval_statistics([0, 1], [1e308, 1e308], 0, 1)
    assert stats.mean == 1e308


@pytest.mark.parametrize(
    ("times", "start", "end"),
    [([0, 3, 2, 4], 0, 4), ([0, 2, 1, 5], 0, 1)],
)
def test_unsorted_times_inside_the_interval_are_rejected(
    times: list[int], start: int, end: int
) -> None:
    with pytest.raises(ValueError, match="non-decreasing"):
        interval_statistics(times, [1.0] * len(times), start, end)


def test_bad_arguments_are_rejected() -> None:
    with pytest.raises(ValueError, match="same length"):
        interval_statistics([0, 1], [1.0], 0, 1)
    with pytest.raises(ValueError, match="start_ns must not be after end_ns"):
        interval_statistics([0, 1], [1.0, 2.0], 2, 1)


def test_statistics_are_immutable() -> None:
    stats = interval_statistics(TIMES, VALUES, 0, S)
    with pytest.raises(FrozenInstanceError):
        stats.mean = 0.0  # type: ignore[misc]


def test_large_selection_is_computed_quickly() -> None:
    count = 1_000_000
    times = array("q", range(0, count * 1000, 1000))
    # Repeating block of 0..99 with every value ending in 9 missing: 90 valid values
    # summing to 4950 - 540 = 4410 per block, so the mean is exactly 49.
    values = array("d", (NAN if i % 10 == 9 else float(i % 100) for i in range(count)))

    started = time.perf_counter()
    stats = interval_statistics(times, values, 0, times[-1])
    elapsed = time.perf_counter() - started

    assert (stats.valid_count, stats.missing_count) == (900_000, 100_000)
    assert (stats.minimum, stats.maximum, stats.mean) == (0.0, 98.0, 49.0)
    assert elapsed < 10.0

    started = time.perf_counter()
    narrow = interval_statistics(times, values, 500_000_000, 500_099_000)
    assert time.perf_counter() - started < 0.1
    assert narrow.sample_count == 100


def test_current_value_valid_and_duplicate_timestamp() -> None:
    current = current_value([0, 5, 5, 10], [1.0, 2.0, 3.0, 4.0], 7, unit="bar")
    assert current == CurrentValue(CurrentValueState.VALID, 7, "bar", 5, 3.0, 2, None)


def test_current_value_at_exact_sample_time() -> None:
    current = current_value(TIMES, VALUES, 3 * S)
    assert (current.state, current.value, current.age_ns) == (CurrentValueState.VALID, 7.25, 0)


@pytest.mark.parametrize(("times", "at"), [([], 0), (TIMES, -1)])
def test_current_value_without_any_sample(times: list[int], at: int) -> None:
    current = current_value(times, VALUES[: len(times)], at)
    assert current.state is CurrentValueState.NO_DATA
    assert (current.time_ns, current.value, current.age_ns) == (None, None, None)


@pytest.mark.parametrize("missing", [None, NAN])
def test_missing_latest_sample_does_not_fall_back(missing: float | None) -> None:
    current = current_value([0, S], [5.0, missing], 2 * S)
    assert current.state is CurrentValueState.MISSING
    assert current.value is None
    assert (current.time_ns, current.age_ns) == (S, S)


def test_non_finite_latest_sample_is_invalid() -> None:
    current = current_value([0, 1], [5.0, -INF], 1)
    assert (current.state, current.value) == (CurrentValueState.INVALID, None)


def test_old_valid_sample_is_stale() -> None:
    times, values = [0, S], [1.0, 2.0]
    fresh = current_value(times, values, 2 * S, max_age_ns=S)
    stale = current_value(times, values, 2 * S + 1, max_age_ns=S)
    assert fresh.state is CurrentValueState.VALID
    assert stale.state is CurrentValueState.STALE
    assert (stale.value, stale.age_ns, stale.max_age_ns) == (2.0, S + 1, S)


def test_current_value_rejects_bad_arguments() -> None:
    with pytest.raises(ValueError, match="same length"):
        current_value([0], [], 0)
    with pytest.raises(ValueError, match="max_age_ns"):
        current_value([0], [1.0], 0, max_age_ns=-1)


@pytest.mark.parametrize(
    ("value", "unit", "text"),
    [
        (-4.5, "V", "-4.5 V"),
        (1 / 3, "", "0.333333"),
        (1234567.0, "rpm", "1.23457e+06 rpm"),
        (0.000012345678, "s", "1.23457e-05 s"),
        (0.0, "", "0"),
    ],
)
def test_format_number_bounds_precision(value: float, unit: str, text: str) -> None:
    assert format_number(value, unit) == text


@pytest.mark.parametrize(
    ("time_ns", "text"),
    [(0, "0.000 s"), (1_500_000_000, "1.500 s"), (S + 1, "1.000000001 s"), (-1, "-0.000000001 s")],
)
def test_format_seconds_is_exact(time_ns: int, text: str) -> None:
    assert format_seconds(time_ns) == text


def test_format_statistics_rows() -> None:
    rows = format_statistics(interval_statistics(TIMES, VALUES, 1 * S, 4 * S, unit="V"))
    assert rows == [
        ("Interval", "1.000 s to 4.000 s"),
        ("Duration", "3.000 s"),
        ("Samples", "2 valid, 2 missing, 0 invalid (4 in interval)"),
        ("Minimum", "-4.5 V"),
        ("Maximum", "7.25 V"),
        ("Mean", "1.375 V"),
        ("Weighting", WEIGHTING_TEXT),
        ("Counts", COUNT_DEFINITION_TEXT),
    ]


def test_format_statistics_without_valid_samples() -> None:
    rows = dict(format_statistics(interval_statistics([0], [None], 0, 0)))
    assert rows["Minimum"] == rows["Maximum"] == rows["Mean"] == NO_VALID_TEXT
    assert rows["Samples"] == "0 valid, 1 missing, 0 invalid (1 in interval)"


@pytest.mark.parametrize(
    ("values", "at", "max_age", "expected"),
    [
        (
            [1.5, 2.5],
            2 * S,
            None,
            [
                ("Current value", "2.5 V"),
                ("Current state", "valid: sample at 1.000 s, 1.000 s old"),
            ],
        ),
        (
            [1.5, 2.5],
            3 * S,
            S,
            [
                ("Current value", "2.5 V (stale)"),
                ("Current state", "stale: sample at 1.000 s is 2.000 s old (limit 1.000 s)"),
            ],
        ),
        (
            [1.5, None],
            S,
            None,
            [
                ("Current value", "--"),
                ("Current state", "missing: latest sample at 1.000 s has no value"),
            ],
        ),
        (
            [1.5, INF],
            S,
            None,
            [
                ("Current value", "--"),
                ("Current state", "invalid: latest sample at 1.000 s is not finite"),
            ],
        ),
        (
            [1.5, 2.5],
            -S,
            None,
            [("Current value", "--"), ("Current state", "no data at or before -1.000 s")],
        ),
    ],
)
def test_format_current_value_states_in_words(
    values: list[float | None], at: int, max_age: int | None, expected: list[tuple[str, str]]
) -> None:
    current = current_value([0, S], values, at, max_age_ns=max_age, unit="V")
    assert format_current_value(current) == expected
