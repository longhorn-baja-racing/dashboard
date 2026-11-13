"""Data-quality summaries must be deterministic, labelled and honest about loss."""

import math
from array import array

import pytest

from lbr_dashboard.core.data_quality import (
    MAX_EXAMPLES,
    Completeness,
    Evidence,
    Gap,
    PeriodSource,
    Regression,
    SequenceEvent,
    SequenceEventKind,
    SequenceSummary,
    SignalQuality,
    Timing,
    ValueCounts,
    format_quality,
    summarize_session,
    summarize_signal,
)

MS = 1_000_000


def _clean(count: int = 100, period: int = 10 * MS) -> tuple[list[int], list[float | None]]:
    return [index * period for index in range(count)], [float(index) for index in range(count)]


def test_clean_fixture_has_no_findings_and_unknown_completeness() -> None:
    times, values = _clean()
    quality = summarize_signal(times, values, name="rpm")
    assert quality.values == ValueCounts(samples=100, valid=100, missing=0, non_finite=0)
    timing = quality.timing
    assert (timing.first_ns, timing.last_ns) == (0, 990 * MS)
    assert timing.duplicate_timestamps == 0
    assert timing.regressions == 0
    assert timing.regression_examples == ()
    assert timing.min_delta_ns == timing.median_delta_ns == timing.max_delta_ns == 10 * MS
    assert timing.rate_hz == pytest.approx(100.0)
    assert timing.reference_period_ns == 10 * MS
    assert timing.reference_source is PeriodSource.MEDIAN
    assert timing.gaps == ()
    assert quality.sequence is None
    assert quality.completeness is Completeness.UNKNOWN


def test_same_input_gives_identical_output() -> None:
    times = [0, 10, 20, 15, 60, 70, 70, 80]
    values: list[float | None] = [1.0, None, 2.0, math.nan, 3.0, math.inf, 4.0, 5.0]
    counter = [0, 1, 3, 3, 4, 0, 1, 2]
    first = summarize_signal(times, values, sequence=counter, sequence_contiguous=True)
    second = summarize_signal(
        array("q", times),
        array("d", [math.nan if item is None else item for item in values]),
        sequence=tuple(counter),
        sequence_contiguous=True,
    )
    assert first == second
    assert format_quality(first) == format_quality(second)


def test_gaps_against_expected_period() -> None:
    times = [0, 10, 20, 50, 60, 65, 70, 130]
    quality = summarize_signal(times, [1.0] * len(times), expected_period_ns=10)
    timing = quality.timing
    assert timing.reference_period_ns == 10
    assert timing.reference_source is PeriodSource.EXPECTED
    assert timing.gaps == (Gap(20, 50, 2), Gap(70, 130, 5))
    assert [gap.duration_ns for gap in timing.gaps] == [30, 60]
    assert all(gap.evidence is Evidence.INFERRED for gap in timing.gaps)
    # The rate estimate stays median-based even when a reference is given.
    assert timing.median_delta_ns == 10


def test_gaps_against_median_period() -> None:
    times = [0, 10, 20, 30, 40, 55, 65, 100]
    timing = summarize_signal(times, [1.0] * len(times)).timing
    assert timing.reference_source is PeriodSource.MEDIAN
    assert timing.reference_period_ns == 10
    # 15 is exactly 1.5 periods, which is not longer than the threshold.
    assert timing.gaps == (Gap(65, 100, 3),)


def test_gap_factor_moves_the_threshold() -> None:
    times = [0, 10, 20, 35, 45]
    assert summarize_signal(times, [1.0] * 5, gap_factor=1.2).timing.gaps == (Gap(20, 35, 1),)
    assert summarize_signal(times, [1.0] * 5, gap_factor=4.0).timing.gaps == ()
    # A gap shorter than 1.5 periods rounds to zero missing periods.
    assert summarize_signal(times, [1.0] * 5, gap_factor=1.1, expected_period_ns=12).timing.gaps[
        0
    ] == Gap(20, 35, 0)


def test_live_decimation_is_not_loss_by_itself() -> None:
    sd_times, sd_values = _clean(count=100, period=10 * MS)
    live_times, live_values = sd_times[::10], sd_values[::10]
    live = summarize_signal(live_times, live_values)
    assert live.timing.gaps == ()
    assert live.timing.rate_hz == pytest.approx(10.0)
    # Judging the decimated stream against the SD logging rate flags every step,
    # but completeness stays unknown: timestamps alone never confirm loss.
    judged = summarize_signal(live_times, live_values, expected_period_ns=10 * MS)
    assert len(judged.timing.gaps) == 9
    assert {gap.missing_periods for gap in judged.timing.gaps} == {9}
    assert judged.completeness is Completeness.UNKNOWN


def test_duplicate_timestamps() -> None:
    timing = summarize_signal([0, 10, 10, 20, 10, 30], [1.0] * 6).timing
    assert timing.duplicate_timestamps == 2
    assert timing.min_delta_ns == 10
    assert timing.gaps == ()
    all_same = summarize_signal([5, 5, 5], [1.0] * 3).timing
    assert all_same.duplicate_timestamps == 2
    assert all_same.median_delta_ns is None
    assert all_same.rate_hz is None
    assert all_same.reference_source is None


def test_regressions_in_unsorted_data() -> None:
    times = [0, 10, 30, 20, 40, 50, 70, 60]
    timing = summarize_signal(times, [1.0] * len(times)).timing
    assert timing.regressions == 2
    assert timing.regression_examples == (Regression(3, 30, 20), Regression(7, 70, 60))
    assert all(item.evidence is Evidence.INFERRED for item in timing.regression_examples)
    assert (timing.first_ns, timing.last_ns) == (0, 70)
    # Out-of-order samples still cover their moment in time: no gaps.
    assert timing.gaps == ()


def test_regression_examples_are_capped_but_counts_are_exact() -> None:
    times = [value for pair in range(30) for value in (pair * 10 + 5, pair * 10)]
    timing = summarize_signal(times, [1.0] * len(times)).timing
    assert timing.regressions == 30
    assert len(timing.regression_examples) == MAX_EXAMPLES


def test_mixed_rates_give_independent_summaries() -> None:
    fast = ([index * MS for index in range(1000)], [1.0] * 1000)
    slow = ([index * 100 * MS for index in range(10)], [2.0] * 10)
    by_name = {item.name: item for item in summarize_session({"wheel": fast, "temp": slow})}
    assert by_name["wheel"].timing.rate_hz == pytest.approx(1000.0)
    assert by_name["temp"].timing.rate_hz == pytest.approx(10.0)
    assert by_name["wheel"].timing.gaps == by_name["temp"].timing.gaps == ()
    assert by_name["temp"].timing.reference_period_ns == 100 * MS


def test_session_is_sorted_and_passes_options() -> None:
    signals = {
        "b": ([0, 10, 20, 50], [1.0, 2.0, None, 4.0]),
        "a": ([0, 5, 10, 15, 20], [1.0] * 5),
    }
    result = summarize_session(signals, start_ns=5, end_ns=50, expected_periods_ns={"b": 10})
    assert [item.name for item in result] == ["a", "b"]
    assert result[0].values.samples == 4
    assert result[0].timing.reference_source is PeriodSource.MEDIAN
    assert result[1].values.samples == 3
    assert result[1].values.missing == 1
    assert result[1].timing.reference_source is PeriodSource.EXPECTED
    assert result[1].timing.gaps == (Gap(20, 50, 2),)
    assert all(item.completeness is Completeness.UNKNOWN for item in result)
    assert summarize_session({}) == ()
    with pytest.raises(ValueError, match="unknown signals: c"):
        summarize_session(signals, expected_periods_ns={"c": 10})


def test_missing_and_non_finite_values() -> None:
    values: list[float | None] = [1.0, None, math.nan, math.inf, -math.inf, 0, 2.5]
    counts = summarize_signal(list(range(7)), values).values
    assert counts == ValueCounts(samples=7, valid=3, missing=2, non_finite=2)
    assert counts.evidence is Evidence.RECORDED


def test_counter_with_drops_is_confirmed_only_when_contiguous() -> None:
    times = list(range(8))
    counter = [10, 11, 12, 15, 16, 20, 21, 22]
    contiguous = summarize_signal(
        times, [1.0] * 8, sequence=counter, sequence_contiguous=True
    ).sequence
    assert contiguous is not None
    assert contiguous.missing_values == 5
    assert contiguous.dropped_records == 5
    assert contiguous.events == (
        SequenceEvent(SequenceEventKind.MISSING, 3, 12, 15, 2),
        SequenceEvent(SequenceEventKind.MISSING, 5, 16, 20, 3),
    )
    assert contiguous.completeness is Completeness.INCOMPLETE
    assert contiguous.evidence is Evidence.RECORDED
    loose = summarize_signal(times, [1.0] * 8, sequence=counter)
    assert loose.sequence is not None
    assert loose.sequence.missing_values == 5
    assert loose.sequence.dropped_records is None
    assert loose.completeness is Completeness.UNKNOWN


def test_contiguous_counter_without_skips_is_complete() -> None:
    times, values = _clean(count=5)
    quality = summarize_signal(times, values, sequence=[3, 4, 5, 6, 7], sequence_contiguous=True)
    assert quality.sequence == SequenceSummary(
        contiguous=True,
        modulus=None,
        runs=1,
        missing_values=0,
        repeats=0,
        rollovers=0,
        resets=0,
        events=(),
    )
    assert quality.completeness is Completeness.COMPLETE


def test_counter_repeats() -> None:
    sequence = summarize_signal(
        list(range(5)), [1.0] * 5, sequence=[0, 1, 1, 1, 2], sequence_contiguous=True
    ).sequence
    assert sequence is not None
    assert sequence.repeats == 2
    assert sequence.missing_values == 0
    assert sequence.completeness is Completeness.COMPLETE
    assert [event.kind for event in sequence.events] == [SequenceEventKind.REPEAT] * 2
    many = summarize_signal(list(range(30)), [1.0] * 30, sequence=[7] * 30).sequence
    assert many is not None
    assert many.repeats == 29
    assert len(many.events) == MAX_EXAMPLES


def test_rollover_at_modulus_is_not_a_reset() -> None:
    counter = [253, 254, 255, 0, 1, 3, 4]
    sequence = summarize_signal(
        list(range(7)),
        [1.0] * 7,
        sequence=counter,
        sequence_modulus=256,
        sequence_contiguous=True,
    ).sequence
    assert sequence is not None
    assert (sequence.rollovers, sequence.resets, sequence.runs) == (1, 0, 1)
    assert sequence.missing_values == 1
    skipping = summarize_signal(
        [0, 1, 2], [1.0] * 3, sequence=[254, 255, 2], sequence_modulus=256
    ).sequence
    assert skipping is not None
    assert skipping.events == (SequenceEvent(SequenceEventKind.ROLLOVER, 2, 255, 2, 2),)
    assert skipping.missing_values == 2


def test_real_reset_starts_a_new_run() -> None:
    times = list(range(6))
    counter = [100, 101, 102, 0, 1, 2]
    with_modulus = summarize_signal(
        times, [1.0] * 6, sequence=counter, sequence_modulus=256, sequence_contiguous=True
    )
    sequence = with_modulus.sequence
    assert sequence is not None
    assert (sequence.rollovers, sequence.resets, sequence.runs) == (0, 1, 2)
    assert sequence.events == (SequenceEvent(SequenceEventKind.RESET, 3, 102, 0, 0),)
    assert sequence.missing_values == 0
    assert with_modulus.completeness is Completeness.UNKNOWN
    # Without a modulus every decrease is a reset, including a wrap.
    no_modulus = summarize_signal(
        [0, 1], [1.0] * 2, sequence=[255, 0], sequence_contiguous=True
    ).sequence
    assert no_modulus is not None
    assert (no_modulus.rollovers, no_modulus.resets) == (0, 1)


def test_reset_with_drops_is_still_incomplete() -> None:
    sequence = summarize_signal(
        list(range(4)), [1.0] * 4, sequence=[5, 7, 0, 1], sequence_contiguous=True
    ).sequence
    assert sequence is not None
    assert sequence.completeness is Completeness.INCOMPLETE


def test_missing_counter_means_unknown_completeness() -> None:
    quality = summarize_signal([0, 10, 20], [1.0, 2.0, 3.0], expected_period_ns=10)
    assert quality.sequence is None
    assert quality.completeness is Completeness.UNKNOWN
    assert ("Completeness", "unknown (no sequence counter)") in format_quality(quality)


def test_interval_edges_are_closed() -> None:
    times = [0, 10, 20, 30, 40]
    counter = [0, 1, 2, 3, 4]
    quality = summarize_signal(
        times, [1.0] * 5, start_ns=10, end_ns=30, sequence=counter, sequence_contiguous=True
    )
    assert quality.values.samples == 3
    assert (quality.timing.first_ns, quality.timing.last_ns) == (10, 30)
    assert quality.sequence is not None
    assert quality.sequence.runs == 1
    assert quality.completeness is Completeness.COMPLETE
    assert summarize_signal(times, [1.0] * 5, start_ns=20, end_ns=20).values.samples == 1
    assert summarize_signal(times, [1.0] * 5, start_ns=11, end_ns=19).values.samples == 0
    assert summarize_signal(times, [1.0] * 5, start_ns=35).values.samples == 1
    assert summarize_signal(times, [1.0] * 5, end_ns=5).values.samples == 1


def test_interval_selection_keeps_recorded_order() -> None:
    times = [30, 0, 20, 10, 50]
    timing = summarize_signal(times, [1.0] * 5, start_ns=0, end_ns=30).timing
    assert timing.regressions == 2
    assert timing.regression_examples == (Regression(1, 30, 0), Regression(3, 20, 10))


def test_empty_input() -> None:
    quality = summarize_signal([], [], sequence=[], sequence_contiguous=True)
    assert quality.values == ValueCounts(samples=0, valid=0, missing=0, non_finite=0)
    assert quality.timing == Timing(
        first_ns=None,
        last_ns=None,
        duplicate_timestamps=0,
        regressions=0,
        regression_examples=(),
        min_delta_ns=None,
        median_delta_ns=None,
        max_delta_ns=None,
        rate_hz=None,
        reference_period_ns=None,
        reference_source=None,
        gap_factor=1.5,
        gaps=(),
    )
    assert quality.sequence is not None
    assert quality.sequence.runs == 0
    assert quality.completeness is Completeness.UNKNOWN
    single = summarize_signal([7], [1.0], expected_period_ns=10).timing
    assert single.reference_source is PeriodSource.EXPECTED
    assert single.gaps == ()


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        pytest.param({"values": [1.0]}, "same length", id="values-length"),
        pytest.param({"sequence": [1]}, "one value per sample", id="sequence-length"),
        pytest.param({"sequence_modulus": 8}, "require a sequence", id="modulus-alone"),
        pytest.param({"sequence_contiguous": True}, "require a sequence", id="contiguous-alone"),
        pytest.param({"start_ns": 5, "end_ns": 4}, "start must not", id="interval"),
        pytest.param({"expected_period_ns": 0}, "positive", id="period"),
        pytest.param({"gap_factor": 1.0}, "greater than 1", id="factor"),
        pytest.param({"gap_factor": math.nan}, "greater than 1", id="factor-nan"),
        pytest.param({"sequence": [0, 1], "sequence_modulus": 1}, "at least 2", id="modulus"),
        pytest.param({"sequence": [0, 8], "sequence_modulus": 8}, r"\[0, modulus\)", id="range"),
    ],
)
def test_invalid_arguments(kwargs: dict[str, object], message: str) -> None:
    arguments: dict[str, object] = {"times_ns": [0, 1], "values": [1.0, 2.0], **kwargs}
    with pytest.raises(ValueError, match=message):
        summarize_signal(**arguments)  # type: ignore[arg-type]


def test_format_rows_are_ascii_and_labelled() -> None:
    times = [0, 10, 20, 15, 60, 70]
    quality = summarize_signal(
        times,
        [1.0, None, 2.0, 3.0, math.inf, 4.0],
        name="brake",
        start_ns=0,
        end_ns=100,
        expected_period_ns=10,
        sequence=[0, 1, 1, 5, 0, 1],
        sequence_modulus=8,
    )
    rows = format_quality(quality)
    assert all(label.isascii() and value.isascii() for label, value in rows)
    table = dict(rows)
    assert table["Signal"] == "brake"
    assert table["Interval"] == "[0.000000 s, 0.000000 s] (closed)"
    assert table["Missing values [recorded]"] == "1"
    assert table["Timestamp regressions [inferred]"] == "1"
    assert table["  regression at sample 3"] == "0.000000 s -> 0.000000 s"
    assert table["Gap reference [inferred]"] == (
        "0.000 ms expected period, gap if delta > 1.5 x reference"
    )
    assert table["  gap 1"].endswith("~3 missing periods)")
    assert table["Sequence counter [recorded]"] == "not defined as contiguous, modulus 8"
    assert table["Confirmed dropped records [recorded]"] == (
        "unknown (counter not defined as contiguous)"
    )
    assert table["Counter rollovers [recorded]"] == "1"
    assert table["Completeness [recorded]"] == "unknown"
    assert table["  rollover at sample 4"] == "5 -> 0"
    assert table["  missing at sample 3"] == "1 -> 5"


def test_format_handles_empty_and_many_gaps() -> None:
    empty = dict(format_quality(summarize_signal([], [])))
    assert empty["Signal"] == "-"
    assert empty["Interval"] == "[start of data, end of data] (closed)"
    assert empty["First time [inferred]"] == "-"
    assert empty["Delta min / median / max [inferred]"] == "- / - / -"
    assert empty["Rate estimate [inferred]"] == "-"
    assert empty["Gap reference [inferred]"] == "-"
    times = [index * 100 for index in range(MAX_EXAMPLES + 3)]
    many = format_quality(summarize_signal(times, [1.0] * len(times), expected_period_ns=10))
    labels = [label for label, _ in many]
    assert labels.count("  gap 1") == 1
    assert f"  gap {MAX_EXAMPLES + 1}" not in labels
    assert ("  ...", "2 more gaps") in many
    median = dict(format_quality(summarize_signal([0, MS, 2 * MS], [1.0] * 3, name="x")))
    assert median["Rate estimate [inferred]"] == "1000.000 Hz (from median delta)"
    assert median["Gap reference [inferred]"].startswith("1.000 ms median,")
    contiguous = dict(
        format_quality(
            summarize_signal([0, 1], [1.0] * 2, sequence=[0, 2], sequence_contiguous=True)
        )
    )
    assert contiguous["Sequence counter [recorded]"] == "contiguous, no rollover"
    assert contiguous["Confirmed dropped records [recorded]"] == "1"
    assert contiguous["Completeness [recorded]"] == "incomplete"


def test_large_input_is_fast_enough() -> None:
    count = 500_000
    times = array("q", range(0, count * 1000, 1000))
    values = array("d", [1.0]) * count
    counter = [index % 65536 for index in range(count)]
    quality = summarize_signal(
        times, values, sequence=counter, sequence_modulus=65536, sequence_contiguous=True
    )
    assert quality.values.valid == count
    assert quality.timing.gaps == ()
    assert quality.sequence is not None
    assert quality.sequence.rollovers == count // 65536
    assert quality.completeness is Completeness.COMPLETE


def test_signal_quality_is_frozen() -> None:
    quality = summarize_signal([0], [1.0])
    assert isinstance(quality, SignalQuality)
    with pytest.raises(AttributeError):
        quality.name = "other"  # type: ignore[misc]
