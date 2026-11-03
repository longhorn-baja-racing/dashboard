"""Closed time intervals and the shared interval selection state."""

from array import array
from dataclasses import FrozenInstanceError

import pytest

from lbr_dashboard.core.interval import (
    IntervalListenerError,
    IntervalSelection,
    TimeInterval,
)

EPOCH_NS = 1_700_000_000_000_000_000


class Recorder:
    def __init__(self) -> None:
        self.values: list[TimeInterval | None] = []

    def __call__(self, value: TimeInterval | None) -> None:
        self.values.append(value)


# TimeInterval


def test_interval_is_closed_and_frozen() -> None:
    interval = TimeInterval(10, 20)
    assert interval.contains(10)
    assert interval.contains(20)
    assert not interval.contains(9)
    assert not interval.contains(21)
    assert interval.duration_ns == 10
    with pytest.raises(FrozenInstanceError):
        interval.start_ns = 0  # type: ignore[misc]


def test_zero_width_interval_is_a_single_instant() -> None:
    instant = TimeInterval(EPOCH_NS, EPOCH_NS)
    assert instant.duration_ns == 0
    assert instant.contains(EPOCH_NS)
    assert not instant.contains(EPOCH_NS + 1)


def test_reversed_constructor_is_rejected_but_between_swaps() -> None:
    with pytest.raises(ValueError, match="after its end"):
        TimeInterval(20, 10)
    assert TimeInterval.between(20, 10) == TimeInterval(10, 20)
    assert TimeInterval.between(-5, -50) == TimeInterval(-50, -5)


@pytest.mark.parametrize("bad", [1.0, True, "1", None, float("nan")])
def test_endpoints_must_be_integer_nanoseconds(bad: object) -> None:
    with pytest.raises(TypeError, match="integer nanoseconds"):
        TimeInterval(bad, 5)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="integer nanoseconds"):
        TimeInterval(0, bad)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="integer nanoseconds"):
        TimeInterval.between(bad, 5)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="integer nanoseconds"):
        TimeInterval.between(5, bad)  # type: ignore[arg-type]


def test_overlap_and_clamp_use_closed_endpoints() -> None:
    bounds = TimeInterval(0, 100)
    assert TimeInterval(-10, 50).clamp(bounds) == TimeInterval(0, 50)
    assert TimeInterval(50, 500).clamp(bounds) == TimeInterval(50, 100)
    assert TimeInterval(-10, 500).clamp(bounds) == bounds
    assert TimeInterval(10, 20).clamp(bounds) == TimeInterval(10, 20)
    assert TimeInterval(100, 200).clamp(bounds) == TimeInterval(100, 100)
    assert TimeInterval(-50, 0).clamp(bounds) == TimeInterval(0, 0)
    assert TimeInterval(101, 200).clamp(bounds) is None
    assert TimeInterval(-50, -1).clamp(bounds) is None
    assert TimeInterval(100, 200).overlaps(bounds)
    assert not TimeInterval(101, 200).overlaps(bounds)


def test_sample_slice_cuts_mixed_rate_signals_by_time() -> None:
    interval = TimeInterval(EPOCH_NS + 10, EPOCH_NS + 30)
    fast = array("q", (EPOCH_NS + step for step in range(0, 41, 5)))  # 200 Hz-like
    slow = array("q", (EPOCH_NS + step for step in range(0, 41, 20)))  # 50 Hz-like
    assert list(fast[interval.sample_slice(fast)]) == [EPOCH_NS + t for t in (10, 15, 20, 25, 30)]
    assert list(slow[interval.sample_slice(slow)]) == [EPOCH_NS + 20]
    repeated = [0, 10, 10, 10, 30, 31]
    assert repeated[TimeInterval(10, 30).sample_slice(repeated)] == [10, 10, 10, 30]
    assert repeated[TimeInterval(11, 29).sample_slice(repeated)] == []
    assert [][TimeInterval(0, 1).sample_slice([])] == []


# IntervalSelection


def test_selection_starts_empty_and_normalizes_reversed_endpoints() -> None:
    selection = IntervalSelection()
    assert selection.interval is None
    assert selection.bounds is None
    assert selection.select(30, 10) == TimeInterval(10, 30)
    assert selection.interval == TimeInterval(10, 30)
    assert selection.select(7, 7) == TimeInterval(7, 7)
    selection.clear()
    assert selection.interval is None


def test_selection_is_clamped_to_bounds() -> None:
    selection = IntervalSelection(TimeInterval(0, 100))
    assert selection.select(-20, 50) == TimeInterval(0, 50)
    assert selection.select(150, 80) == TimeInterval(80, 100)
    assert selection.select(100, 300) == TimeInterval(100, 100)


def test_out_of_bounds_selection_is_rejected_without_changing_state() -> None:
    selection = IntervalSelection(TimeInterval(0, 100))
    recorder = Recorder()
    selection.subscribe(recorder)
    selection.select(10, 20)
    for first, second in ((101, 200), (-50, -1), (300, 200)):
        with pytest.raises(ValueError, match="outside the session range"):
            selection.select(first, second)
    assert selection.interval == TimeInterval(10, 20)
    assert recorder.values == [TimeInterval(10, 20)]


def test_invalid_endpoint_types_leave_state_unchanged() -> None:
    selection = IntervalSelection()
    selection.select(1, 2)
    with pytest.raises(TypeError):
        selection.select(1.5, 2)  # type: ignore[arg-type]
    assert selection.interval == TimeInterval(1, 2)


def test_endpoint_edits_swap_when_crossing() -> None:
    selection = IntervalSelection(TimeInterval(0, 100))
    selection.select(20, 40)
    assert selection.set_start(30) == TimeInterval(30, 40)
    assert selection.set_end(90) == TimeInterval(30, 90)
    assert selection.set_start(95) == TimeInterval(90, 95)
    assert selection.set_end(10) == TimeInterval(10, 90)
    assert selection.set_end(500) == TimeInterval(10, 100)
    assert selection.set_start(100) == TimeInterval(100, 100)
    # The kept endpoint is always in bounds, so an edit is clamped, never rejected.
    assert selection.set_start(-1_000) == TimeInterval(0, 100)


def test_endpoint_edit_needs_a_selection() -> None:
    selection = IntervalSelection()
    with pytest.raises(ValueError, match="No interval"):
        selection.set_start(1)
    with pytest.raises(ValueError, match="No interval"):
        selection.set_end(1)
    assert selection.interval is None


def test_bounds_change_reclamps_or_clears_selection() -> None:
    selection = IntervalSelection(TimeInterval(0, 100))
    recorder = Recorder()
    selection.subscribe(recorder)
    selection.select(20, 80)
    selection.set_bounds(TimeInterval(50, 200))
    assert selection.interval == TimeInterval(50, 80)
    selection.set_bounds(TimeInterval(0, 1_000))  # Widening never restores the old range.
    assert selection.interval == TimeInterval(50, 80)
    selection.set_bounds(None)
    assert selection.bounds is None
    assert selection.select(-5, 5_000) == TimeInterval(-5, 5_000)
    selection.set_bounds(TimeInterval(6_000, 7_000))
    assert selection.interval is None
    assert selection.bounds == TimeInterval(6_000, 7_000)
    selection.set_bounds(TimeInterval(0, 10))  # Nothing selected: nothing to notify.
    assert recorder.values == [
        TimeInterval(20, 80),
        TimeInterval(50, 80),
        TimeInterval(-5, 5_000),
        None,
    ]


def test_listeners_run_in_order_and_only_on_change() -> None:
    selection = IntervalSelection(TimeInterval(0, 100))
    calls: list[tuple[str, TimeInterval | None]] = []
    selection.subscribe(lambda value: calls.append(("statistics", value)))
    selection.subscribe(lambda value: calls.append(("export", value)))
    selection.select(10, 20)
    selection.select(20, 10)  # Same interval after normalization.
    selection.select(-5, 20)
    selection.select(0, 20)  # Same interval after clamping.
    selection.clear()
    selection.clear()
    assert calls == [
        ("statistics", TimeInterval(10, 20)),
        ("export", TimeInterval(10, 20)),
        ("statistics", TimeInterval(0, 20)),
        ("export", TimeInterval(0, 20)),
        ("statistics", None),
        ("export", None),
    ]


def test_consumers_read_the_same_shared_interval() -> None:
    selection = IntervalSelection()
    seen: dict[str, TimeInterval | None] = {}
    selection.subscribe(lambda _value: seen.update(statistics=selection.interval))
    selection.subscribe(lambda _value: seen.update(export=selection.interval))
    selection.select(EPOCH_NS, EPOCH_NS + 5)
    assert seen == {"statistics": selection.interval, "export": selection.interval}


def test_failing_listener_does_not_block_others_or_corrupt_state() -> None:
    selection = IntervalSelection()
    first, last = Recorder(), Recorder()
    boom = RuntimeError("boom")

    def broken(_value: TimeInterval | None) -> None:
        raise boom

    selection.subscribe(first)
    selection.subscribe(broken)
    selection.subscribe(broken)
    selection.subscribe(last)
    with pytest.raises(IntervalListenerError) as caught:
        selection.select(1, 2)
    assert caught.value.errors == (boom, boom)
    assert caught.value.__cause__ is boom
    assert "2 interval listener(s) failed" in str(caught.value)
    assert selection.interval == TimeInterval(1, 2)
    assert first.values == last.values == [TimeInterval(1, 2)]
    with pytest.raises(IntervalListenerError):
        selection.clear()
    assert selection.interval is None
    assert last.values == [TimeInterval(1, 2), None]


def test_unsubscribe_stops_notifications_and_is_idempotent() -> None:
    selection = IntervalSelection()
    recorder = Recorder()
    unsubscribe = selection.subscribe(recorder)
    selection.select(1, 2)
    unsubscribe()
    unsubscribe()
    selection.select(3, 4)
    assert recorder.values == [TimeInterval(1, 2)]


def test_same_listener_subscribed_twice_is_removed_per_subscription() -> None:
    selection = IntervalSelection()
    recorder = Recorder()
    unsubscribe_first = selection.subscribe(recorder)
    selection.subscribe(recorder)
    selection.select(1, 2)
    unsubscribe_first()
    selection.select(3, 4)
    assert recorder.values == [TimeInterval(1, 2), TimeInterval(1, 2), TimeInterval(3, 4)]


def test_listener_unsubscribed_during_notification_is_skipped() -> None:
    selection = IntervalSelection()
    later = Recorder()
    unsubscribe_later = None

    def remove_later(_value: TimeInterval | None) -> None:
        assert unsubscribe_later is not None
        unsubscribe_later()

    selection.subscribe(remove_later)
    unsubscribe_later = selection.subscribe(later)
    selection.select(1, 2)
    assert later.values == []


def test_change_made_by_a_listener_is_delivered_in_order_after_the_current_one() -> None:
    selection = IntervalSelection(TimeInterval(0, 100))
    calls: list[tuple[str, TimeInterval | None]] = []

    def widen_instants(value: TimeInterval | None) -> None:
        calls.append(("widen", value))
        if value is not None and value.duration_ns == 0:
            selection.select(value.start_ns, value.start_ns + 10)

    selection.subscribe(widen_instants)
    selection.subscribe(lambda value: calls.append(("other", value)))
    selection.select(5, 5)
    assert selection.interval == TimeInterval(5, 15)
    assert calls == [
        ("widen", TimeInterval(5, 5)),
        ("other", TimeInterval(5, 5)),
        ("widen", TimeInterval(5, 15)),
        ("other", TimeInterval(5, 15)),
    ]


def test_epoch_scale_endpoints_are_exact() -> None:
    selection = IntervalSelection(TimeInterval(EPOCH_NS, EPOCH_NS + 3_600 * 10**9))
    selected = selection.select(EPOCH_NS + 123_456_789, EPOCH_NS + 1)
    assert selected == TimeInterval(EPOCH_NS + 1, EPOCH_NS + 123_456_789)
    assert selected.duration_ns == 123_456_788
