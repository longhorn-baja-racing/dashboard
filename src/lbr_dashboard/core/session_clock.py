"""Application-owned replay time and live-follow, independent of Qt and sample rate."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum
from fractions import Fraction
from math import isfinite
from threading import get_ident
from time import monotonic_ns

from .session import SessionSnapshot


class PlaybackMode(str, Enum):
    PAUSED = "paused"
    PLAYING = "playing"
    ENDED = "ended"
    LIVE = "live"


@dataclass(frozen=True, slots=True)
class ClockSnapshot:
    """One immutable frame delivered identically to every subscriber."""

    selected_ns: int | None
    start_ns: int | None
    end_ns: int | None
    speed: Fraction
    mode: PlaybackMode
    connected: bool
    reference: tuple[str, str, str] | None
    revision: int


class ClockSubscriberError(RuntimeError):
    """All subscribers were notified; one or more callbacks failed."""

    def __init__(self, errors: tuple[Exception, ...]) -> None:
        super().__init__(f"{len(errors)} session-clock subscriber(s) failed")
        self.errors = errors


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer number of nanoseconds")
    return value


def _bounds(start: int | None, end: int | None) -> None:
    if start is None and end is None:
        return
    if start is None or end is None:
        raise ValueError("both bounds must be present or absent")
    if _integer(start, "start_ns") > _integer(end, "end_ns"):
        raise ValueError("start_ns must not exceed end_ns")


def _rate(value: int | float | Fraction) -> Fraction:
    if isinstance(value, bool) or not isinstance(value, (int, float, Fraction)):
        raise ValueError("speed must be a positive finite number")
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError("speed must be a positive finite number")
        rate = Fraction(str(value))
    else:
        rate = Fraction(value)
    if rate <= 0:
        raise ValueError("speed must be positive")
    return rate


class SessionClock:
    """One owner advances time; subscribers render the published immutable frame.

    The application calls tick from its own pump. Reading snapshot never reads
    wall time, advances playback or invokes callbacks. Control mutations during
    subscriber delivery are rejected so every subscriber observes one frame.
    """

    def __init__(
        self,
        *,
        start_ns: int | None = None,
        end_ns: int | None = None,
        now_ns: Callable[[], int] = monotonic_ns,
    ) -> None:
        _bounds(start_ns, end_ns)
        if not callable(now_ns):
            raise TypeError("now_ns must be callable")
        self._now = now_ns
        self._last_now = _integer(now_ns(), "monotonic clock")
        self._owner = get_ident()
        self._start, self._end = start_ns, end_ns
        self._position = Fraction(start_ns) if start_ns is not None else None
        self._speed = Fraction(1)
        self._mode = PlaybackMode.PAUSED
        self._connected = True
        self._reference: tuple[str, str, str] | None = None
        self._listeners: dict[int, Callable[[ClockSnapshot], None]] = {}
        self._next_listener = 0
        self._dispatching = False
        self._frame = self._make_frame(0)

    def _make_frame(self, revision: int) -> ClockSnapshot:
        selected = (
            self._position.numerator // self._position.denominator
            if self._position is not None
            else None
        )
        return ClockSnapshot(
            selected,
            self._start,
            self._end,
            self._speed,
            self._mode,
            self._connected,
            self._reference,
            revision,
        )

    def snapshot(self) -> ClockSnapshot:
        return self._frame

    def _check_owner(self, *, mutation: bool = True) -> None:
        if get_ident() != self._owner:
            raise RuntimeError("session-clock controls belong to the application owner thread")
        if mutation and self._dispatching:
            raise RuntimeError("defer clock commands until subscriber delivery finishes")

    def _advance(self, now: int) -> None:
        if self._mode is PlaybackMode.PLAYING:
            assert self._position is not None and self._end is not None
            self._position += (now - self._last_now) * self._speed
            if self._position >= self._end:
                self._position = Fraction(self._end)
                self._mode = PlaybackMode.ENDED
        self._last_now = now

    def _command(self, action: Callable[[], None]) -> ClockSnapshot:
        self._check_owner()
        now = _integer(self._now(), "monotonic clock")
        if now < self._last_now:
            raise ValueError("injected monotonic clock moved backwards")
        self._advance(now)
        action()
        candidate = self._make_frame(self._frame.revision)
        if candidate == self._frame:
            return self._frame
        self._frame = replace(candidate, revision=candidate.revision + 1)
        frame = self._frame
        self._dispatching = True
        errors = []
        try:
            for listener in tuple(self._listeners.values()):
                try:
                    listener(frame)
                except Exception as error:
                    errors.append(error)
        finally:
            self._dispatching = False
        if errors:
            raise ClockSubscriberError(tuple(errors))
        return frame

    def subscribe(self, listener: Callable[[ClockSnapshot], None]) -> Callable[[], None]:
        """Deliver current state immediately; return an idempotent unsubscribe."""
        self._check_owner()
        if not callable(listener):
            raise TypeError("listener must be callable")
        self._next_listener += 1
        identity = self._next_listener
        self._listeners[identity] = listener
        self._dispatching = True
        try:
            listener(self._frame)
        except Exception:
            del self._listeners[identity]
            raise
        finally:
            self._dispatching = False

        def unsubscribe() -> None:
            self._check_owner(mutation=False)
            self._listeners.pop(identity, None)

        return unsubscribe

    def tick(self) -> ClockSnapshot:
        return self._command(lambda: None)

    def play(self) -> ClockSnapshot:
        def action() -> None:
            if self._position is None:
                self._mode = PlaybackMode.PAUSED
            elif self._end is not None and self._position >= self._end:
                self._mode = PlaybackMode.ENDED
            else:
                self._mode = PlaybackMode.PLAYING

        return self._command(action)

    def pause(self) -> ClockSnapshot:
        def action() -> None:
            self._mode = PlaybackMode.PAUSED

        return self._command(action)

    def seek(self, timestamp_ns: int) -> ClockSnapshot:
        timestamp_ns = _integer(timestamp_ns, "timestamp_ns")

        def action() -> None:
            self._mode = PlaybackMode.PAUSED
            if self._start is not None and self._end is not None:
                self._position = Fraction(min(max(timestamp_ns, self._start), self._end))

        return self._command(action)

    def set_speed(self, speed: int | float | Fraction) -> ClockSnapshot:
        rate = _rate(speed)

        def action() -> None:
            self._speed = rate

        return self._command(action)

    def follow_live(self) -> ClockSnapshot:
        def action() -> None:
            self._mode = PlaybackMode.LIVE
            if self._connected and self._end is not None:
                self._position = Fraction(self._end)

        return self._command(action)

    def set_connected(self, connected: bool) -> ClockSnapshot:
        if not isinstance(connected, bool):
            raise ValueError("connected must be boolean")

        def action() -> None:
            self._connected = connected
            if connected and self._mode is PlaybackMode.LIVE and self._end is not None:
                self._position = Fraction(self._end)

        return self._command(action)

    def update_bounds(self, start_ns: int | None, end_ns: int | None) -> ClockSnapshot:
        _bounds(start_ns, end_ns)

        return self._command(lambda: self._set_bounds(start_ns, end_ns))

    def _set_bounds(self, start_ns: int | None, end_ns: int | None) -> None:
        self._start, self._end = start_ns, end_ns
        if start_ns is None or end_ns is None:
            self._position = None
            if self._mode is not PlaybackMode.LIVE:
                self._mode = PlaybackMode.PAUSED
        elif self._mode is PlaybackMode.LIVE and self._connected:
            self._position = Fraction(end_ns)
        elif self._position is None:
            self._position = Fraction(start_ns)
        else:
            self._position = min(max(self._position, Fraction(start_ns)), Fraction(end_ns))
            if self._mode is PlaybackMode.PLAYING and self._position >= end_ns:
                self._mode = PlaybackMode.ENDED

    def update_from_session(self, snapshot: SessionSnapshot, signal_id: str) -> ClockSnapshot:
        """Use one declared reference timeline; never infer cross-device alignment."""
        self._check_owner()
        if not isinstance(snapshot, SessionSnapshot):
            raise TypeError("snapshot must be SessionSnapshot")
        bounds = snapshot.bounds(signal_id)
        descriptor = next(item for item in snapshot.schema.signals if item.identifier == signal_id)
        domain = snapshot.domains.get(signal_id, descriptor.timestamp_domain)
        if bounds is None and domain == "unspecified" and self._reference is not None:
            if self._reference[:2] == (signal_id, descriptor.source_id):
                domain = self._reference[2]
        reference = (signal_id, descriptor.source_id, domain)
        if self._reference is not None and self._reference != reference:
            raise ValueError(
                "reference signal/source/clock domain changed; create a new session clock"
            )

        # Validate the clock before changing binding metadata as well as time.
        def action() -> None:
            self._set_bounds(*(bounds if bounds is not None else (None, None)))
            if bounds is not None:
                self._reference = reference

        return self._command(action)
