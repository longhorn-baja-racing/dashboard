# One session clock for replay and live-follow

Issue #22 adds the Qt-free `SessionClock`, immutable `ClockSnapshot` and
`PlaybackMode` types. The application owns one clock and one pump that calls
`tick()`. Widgets subscribe to the resulting frame; they do not calculate their
own selected times or create playback timers. Production replay controls/cursor
integration belong to #40/#41. This change adds the shared state and tests.

## Recorded time and progression

The default time provider is `time.monotonic_ns`. Tests inject a callable that
returns integer nanoseconds, so progression is verified without sleeping.
Commands and ticks sample that provider once; `snapshot()` never samples it or
advances playback. Clock values may have any monotonic origin. A non-integer
return or backwards jump is rejected without changing the published frame.

Selected time uses the reference session's recorded integer nanoseconds; it is
not wall-clock time or packet-arrival time. Negative/large timestamps retain
precision. Playback advances by elapsed monotonic nanoseconds multiplied by the
configured rate. Internal `Fraction` arithmetic retains sub-nanosecond remainders
across ticks, pause/resume and speed changes. Published integer time is floored,
with less than one nanosecond quantization error; progression does not accumulate
one rounding error per tick. Finite positive integers, floats and Fractions are
accepted as speeds; decimal float spelling determines its exact rational rate.

`update_from_session(snapshot, signal_id)` uses #21's retained bounds for one
explicit reference signal. The frame identifies that signal, source and clock
domain in its `reference` tuple. Other widgets query their data at that selected
time only when the adapter/session has established a common timeline. The clock
does not infer alignment between unrelated device clocks. Changing a bound
reference's signal/source/domain requires a new session clock; an empty
unspecified session may wait for its first actual domain before binding.

## Commands and boundary policy

| Operation | Behavior |
| --- | --- |
| `play()` | Advances from the current selection at the configured speed. Empty bounds remain paused. At the end it stays ended; there is no automatic rewind or loop. Seek to the start before playing again. |
| `pause()` | First accounts for elapsed playback, then holds the exact position. It also exits live-follow. |
| `seek(timestamp_ns)` | Clamps to retained bounds, selects that exact integer time and pauses. Backwards seeking is supported. With no bounds there is no selected time. Seeking out of live-follow explicitly detaches it. |
| `set_speed(rate)` | Accounts for elapsed playback at the old rate, then applies the new positive rate without a position jump. |
| `tick()` | Advances playing state once and publishes a changed frame. It does not extrapolate live-follow beyond recorded bounds. |
| `update_bounds(start_ns, end_ns)` | Updates one declared timeline. Selection clamps into retained bounds, including when eviction advances the start. Shrinking bounds can end playback; later expansion does not automatically resume ended playback. |
| `follow_live()` | Follows the latest recorded end while connected. Speed is retained for later replay and does not multiply the live timestamp. |
| `set_connected(False)` | Keeps live-follow intent but holds its last selected time while disconnected; it does not advance using reception/wall time. |
| `set_connected(True)` | If still following live, jumps to the latest known recorded end. A paused or manually sought selection remains detached. |

Both bounds must be present or absent; reversed bounds and non-integer times are
errors. A single recorded timestamp is valid and immediately ends playback.
Clearing bounds clears selected time and pauses replay. Live-follow intent may
remain active with no data, waiting for bounds. If retention removes a held
selection, even a paused/disconnected selection clamps to retained bounds; the
clock never reports a time outside the queryable interval.

Reconnect assumes continuity of the declared recorded timeline. A device counter
reset/new epoch needs an explicit adapter mapping or a new session/clock; it must
not silently be treated as synchronized with earlier measurements.

## Gaps and unrelated signal rates

Selected time progresses continuously through gaps and irregular sample periods.
It does not snap to a measurement or depend on how often any sensor publishes.
Consumers use the same frame's `selected_ns` with #21 range/nearest queries and
an explicit distance tolerance. An unobserved gap can return no sample; an
explicitly unavailable record remains unavailable. The clock does not interpolate,
forward-fill, invent zero, alter units/quality or rewrite source timestamps.

## Subscriber and owner rules

`subscribe(callback)` immediately supplies the current immutable frame and
returns an idempotent unsubscribe function. Every subsequent changed state is
delivered as the **same frame object**, with one revision and selected time, to
all subscribers present at the start of that delivery. A no-op tick does not
create a revision. Reads can hand that immutable frame to another thread;
controls, subscription and unsubscription belong to the creating owner thread.

Subscribers must render the supplied frame. Mutating the clock or subscribing
again during delivery is rejected; defer that command to the next application
pump. Unsubscription during delivery applies to later frames. If one callback
throws, the remaining subscribers still receive the frame, then the owner gets
`ClockSubscriberError` containing the failures. The already committed clock state
is retained. Initial subscription failure removes that registration and
propagates the original exception.

The owner must remove callbacks before disposing their presentations. The clock
owns no background thread, Qt object or hidden timer. A future application timer
can call `tick`; two independent widget timers must not each advance selection.

## Deterministic example

```python
from fractions import Fraction
from lbr_dashboard.core import PlaybackMode, SessionClock

now = [0]
clock = SessionClock(start_ns=0, end_ns=1_000_000_000, now_ns=lambda: now[0])
first, second = [], []
unsubscribe_a = clock.subscribe(first.append)
unsubscribe_b = clock.subscribe(second.append)
clock.set_speed(Fraction(1, 2))
clock.play()
now[0] += 400_000_000
frame = clock.tick()
assert frame.selected_ns == 200_000_000
assert first[-1] is second[-1] is frame
clock.seek(0)
assert clock.snapshot().mode is PlaybackMode.PAUSED
unsubscribe_a()
unsubscribe_b()
```

`tests/core/test_session_clock.py` checks exact fake-time behavior, fractional
precision, end/bounds/gaps, backwards seek, reconnect, session binding and
subscriber failure/reentrancy. `tests/ui/test_clock_workflow.py` verifies two
native presentations consume identical frames from one owner pump.
