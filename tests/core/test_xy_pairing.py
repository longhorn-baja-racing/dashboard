"""Alignment, exclusion, interval, and point-limit rules for XY pairing."""

from __future__ import annotations

import time
from array import array
from math import inf, nan

import pytest

from lbr_dashboard.core.xy_pairing import (
    AlignmentPolicy,
    ExclusionCounts,
    PairedSeries,
    limit_points,
    pair_signals,
)

NEAREST = AlignmentPolicy.NEAREST
PREVIOUS = AlignmentPolicy.PREVIOUS
MS = 1_000_000


def _pairs(series: PairedSeries) -> list[tuple[int, float, float]]:
    return list(zip(series.times_ns, series.x, series.y))


def test_policy_docstring_examples() -> None:
    y_times, y_values = [0, 10], [1.0, 2.0]
    nearest = pair_signals(
        [4, 5, 8], [0.4, 0.5, 0.8], y_times, y_values, policy=NEAREST, tolerance_ns=5
    )
    assert _pairs(nearest) == [(4, 0.4, 1.0), (5, 0.5, 1.0), (8, 0.8, 2.0)]
    previous = pair_signals(
        [4, 5, 8], [0.4, 0.5, 0.8], y_times, y_values, policy=PREVIOUS, tolerance_ns=5
    )
    assert _pairs(previous) == [(4, 0.4, 1.0), (5, 0.5, 1.0)]
    assert previous.excluded == ExclusionCounts(no_match=1)


def test_equal_rates_pair_one_to_one_and_keep_metadata() -> None:
    series = pair_signals(
        array("q", [0, MS, 2 * MS]),
        array("d", [10.0, 20.0, 30.0]),
        array("q", [0, MS, 2 * MS]),
        array("d", [1.5, 2.5, 3.5]),
        policy=NEAREST,
        tolerance_ns=0,
        x_unit="km/h",
        y_unit="deg C",
    )
    assert series.x == (10.0, 20.0, 30.0)
    assert series.y == (1.5, 2.5, 3.5)
    assert series.times_ns == (0, MS, 2 * MS)
    assert (series.policy, series.tolerance_ns, series.driver) == (NEAREST, 0, "x")
    assert (series.x_unit, series.y_unit) == ("km/h", "deg C")
    assert series.excluded.total == 0
    assert len(series) == 3


def test_fast_x_reuses_slow_y_samples() -> None:
    x_times = list(range(0, 100, 10))
    x_values = [float(t) for t in x_times]
    y_times, y_values = [0, 50], [100.0, 200.0]
    nearest = pair_signals(x_times, x_values, y_times, y_values, policy=NEAREST, tolerance_ns=30)
    assert nearest.y == (100.0, 100.0, 100.0, 200.0, 200.0, 200.0, 200.0, 200.0, 200.0)
    assert nearest.excluded == ExclusionCounts(no_match=1)  # t=90 is 40 from t=50
    previous = pair_signals(x_times, x_values, y_times, y_values, policy=PREVIOUS, tolerance_ns=30)
    assert previous.times_ns == (0, 10, 20, 30, 50, 60, 70, 80)
    assert previous.y == (100.0, 100.0, 100.0, 100.0, 200.0, 200.0, 200.0, 200.0)
    assert previous.excluded == ExclusionCounts(no_match=2)


def test_slow_x_picks_one_sample_from_fast_y() -> None:
    y_times = list(range(0, 101))
    y_values = [float(t) * 2 for t in y_times]
    series = pair_signals(
        [0, 33, 100], [1.0, 2.0, 3.0], y_times, y_values, policy=NEAREST, tolerance_ns=0
    )
    assert series.y == (0.0, 66.0, 200.0)
    offset = pair_signals([7], [1.0], [0, 10], [5.0, 6.0], policy=NEAREST, tolerance_ns=5)
    assert offset.y == (6.0,)


@pytest.mark.parametrize(
    ("policy", "x_time", "expected"),
    [
        (NEAREST, 15, (1.0,)),
        (NEAREST, 16, ()),
        (NEAREST, 5, (1.0,)),
        (NEAREST, 4, ()),
        (PREVIOUS, 15, (1.0,)),
        (PREVIOUS, 16, ()),
        (PREVIOUS, 5, ()),
    ],
)
def test_tolerance_edge_is_inclusive(
    policy: AlignmentPolicy, x_time: int, expected: tuple[float, ...]
) -> None:
    series = pair_signals([x_time], [9.0], [10], [1.0], policy=policy, tolerance_ns=5)
    assert series.y == expected
    assert series.excluded.no_match == 1 - len(expected)


def test_nearest_tie_picks_earlier_sample() -> None:
    series = pair_signals([5], [0.0], [0, 10], [1.0, 2.0], policy=NEAREST, tolerance_ns=5)
    assert series.y == (1.0,)


def test_previous_never_looks_ahead() -> None:
    series = pair_signals([9], [0.0], [10], [1.0], policy=PREVIOUS, tolerance_ns=100)
    assert series.y == ()
    assert series.excluded == ExclusionCounts(no_match=1)


def test_gap_in_y_excludes_driver_samples_inside_it() -> None:
    x_times = list(range(0, 100, 10))
    y_times = [0, 10, 20, 80, 90]
    y_values = [0.0, 1.0, 2.0, 8.0, 9.0]
    series = pair_signals(x_times, [1.0] * 10, y_times, y_values, policy=NEAREST, tolerance_ns=10)
    assert series.times_ns == (0, 10, 20, 30, 70, 80, 90)
    assert series.y == (0.0, 1.0, 2.0, 2.0, 8.0, 8.0, 9.0)
    assert series.excluded == ExclusionCounts(no_match=3)


def test_missing_values_are_counted_and_never_replaced() -> None:
    x_times = [0, 10, 20, 30, 40, 50]
    x_values: list[float | None] = [1.0, None, 3.0, nan, 5.0, inf]
    y_values: list[float | None] = [10.0, 20.0, None, 40.0, nan, 60.0]
    series = pair_signals(x_times, x_values, x_times, y_values, policy=NEAREST, tolerance_ns=9)
    assert _pairs(series) == [(0, 1.0, 10.0)]
    assert series.excluded == ExclusionCounts(x_missing=3, y_missing=2)
    assert series.excluded.total == 5


def test_missing_nearest_y_does_not_fall_back_to_another_sample() -> None:
    # The nearest Y (t=9) is missing; t=0 is within tolerance but must not be used.
    series = pair_signals([10], [1.0], [0, 9], [5.0, None], policy=NEAREST, tolerance_ns=20)
    assert series.y == ()
    assert series.excluded == ExclusionCounts(y_missing=1)
    held = pair_signals([10], [1.0], [0, 9], [5.0, None], policy=PREVIOUS, tolerance_ns=20)
    assert held.excluded == ExclusionCounts(y_missing=1)


def test_missing_x_takes_precedence_over_other_reasons() -> None:
    series = pair_signals([0], [None], [], [], policy=NEAREST, tolerance_ns=0)
    assert series.excluded == ExclusionCounts(x_missing=1)


def test_duplicate_timestamps_use_last_sample_at_that_time() -> None:
    y_times, y_values = [0, 10, 10, 20], [1.0, 2.0, 3.0, 4.0]
    nearest = pair_signals([9, 11], [0.0, 0.0], y_times, y_values, policy=NEAREST, tolerance_ns=5)
    assert nearest.y == (3.0, 3.0)
    previous = pair_signals([10], [0.0], y_times, y_values, policy=PREVIOUS, tolerance_ns=0)
    assert previous.y == (3.0,)
    duplicate_x = pair_signals(
        [10, 10], [7.0, 8.0], y_times, y_values, policy=NEAREST, tolerance_ns=0
    )
    assert _pairs(duplicate_x) == [(10, 7.0, 3.0), (10, 8.0, 3.0)]


def test_interval_is_closed_on_driver_times() -> None:
    times = [0, 10, 20, 30, 40]
    values = [0.0, 1.0, 2.0, 3.0, 4.0]
    series = pair_signals(
        times, values, times, values, policy=NEAREST, tolerance_ns=0, start_ns=10, end_ns=30
    )
    assert series.times_ns == (10, 20, 30)
    open_start = pair_signals(
        times, values, times, values, policy=NEAREST, tolerance_ns=0, end_ns=10
    )
    assert open_start.times_ns == (0, 10)
    open_end = pair_signals(
        times, values, times, values, policy=NEAREST, tolerance_ns=0, start_ns=40
    )
    assert open_end.times_ns == (40,)
    point = pair_signals(
        times, values, times, values, policy=NEAREST, tolerance_ns=0, start_ns=20, end_ns=20
    )
    assert point.times_ns == (20,)


def test_interval_bounds_driver_but_not_matched_signal() -> None:
    series = pair_signals(
        [10, 20], [1.0, 2.0], [5], [50.0], policy=PREVIOUS, tolerance_ns=20, start_ns=10
    )
    assert series.y == (50.0, 50.0)


def test_exclusions_only_count_driver_samples_inside_interval() -> None:
    series = pair_signals(
        [0, 10, 20],
        [None, 1.0, None],
        [10],
        [5.0],
        policy=NEAREST,
        tolerance_ns=0,
        start_ns=5,
        end_ns=15,
    )
    assert series.y == (5.0,)
    assert series.excluded.total == 0


def test_empty_inputs_and_empty_interval() -> None:
    empty = pair_signals([], [], [], [], policy=NEAREST, tolerance_ns=10)
    assert len(empty) == 0
    assert empty.excluded.total == 0
    no_y = pair_signals([0, 1], [1.0, 2.0], [], [], policy=PREVIOUS, tolerance_ns=10)
    assert no_y.excluded == ExclusionCounts(no_match=2)
    outside = pair_signals(
        [0, 1], [1.0, 2.0], [0], [1.0], policy=NEAREST, tolerance_ns=1, start_ns=5
    )
    assert len(outside) == 0
    assert outside.excluded.total == 0


def test_y_driver_keeps_axes_and_swaps_roles() -> None:
    x_times, x_values = [0, 10, 20], [1.0, None, 3.0]
    y_times, y_values = [10, 20], [100.0, 200.0]
    series = pair_signals(
        x_times,
        x_values,
        y_times,
        y_values,
        policy=PREVIOUS,
        tolerance_ns=0,
        driver="y",
        x_unit="rpm",
        y_unit="N",
    )
    assert series.driver == "y"
    assert _pairs(series) == [(20, 3.0, 200.0)]
    assert series.excluded == ExclusionCounts(x_missing=1)
    assert (series.x_unit, series.y_unit) == ("rpm", "N")
    y_only = pair_signals([], [], [0], [None], policy=NEAREST, tolerance_ns=0, driver="y")
    assert y_only.excluded == ExclusionCounts(y_missing=1)


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (([0, 1], [1.0], [], []), "X has 2 timestamps but 1 values"),
        (([], [], [0], []), "Y has 1 timestamps but 0 values"),
        (([0, 5, 3], [1.0, 2.0, 3.0], [], []), "X timestamps must be non-decreasing"),
        (([], [], [2, 1], [1.0, 2.0]), "Y timestamps must be non-decreasing"),
    ],
)
def test_invalid_signals_are_rejected(arguments: tuple, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        pair_signals(*arguments, policy=NEAREST, tolerance_ns=0)


def test_invalid_options_are_rejected() -> None:
    with pytest.raises(ValueError, match="tolerance_ns must be >= 0"):
        pair_signals([], [], [], [], policy=NEAREST, tolerance_ns=-1)
    with pytest.raises(ValueError, match="is after end_ns"):
        pair_signals([], [], [], [], policy=NEAREST, tolerance_ns=0, start_ns=2, end_ns=1)
    with pytest.raises(ValueError, match="driver must be"):
        pair_signals([], [], [], [], policy=NEAREST, tolerance_ns=0, driver="z")  # type: ignore[arg-type]


def _series(count: int) -> PairedSeries:
    times = list(range(count))
    values = [float(t) for t in times]
    return pair_signals(times, values, times, values, policy=NEAREST, tolerance_ns=0, x_unit="V")


def test_limit_points_keeps_small_series_unchanged() -> None:
    series = _series(5)
    assert limit_points(series, 5) == (series, 0)
    assert limit_points(series, 100)[0] is series


def test_limit_points_is_bounded_deterministic_and_keeps_ends() -> None:
    series = _series(1001)
    limited, dropped = limit_points(series, 11)
    assert limited.times_ns == tuple(range(0, 1001, 100))
    assert dropped == 990
    assert len(limited) == 11
    assert limited.x == limited.y == tuple(float(t) for t in range(0, 1001, 100))
    assert (limited.x_unit, limited.policy, limited.excluded) == ("V", NEAREST, series.excluded)
    assert limit_points(series, 11) == (limited, dropped)


@pytest.mark.parametrize("count", [3, 7, 100, 12345])
@pytest.mark.parametrize("max_points", [2, 3, 50])
def test_limit_points_bound_holds_for_any_size(count: int, max_points: int) -> None:
    series = _series(count)
    limited, dropped = limit_points(series, max_points)
    assert len(limited) == min(count, max_points)
    assert len(limited) + dropped == count
    assert limited.times_ns[0] == 0 and limited.times_ns[-1] == count - 1
    assert list(limited.times_ns) == sorted(set(limited.times_ns))


def test_limit_points_rejects_too_small_bound() -> None:
    with pytest.raises(ValueError, match="max_points must be >= 2"):
        limit_points(_series(3), 1)


@pytest.mark.parametrize("policy", [NEAREST, PREVIOUS])
def test_large_interval_pairs_and_limits_quickly(policy: AlignmentPolicy) -> None:
    count = 500_000  # about 8 minutes of a 1 kHz signal
    x_times = array("q", range(0, count * MS, MS))
    x_values = array("d", (float(index % 97) for index in range(count)))
    y_times = array("q", range(0, count * MS, 10 * MS + 3))  # just under 100 Hz
    y_values = array("d", (float(index) for index in range(len(y_times))))
    started = time.perf_counter()
    series = pair_signals(x_times, x_values, y_times, y_values, policy=policy, tolerance_ns=10 * MS)
    limited, dropped = limit_points(series, 20_000)
    elapsed = time.perf_counter() - started
    assert len(series) + series.excluded.total == count
    assert len(series) == count  # every X has a Y within 10 ms
    assert len(limited) == 20_000
    assert dropped == len(series) - 20_000
    assert elapsed < 10
