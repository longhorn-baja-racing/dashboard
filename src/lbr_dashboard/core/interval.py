"""Closed time intervals and the one shared, session-level interval selection.

All times are integer nanoseconds on the session time base. An interval is a
pure timestamp range: it never refers to sample indices, so signals recorded at
different rates are cut by the same rule.

Intervals are CLOSED: a sample at exactly ``start_ns`` or ``end_ns`` is inside.
A zero-width interval (``start_ns == end_ns``) is valid and selects a single
instant. Selection does not own playback time; the session clock does.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TypeAlias


def _require_ns(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be integer nanoseconds, not {type(value).__name__}")


@dataclass(frozen=True, slots=True)
class TimeInterval:
    """A closed range of integer nanosecond timestamps with ``start_ns <= end_ns``."""

    start_ns: int
    end_ns: int

    def __post_init__(self) -> None:
        _require_ns("start_ns", self.start_ns)
        _require_ns("end_ns", self.end_ns)
        if self.start_ns > self.end_ns:
            raise ValueError(
                f"Interval start {self.start_ns} ns is after its end {self.end_ns} ns; "
                "use TimeInterval.between to accept endpoints in either order"
            )

    @classmethod
    def between(cls, first_ns: int, second_ns: int) -> TimeInterval:
        """Build an interval from two endpoints given in either order."""

        _require_ns("first_ns", first_ns)
        _require_ns("second_ns", second_ns)
        return cls(min(first_ns, second_ns), max(first_ns, second_ns))

    @property
    def duration_ns(self) -> int:
        """Return ``end_ns - start_ns``; zero for a single instant."""

        return self.end_ns - self.start_ns

    def contains(self, time_ns: int) -> bool:
        """Return whether ``time_ns`` is inside, endpoints included."""

        return self.start_ns <= time_ns <= self.end_ns

    def overlaps(self, other: TimeInterval) -> bool:
        """Return whether the intervals share at least one instant (touching counts)."""

        return self.start_ns <= other.end_ns and other.start_ns <= self.end_ns

    def clamp(self, bounds: TimeInterval) -> TimeInterval | None:
        """Return the part of this interval inside ``bounds``, or None without overlap."""

        if not self.overlaps(bounds):
            return None
        return TimeInterval(max(self.start_ns, bounds.start_ns), min(self.end_ns, bounds.end_ns))

    def sample_slice(self, times_ns: Sequence[int]) -> slice:
        """Return the slice of samples inside this interval.

        ``times_ns`` must be non-decreasing. Each signal is cut by its own
        timestamps, so signals with different sample rates share one interval.
        """

        return slice(bisect_left(times_ns, self.start_ns), bisect_right(times_ns, self.end_ns))


IntervalListener: TypeAlias = Callable[[TimeInterval | None], None]


class IntervalListenerError(Exception):
    """One or more listeners failed; the selection change itself was applied."""

    def __init__(self, errors: Sequence[Exception]) -> None:
        self.errors = tuple(errors)
        super().__init__(
            f"{len(self.errors)} interval listener(s) failed after the selection changed"
        )


class IntervalSelection:
    """The one interval selection shared by every consumer of a session.

    ``bounds`` is the session time range. When set, every selection is clamped
    to it; a request entirely outside it raises ValueError and leaves the state
    unchanged. Reversed endpoints are swapped. Listeners receive the new
    ``TimeInterval | None`` in subscription order, only when the value changes.
    """

    def __init__(self, bounds: TimeInterval | None = None) -> None:
        self._bounds = bounds
        self._interval: TimeInterval | None = None
        self._listeners: dict[object, IntervalListener] = {}
        self._pending: deque[TimeInterval | None] = deque()
        self._notifying = False

    @property
    def bounds(self) -> TimeInterval | None:
        return self._bounds

    @property
    def interval(self) -> TimeInterval | None:
        return self._interval

    def select(self, first_ns: int, second_ns: int) -> TimeInterval:
        """Select the closed range between two endpoints and return the stored interval."""

        requested = TimeInterval.between(first_ns, second_ns)
        if self._bounds is None:
            selected = requested
        else:
            clamped = requested.clamp(self._bounds)
            if clamped is None:
                raise ValueError(
                    f"Interval {requested.start_ns}..{requested.end_ns} ns is outside the "
                    f"session range {self._bounds.start_ns}..{self._bounds.end_ns} ns"
                )
            selected = clamped
        self._set(selected)
        return selected

    def set_start(self, start_ns: int) -> TimeInterval:
        """Move the start; a start past the current end swaps the endpoints."""

        return self.select(start_ns, self._require_interval().end_ns)

    def set_end(self, end_ns: int) -> TimeInterval:
        """Move the end; an end before the current start swaps the endpoints."""

        return self.select(self._require_interval().start_ns, end_ns)

    def clear(self) -> None:
        self._set(None)

    def set_bounds(self, bounds: TimeInterval | None) -> None:
        """Replace the session range and re-clamp the selection (cleared without overlap)."""

        self._bounds = bounds
        if bounds is not None and self._interval is not None:
            self._set(self._interval.clamp(bounds))

    def subscribe(self, listener: IntervalListener) -> Callable[[], None]:
        """Register ``listener`` and return a callable that removes it (safe to call twice)."""

        token = object()
        self._listeners[token] = listener

        def unsubscribe() -> None:
            self._listeners.pop(token, None)

        return unsubscribe

    def _require_interval(self) -> TimeInterval:
        if self._interval is None:
            raise ValueError("No interval is selected to edit")
        return self._interval

    def _set(self, value: TimeInterval | None) -> None:
        if value == self._interval:
            return
        self._interval = value
        self._pending.append(value)
        if self._notifying:
            return  # A listener changed the selection; deliver it after the current value.
        self._notifying = True
        errors: list[Exception] = []
        try:
            while self._pending:
                current = self._pending.popleft()
                for token, listener in list(self._listeners.items()):
                    if token not in self._listeners:
                        continue  # Unsubscribed by an earlier listener.
                    try:
                        listener(current)
                    except Exception as error:
                        errors.append(error)
        finally:
            self._notifying = False
            self._pending.clear()
        if errors:
            raise IntervalListenerError(errors) from errors[0]
