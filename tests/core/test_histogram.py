"""Sample-count histograms: bin edges, boundaries, missing data and intervals."""

from array import array
from dataclasses import FrozenInstanceError
from math import inf, nan

import numpy
import pytest

from lbr_dashboard.core.histogram import EDGE_RULE, Histogram, HistogramRow, histogram


def _times(count: int, step_ns: int = 10_000_000) -> list[int]:
    return [index * step_ns for index in range(count)]


def test_known_bins_match_hand_counts() -> None:
    values = [0.0, 1.0, 2.0, 2.5, 3.0, 4.0, 9.0, 10.0]
    hist = histogram(_times(len(values)), values, bins=5, unit="kPa")
    assert hist.edges == (0.0, 2.0, 4.0, 6.0, 8.0, 10.0)
    # [0,2): 0, 1   [2,4): 2, 2.5, 3   [4,6): 4   [6,8): -   [8,10]: 9, 10
    assert hist.counts == (2, 3, 1, 0, 2)
    assert hist.valid == hist.total == 8
    assert (hist.below_range, hist.above_range, hist.missing, hist.non_finite) == (0, 0, 0, 0)
    assert hist.unit == "kPa"
    assert hist.edge_rule == EDGE_RULE
    assert (hist.start_ns, hist.end_ns) == (None, None)


def test_default_is_ten_bins_over_the_data_range() -> None:
    hist = histogram(_times(11), [float(value) for value in range(11)])
    assert len(hist.counts) == 10
    assert hist.edges[0] == 0.0
    assert hist.edges[-1] == 10.0
    assert hist.counts == (1, 1, 1, 1, 1, 1, 1, 1, 1, 2)


def test_values_on_inner_edges_go_right_and_last_edge_is_closed() -> None:
    edges = [0.0, 1.0, 2.0, 3.0]
    hist = histogram(_times(4), [0.0, 1.0, 2.0, 3.0], edges=edges)
    assert hist.counts == (1, 1, 2)
    rows = hist.to_rows()
    assert rows == (
        HistogramRow(0.0, 1.0, 1, "left"),
        HistogramRow(1.0, 2.0, 1, "left"),
        HistogramRow(2.0, 3.0, 2, "both"),
    )


def test_explicit_uneven_edges_and_out_of_range_counts() -> None:
    values = [-5.0, 0.0, 0.5, 1.0, 9.99, 10.0, 10.01, 50.0]
    hist = histogram(_times(len(values)), values, edges=(0, 1, 10))
    assert hist.edges == (0.0, 1.0, 10.0)
    assert hist.counts == (2, 3)
    assert hist.below_range == 1
    assert hist.above_range == 2
    assert hist.total == len(values)


@pytest.mark.parametrize(
    "edges",
    [[], [1.0], [0.0, 0.0], [2.0, 1.0], [0.0, 1.0, 1.0], [0.0, nan], [-inf, 1.0], [0.0, inf]],
)
def test_bad_edges_are_rejected(edges: list[float]) -> None:
    with pytest.raises(ValueError):
        histogram(_times(1), [1.0], edges=edges)


@pytest.mark.parametrize("bins", [0, -1, True, 2.0])
def test_bad_bin_counts_are_rejected(bins: object) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        histogram(_times(1), [1.0], bins=bins)  # type: ignore[arg-type]


def test_edges_cannot_be_combined_with_bins_or_range() -> None:
    with pytest.raises(ValueError, match="either edges"):
        histogram(_times(1), [1.0], edges=[0, 1], bins=2)
    with pytest.raises(ValueError, match="either edges"):
        histogram(_times(1), [1.0], edges=[0, 1], value_range=(0, 1))


@pytest.mark.parametrize("value_range", [(2.0, 1.0), (nan, 1.0), (0.0, inf), (-1e308, 1e308)])
def test_bad_value_ranges_are_rejected(value_range: tuple[float, float]) -> None:
    with pytest.raises(ValueError):
        histogram(_times(1), [1.0], value_range=value_range)


def test_too_many_bins_for_a_tiny_range_is_rejected() -> None:
    with pytest.raises(ValueError, match="too narrow"):
        histogram(_times(1), [1.0], bins=10, value_range=(1.0, 1.0 + 2.0**-52))


def test_mismatched_lengths_and_reversed_interval_are_rejected() -> None:
    with pytest.raises(ValueError, match="same length"):
        histogram([0, 1], [1.0])
    with pytest.raises(ValueError, match="start_ns"):
        histogram([0, 1], [1.0, 2.0], start_ns=5, end_ns=4)


def test_value_range_counts_values_outside_separately() -> None:
    values = [-1.0, 0.0, 4.9, 5.0, 10.0, 10.5, 20.0]
    hist = histogram(_times(len(values)), values, bins=2, value_range=(0.0, 10.0))
    assert hist.edges == (0.0, 5.0, 10.0)
    assert hist.counts == (2, 2)
    assert hist.below_range == 1
    assert hist.above_range == 2


def test_all_equal_values_use_a_unit_wide_range() -> None:
    hist = histogram(_times(3), [7.0, 7.0, 7.0], bins=2)
    assert hist.edges == (6.5, 7.0, 7.5)
    assert hist.counts == (0, 3)


def test_equal_explicit_range_is_widened_the_same_way() -> None:
    hist = histogram(_times(2), [4.0, 4.0], bins=1, value_range=(4.0, 4.0))
    assert hist.edges == (3.5, 4.5)
    assert hist.counts == (2,)


def test_empty_input_has_no_bins_and_no_fake_zeros() -> None:
    hist = histogram([], [])
    assert hist.edges == ()
    assert hist.counts == ()
    assert hist.to_rows() == ()
    assert hist.is_empty
    assert hist.total == 0


def test_all_missing_interval_counts_only_missing() -> None:
    hist = histogram(_times(4), [None, nan, None, nan], unit="deg C")
    assert hist.edges == ()
    assert hist.counts == ()
    assert hist.missing == 4
    assert hist.total == 4
    assert hist.is_empty


def test_all_missing_with_explicit_bins_keeps_bins_but_counts_nothing() -> None:
    hist = histogram(_times(2), [None, nan], edges=[0.0, 1.0])
    assert hist.edges == (0.0, 1.0)
    assert hist.counts == (0,)
    assert hist.missing == 2
    assert hist.is_empty


def test_missing_and_non_finite_never_enter_bins() -> None:
    values = [None, 1.0, nan, inf, 2.0, -inf, 3.0]
    hist = histogram(_times(len(values)), values, bins=2)
    assert hist.edges == (1.0, 2.0, 3.0)
    assert hist.counts == (1, 2)
    assert hist.missing == 2
    assert hist.non_finite == 2
    assert hist.valid == 3
    # A missing value treated as 0.0 would have widened the range down to 0.
    assert hist.edges[0] == 1.0


def test_infinities_are_non_finite_even_with_a_value_range() -> None:
    hist = histogram(_times(3), [inf, -inf, 0.5], bins=1, value_range=(0.0, 1.0))
    assert hist.counts == (1,)
    assert (hist.below_range, hist.above_range, hist.non_finite) == (0, 0, 2)


def test_closed_time_interval_includes_both_end_samples() -> None:
    times = [0, 10, 20, 20, 30, 40]
    values = [100.0, 1.0, 2.0, 3.0, 4.0, 100.0]
    hist = histogram(times, values, start_ns=10, end_ns=30, bins=1)
    assert hist.total == 4
    assert hist.edges == (1.0, 4.0)
    assert (hist.start_ns, hist.end_ns) == (10, 30)


def test_open_ended_and_empty_time_intervals() -> None:
    times = [0, 10, 20, 30]
    values = [1.0, 2.0, 3.0, 4.0]
    assert histogram(times, values, start_ns=20).total == 2
    assert histogram(times, values, end_ns=10).total == 2
    between = histogram(times, values, start_ns=11, end_ns=19)
    assert between.total == 0
    assert between.edges == ()
    assert histogram(times, values, start_ns=40, end_ns=50).total == 0
    assert histogram(times, values, start_ns=30, end_ns=30).total == 1


def test_each_sample_counts_once_whatever_the_sample_spacing() -> None:
    values = [1.0, 1.0, 2.0, 3.0]
    even = histogram([0, 10, 20, 30], values, bins=2)
    uneven = histogram([0, 1, 2, 1_000_000_000], values, bins=2)
    assert even.counts == uneven.counts == (2, 2)


def test_signals_at_different_rates_are_counted_independently() -> None:
    # rpm at 100 Hz and a temperature at 10 Hz over the same 1 s interval.
    rpm_times = _times(101, 10_000_000)
    rpm = [2000.0 + (index % 4) * 500.0 for index in range(101)]
    temp_times = _times(11, 100_000_000)
    temp = [80.0, 80.0, None, 81.0, 82.0, 82.0, nan, 83.0, 84.0, 85.0, 90.0]
    rpm_hist = histogram(rpm_times, rpm, bins=4, unit="rpm", end_ns=1_000_000_000)
    temp_hist = histogram(temp_times, temp, edges=[80, 85, 90], unit="deg C")
    assert rpm_hist.edges == (2000.0, 2375.0, 2750.0, 3125.0, 3500.0)
    assert rpm_hist.counts == (26, 25, 25, 25)
    assert rpm_hist.unit == "rpm"
    assert temp_hist.counts == (7, 2)
    assert temp_hist.missing == 2
    assert temp_hist.unit == "deg C"


def test_accepts_importer_arrays_and_integer_values() -> None:
    hist = histogram(array("q", [0, 1, 2]), array("d", [0.0, 1.0, 2.0]), bins=2)
    assert hist.counts == (1, 2)
    assert histogram([0, 1], [1, 2], bins=1).counts == (2,)


def test_results_are_immutable() -> None:
    hist = histogram(_times(1), [1.0])
    assert isinstance(hist, Histogram)
    with pytest.raises(FrozenInstanceError):
        hist.counts = ()  # type: ignore[misc]


NUMPY_CASES = [
    pytest.param([0.1, 0.2, 0.3, 0.7, 1.0], 3, None, id="thirds"),
    pytest.param([-3.3, -1.0, 0.0, 2.2, 7.9, 7.9], 7, None, id="odd-width"),
    pytest.param([1.0, 2.0, 3.0, 4.0, 5.0], 4, (1.5, 4.5), id="clipped"),
    pytest.param([0.3] * 4, 5, None, id="all-equal"),
    pytest.param([i * 0.1 for i in range(101)], 10, None, id="tenths"),
]


@pytest.mark.parametrize(("values", "bins", "value_range"), NUMPY_CASES)
def test_matches_numpy_histogram(
    values: list[float], bins: int, value_range: tuple[float, float] | None
) -> None:
    hist = histogram(_times(len(values)), values, bins=bins, value_range=value_range)
    counts, edges = numpy.histogram(values, bins=bins, range=value_range)
    assert hist.edges == tuple(float(edge) for edge in edges)
    assert hist.counts == tuple(int(count) for count in counts)


def test_matches_numpy_with_explicit_edges() -> None:
    values = [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5]
    edges = [0.0, 1.0, 2.5, 3.0]
    counts, _ = numpy.histogram(values, bins=edges)
    hist = histogram(_times(len(values)), values, edges=edges)
    assert hist.counts == tuple(int(count) for count in counts)
    assert hist.above_range == 1


def test_large_input_finishes_quickly() -> None:
    count = 1_000_000
    times = array("q", range(count))
    values = array("d", ((index * 7919) % 1000 / 10.0 for index in range(count)))
    hist = histogram(times, values, bins=50)
    assert hist.valid == count
    assert sum(hist.counts) == count
