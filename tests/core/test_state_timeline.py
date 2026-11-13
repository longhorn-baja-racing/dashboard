"""State timelines: hold policy, gaps, categories, duplicates, windows and bounding."""

import time
from array import array
from dataclasses import FrozenInstanceError
from math import inf, nan

import pytest

from lbr_dashboard.core.state_timeline import (
    HOLD_POLICY,
    NO_DATA_LABEL,
    SegmentKind,
    StateSegment,
    StateTimeline,
    build_state_timeline,
)

STATE = SegmentKind.STATE
NO_DATA = SegmentKind.NO_DATA
UNKNOWN = SegmentKind.UNKNOWN
UNSUPPORTED = SegmentKind.UNSUPPORTED
MIXED = SegmentKind.MIXED


def _spans(timeline: StateTimeline) -> list[tuple[int, int, SegmentKind, str]]:
    return [(item.start_ns, item.end_ns, item.kind, item.label) for item in timeline.segments]


def _boundaries(timeline: StateTimeline) -> int:
    segments = timeline.segments
    return sum(1 for left, right in zip(segments, segments[1:]) if left.end_ns == right.start_ns)


def test_irregular_boolean_transitions_hold_until_the_next_sample() -> None:
    timeline = build_state_timeline([0, 7, 9, 30, 31], [False, True, True, False, True])
    assert _spans(timeline) == [
        (0, 7, STATE, "false"),
        (7, 30, STATE, "true"),
        (30, 31, STATE, "false"),
    ]
    # The last sample has no known duration without data_end_ns or max_hold_ns.
    assert timeline.transition_count == 2
    assert [item.time_ns for item in timeline.events] == [0, 7, 9, 30, 31]
    assert [item.transition for item in timeline.events] == [True, True, False, True, True]
    assert timeline.sample_count == 5
    assert timeline.hold_policy == HOLD_POLICY
    assert not timeline.segments_bounded
    assert not timeline.events_bounded


def test_last_sample_holds_until_data_end() -> None:
    timeline = build_state_timeline([0, 10], [True, False], data_end_ns=25)
    assert _spans(timeline) == [(0, 10, STATE, "true"), (10, 25, STATE, "false")]


def test_data_span_without_samples_is_no_data() -> None:
    timeline = build_state_timeline([5, 10], ["a", "b"], data_start_ns=0, data_end_ns=20)
    assert _spans(timeline) == [
        (0, 5, NO_DATA, NO_DATA_LABEL),
        (5, 10, STATE, "a"),
        (10, 20, STATE, "b"),
    ]
    empty = build_state_timeline([], [], data_start_ns=0, data_end_ns=20)
    assert _spans(empty) == [(0, 20, NO_DATA, NO_DATA_LABEL)]
    assert empty.events == ()


def test_max_hold_expiry_shows_no_data_instead_of_old_state() -> None:
    timeline = build_state_timeline(
        [0, 10, 100, 105], [True, True, False, True], max_hold_ns=20, data_end_ns=200
    )
    assert _spans(timeline) == [
        (0, 30, STATE, "true"),
        (30, 100, NO_DATA, NO_DATA_LABEL),
        (100, 105, STATE, "false"),
        (105, 125, STATE, "true"),
        (125, 200, NO_DATA, NO_DATA_LABEL),
    ]
    assert timeline.max_hold_ns == 20
    # The sample at 100 follows no data, so it is a transition.
    assert [item.transition for item in timeline.events] == [True, False, True, True]
    assert timeline.segments[1].no_data_ns == 70


def test_last_sample_holds_for_max_hold_when_no_end_is_known() -> None:
    timeline = build_state_timeline([0], ["on"], max_hold_ns=50)
    assert _spans(timeline) == [(0, 50, STATE, "on")]


def test_hold_exactly_reaching_the_next_sample_leaves_no_gap() -> None:
    timeline = build_state_timeline([0, 20], [1, 2], max_hold_ns=20)
    assert [item.kind for item in timeline.segments] == [STATE, STATE]
    assert timeline.segments[0].end_ns == 20


@pytest.mark.parametrize(
    "missing",
    [pytest.param(None, id="none"), pytest.param(nan, id="nan")],
)
def test_missing_samples_are_explicit_gaps(missing: object) -> None:
    timeline = build_state_timeline([0, 10, 20, 30, 40], [1, missing, missing, 1, 2])
    assert _spans(timeline) == [
        (0, 10, STATE, "1"),
        (10, 30, NO_DATA, NO_DATA_LABEL),
        (30, 40, STATE, "1"),
    ]
    assert [item.kind for item in timeline.events] == [STATE, NO_DATA, NO_DATA, STATE, STATE]
    assert timeline.events[1].value is None


def test_missing_sample_after_an_expired_hold_is_one_gap() -> None:
    timeline = build_state_timeline([0, 50, 60], [True, None, True], max_hold_ns=10)
    assert _spans(timeline) == [
        (0, 10, STATE, "true"),
        (10, 60, NO_DATA, NO_DATA_LABEL),
        (60, 70, STATE, "true"),
    ]
    assert timeline.events[1].transition is False


def test_rapid_toggles_keep_every_transition() -> None:
    times = list(range(0, 100, 1))
    values = [index % 2 == 0 for index in range(100)]
    timeline = build_state_timeline(times, values)
    assert len(timeline.segments) == 99
    assert timeline.transition_count == 98
    assert all(item.duration_ns == 1 for item in timeline.segments)
    assert all(item.transition for item in timeline.events)


def test_duplicate_timestamps_last_wins_and_all_are_events() -> None:
    times = [0, 10, 10, 10, 20]
    values = ["a", "b", "c", "d", "e"]
    timeline = build_state_timeline(times, values, data_end_ns=30)
    assert _spans(timeline) == [
        (0, 10, STATE, "a"),
        (10, 20, STATE, "d"),
        (20, 30, STATE, "e"),
    ]
    assert [(item.time_ns, item.index, item.label) for item in timeline.events] == [
        (0, 0, "a"),
        (10, 1, "b"),
        (10, 2, "c"),
        (10, 3, "d"),
        (20, 4, "e"),
    ]


def test_duplicate_that_restores_the_state_merges_the_segment() -> None:
    timeline = build_state_timeline([0, 10, 10, 20], [True, False, True, True])
    assert _spans(timeline) == [(0, 20, STATE, "true")]
    assert [item.transition for item in timeline.events] == [True, True, True, False]


def test_explicit_labels_and_unknown_categories_keep_the_raw_value() -> None:
    labels = {0: "neutral", 1: "first", 2: "second"}
    timeline = build_state_timeline([0, 10, 20, 30, 40], [0, 1, 7, "x", 2], labels=labels)
    assert _spans(timeline) == [
        (0, 10, STATE, "neutral"),
        (10, 20, STATE, "first"),
        (20, 30, UNKNOWN, "unknown: 7"),
        (30, 40, UNKNOWN, "unknown: 'x'"),
    ]
    assert timeline.segments[2].value == 7
    assert timeline.segments[3].value == "x"
    assert timeline.events[4].label == "second"


def test_bool_and_int_categories_stay_distinct() -> None:
    timeline = build_state_timeline([0, 10, 20], [True, 1, True], labels={True: "pressed"})
    assert [item.kind for item in timeline.segments] == [STATE, UNKNOWN]
    assert timeline.segments[1].value == 1
    assert type(timeline.segments[1].value) is int
    plain = build_state_timeline([0, 10, 20], [True, 1, False], data_end_ns=30)
    assert [item.label for item in plain.segments] == ["true", "1", "false"]


def test_default_labels_for_strings() -> None:
    timeline = build_state_timeline([0, 10], ["", "LOW"], data_end_ns=20)
    assert [item.label for item in timeline.segments] == ["(empty string)", "LOW"]


@pytest.mark.parametrize(
    ("value", "label"),
    [
        pytest.param(2.5, "unsupported: float 2.5", id="fraction"),
        pytest.param(inf, "unsupported: float inf", id="inf"),
        pytest.param(b"x", "unsupported type: bytes", id="bytes"),
        pytest.param([1], "unsupported type: list", id="list"),
    ],
)
def test_unsupported_values_say_why(value: object, label: str) -> None:
    timeline = build_state_timeline([0, 10], [value, True])
    segment = timeline.segments[0]
    assert (segment.kind, segment.label) == (UNSUPPORTED, label)
    assert segment.value == value


def test_integral_floats_are_int_categories() -> None:
    timeline = build_state_timeline(array("q", [0, 10, 20]), array("d", [0.0, 1.0, 1.0]))
    assert [(item.value, item.label) for item in timeline.segments] == [(0, "0"), (1, "1")]
    assert type(timeline.segments[1].value) is int


def test_empty_input() -> None:
    timeline = build_state_timeline([], [])
    assert timeline.is_empty
    assert timeline.segments == ()
    assert timeline.events == ()
    assert timeline.sample_count == 0
    assert timeline.transition_count == 0
    windowed = build_state_timeline([], [], window_start_ns=0, window_end_ns=10)
    assert windowed.is_empty


def test_all_missing_input_is_only_no_data() -> None:
    timeline = build_state_timeline([0, 10, 20], [None, None, None])
    assert _spans(timeline) == [(0, 20, NO_DATA, NO_DATA_LABEL)]
    assert not any(item.transition for item in timeline.events)


def test_window_clips_segments_and_keeps_state_from_before_it() -> None:
    times = [0, 10, 20, 30, 40]
    values = ["a", "b", "c", "d", "e"]
    timeline = build_state_timeline(
        times, values, data_end_ns=50, window_start_ns=15, window_end_ns=30
    )
    assert _spans(timeline) == [(15, 20, STATE, "b"), (20, 30, STATE, "c")]
    # The window is closed: the sample exactly at 30 is an event.
    assert [item.index for item in timeline.events] == [2, 3]
    assert (timeline.start_ns, timeline.end_ns) == (15, 30)


def test_window_between_two_samples_shows_the_held_state() -> None:
    timeline = build_state_timeline([0, 100], [True, False], window_start_ns=40, window_end_ns=60)
    assert _spans(timeline) == [(40, 60, STATE, "true")]
    assert timeline.events == ()
    assert timeline.sample_count == 0


def test_window_after_an_expired_hold_is_no_data() -> None:
    timeline = build_state_timeline(
        [0, 100], [True, False], max_hold_ns=10, window_start_ns=40, window_end_ns=60
    )
    assert _spans(timeline) == [(40, 60, NO_DATA, NO_DATA_LABEL)]


def test_window_starting_on_duplicates_keeps_all_their_events() -> None:
    timeline = build_state_timeline(
        [0, 10, 10, 20], ["a", "b", "c", "d"], window_start_ns=10, window_end_ns=15
    )
    assert _spans(timeline) == [(10, 15, STATE, "c")]
    assert [item.index for item in timeline.events] == [1, 2]
    assert timeline.events[0].transition


def test_first_window_sample_transition_uses_the_sample_before_the_window() -> None:
    times = [0, 10, 20]
    same = build_state_timeline(times, [1, 1, 2], window_start_ns=10)
    assert [item.transition for item in same.events] == [False, True]
    expired = build_state_timeline(times, [1, 1, 2], max_hold_ns=5, window_start_ns=10)
    assert [item.transition for item in expired.events] == [True, True]


@pytest.mark.parametrize(
    ("start", "end"),
    [pytest.param(-100, -50, id="before"), pytest.param(500, 600, id="after")],
)
def test_window_outside_the_data_is_empty(start: int, end: int) -> None:
    timeline = build_state_timeline(
        [0, 10, 20], [1, 2, 3], window_start_ns=start, window_end_ns=end
    )
    assert timeline.is_empty


def test_backwards_seek_gives_the_same_answer() -> None:
    times = list(range(0, 1000, 10))
    values = [index % 3 for index in range(100)]
    first = build_state_timeline(times, values, window_start_ns=200, window_end_ns=300)
    build_state_timeline(times, values, window_start_ns=800, window_end_ns=900)
    again = build_state_timeline(times, values, window_start_ns=200, window_end_ns=300)
    assert first == again


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        pytest.param({"max_hold_ns": 0}, "max_hold_ns", id="hold-zero"),
        pytest.param({"max_hold_ns": True}, "max_hold_ns", id="hold-bool"),
        pytest.param({"max_segments": 1}, "max_segments", id="segments"),
        pytest.param({"max_events": 0}, "max_events", id="events"),
        pytest.param({"window_start_ns": 5, "window_end_ns": 4}, "window_start", id="window"),
        pytest.param({"data_start_ns": 5, "data_end_ns": 4}, "data_start", id="data"),
        pytest.param({"labels": {1.5: "x"}}, "label keys", id="label-key"),
    ],
)
def test_bad_arguments_are_rejected(kwargs: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        build_state_timeline([0], [1], **kwargs)


def test_mismatched_lengths_and_unsorted_times_are_rejected() -> None:
    with pytest.raises(ValueError, match="same length"):
        build_state_timeline([0, 1], [1])
    with pytest.raises(ValueError, match="non-decreasing"):
        build_state_timeline([0, 20, 10], [1, 2, 3])


def test_inputs_are_not_mutated_and_results_are_frozen() -> None:
    times = [0, 10, 10, 20]
    values = [True, None, False, "x"]
    labels = {True: "on", False: "off"}
    build_state_timeline(times, values, labels=labels, max_hold_ns=5, max_segments=2)
    assert times == [0, 10, 10, 20]
    assert values == [True, None, False, "x"]
    assert labels == {True: "on", False: "off"}
    timeline = build_state_timeline(times, values)
    with pytest.raises(FrozenInstanceError):
        timeline.segments = ()  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        timeline.segments[0].label = "x"  # type: ignore[misc]


def test_too_many_segments_are_merged_with_countable_transitions() -> None:
    # Long "idle" stretches separated by bursts of rapid toggles.
    times: list[int] = []
    values: list[object] = []
    for burst in range(10):
        base = burst * 1_000_000
        times.append(base)
        values.append("idle")
        for step in range(200):
            times.append(base + 900_000 + step * 10)
            values.append(step % 2 == 0)
    timeline = build_state_timeline(times, values, data_end_ns=10_000_000, max_segments=50)
    assert timeline.segments_bounded
    assert len(timeline.segments) <= 50
    assert timeline.transition_count == 10 * 201 - 1
    hidden = sum(item.transitions for item in timeline.segments)
    assert hidden + _boundaries(timeline) == timeline.transition_count
    # The long idle stretches survive unmerged, and the bursts become mixed segments.
    idle = [item for item in timeline.segments if item.label == "idle"]
    assert len(idle) == 10
    mixed = [item for item in timeline.segments if item.kind is MIXED]
    assert mixed
    assert all(item.value is None and "changes" in item.label for item in mixed)
    # The merged segments still cover the same time without holes.
    assert timeline.segments[0].start_ns == 0
    assert timeline.segments[-1].end_ns == 10_000_000
    assert all(
        left.end_ns == right.start_ns
        for left, right in zip(timeline.segments, timeline.segments[1:])
    )


def test_merged_segments_report_hidden_no_data() -> None:
    times = list(range(0, 1000, 10))
    values = [None if index % 2 else index % 4 for index in range(100)]
    timeline = build_state_timeline(times, values, max_segments=4)
    assert len(timeline.segments) <= 4
    assert any("some no data" in item.label for item in timeline.segments)
    assert sum(item.no_data_ns for item in timeline.segments) == 490


def test_single_short_segment_between_long_ones_is_not_relabeled() -> None:
    times = [0, 10_000, 10_001, 20_001, 20_002, 30_002, 30_003, 30_004, 30_005]
    values = ["a", "b", "a", "b", "a", "c", "d", "c", "d"]
    timeline = build_state_timeline(times, values, data_end_ns=30_006, max_segments=8)
    assert [item.label for item in timeline.segments] == ["a", "b", "a", "b", "a", "3 changes"]
    assert [item.kind for item in timeline.segments][:5] == [STATE] * 5
    assert timeline.segments[-1].kind is MIXED
    assert timeline.segments[-1].transitions == 3


def test_large_event_lists_are_bounded_and_counted() -> None:
    count = 200_000
    times = array("q", range(0, count * 1000, 1000))
    values = [(index // 3) % 2 == 0 for index in range(count)]
    started = time.perf_counter()
    timeline = build_state_timeline(times, values, max_segments=500, max_events=1000)
    elapsed = time.perf_counter() - started
    assert elapsed < 10
    assert timeline.events_bounded
    assert len(timeline.events) <= 1000
    assert len(timeline.segments) <= 500
    assert timeline.sample_count == count
    assert sum(item.count for item in timeline.events) == count
    assert all(item.last_time_ns is not None for item in timeline.events)
    assert [item.time_ns for item in timeline.events] == sorted(
        item.time_ns for item in timeline.events
    )
    assert any(item.kind is MIXED for item in timeline.events)


def test_large_input_with_a_small_window_visits_only_the_window() -> None:
    count = 1_000_000
    times = array("q", range(count))
    values = [index % 7 for index in range(count)]
    started = time.perf_counter()
    timeline = build_state_timeline(times, values, window_start_ns=500_000, window_end_ns=500_010)
    assert time.perf_counter() - started < 1
    assert timeline.sample_count == 11
    assert len(timeline.segments) == 10


def test_combined_events_with_one_state_keep_it() -> None:
    timeline = build_state_timeline([0, 1, 2, 100], ["a", "a", "a", "b"], max_events=2)
    assert [(item.count, item.kind, item.label) for item in timeline.events] == [
        (3, STATE, "a"),
        (1, STATE, "b"),
    ]
    assert timeline.events[0].last_time_ns == 2
    assert timeline.events[0].transition


def test_segment_duration_property() -> None:
    assert StateSegment(5, 12, STATE, 1, "1").duration_ns == 7
