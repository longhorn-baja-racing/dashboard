# Deterministic generic simulator

Issue #23 adds `SimulatedSignal`, `SimulatorSource`, `SimulatorStats` and two
fictional `example_signals` families. They use the #18 descriptors and the #20
source contract; no hardware, framing codec, replay clock or vehicle model is
required. No channel name is required by the application. The standalone CSV
fixtures in #105 and the production M0 source-selection UI in #102 remain
separate work. Local developer testing controls are outside the package.

## Signal configuration

Each immutable `SimulatedSignal` supplies its descriptor, a nonempty repeating
cycle of typed values, optional numeric noise amplitude and sample-index fault
windows. Names, hierarchy, source/device identity, units, type/shape, clock domain
and positive rate come from its ordinary descriptor. Numeric, boolean, string,
status/event, vector and unknown JSON-compatible types use #18 validation. The
simulator requires a specified clock domain and nominal rate at most 1 GHz;
values and nested configuration are immutable snapshots. Unordered/non-sequence
cycles are rejected.

Times are `start_ns + floor(index * 1_000_000_000 / rate_hz)`. Each index advances
on that signal's own schedule, including lost/stale/unavailable attempts. A 3 Hz
signal has offsets 0, 333333333, 666666666, 1000000000; repeatedly rounding a period
does not accumulate timestamp drift. Negative and large starts retain integer
precision. Equal-time measurements are ordered by stable signal ID. Source input
order does not change that ordering or generated values.

Seeded numeric noise is uniform within the supplied amplitude and uses an
independent stream for each signal ID. Repeating a run with the same configuration
and seed reproduces timestamps, values and fault events. Adding an unrelated
signal does not alter the existing signal's random values. A different seed
changes noisy values, not the schedule. Non-noisy cycles remain exact, including
real zero and false. There is no calibration/unit conversion or inferred sensor
meaning.

## Missingness and fault windows

Windows are sorted disjoint `[start_index, stop_index)` intervals for a single
signal. The three kinds must not overlap. They are absolute sample indices in a
run, not repeating windows; restarting the source restarts indices and seed.

| Window | Records and observability |
| --- | --- |
| `loss` | Scheduled measurements are omitted. A recoverable `simulated_loss` error marks the window's start with signal/time context. Injected lost count increases and remains in persistent source health. |
| `unavailable` | Each scheduled record is present with `available=False`, `value=None`, quality `simulated_unavailable`. It is never represented as zero or skipped as if lost. |
| `stale` | No new record is fabricated. A recoverable `simulated_stale` error marks the start; the persistent stale counter increases. Consumers choose an explicit age/gap display policy rather than extrapolating old values. |

Ordinary records use quality `simulated`. Heartbeats continue on their independent
configured schedule, including while all signals are lost/stale. They report
running state and injected lost count; they do not fabricate measurements. The
existing source contract keeps the most recent error, while bounded queue/history
may discard older errors; persistent `simulator_stats` retains aggregate fault
counts. `source.health().dropped_records` includes injected loss plus actual queue
record loss. `source.events.stats.dropped_records` identifies queue loss alone.
Stale and explicit unavailable counters are separate from loss.

`disconnect_after_ns` is an elapsed offset from the run start. It produces fatal
`simulated_disconnect` with the exact recorded timestamp in context, then the
source reports terminal `failed`. A disconnect wins over measurements at the
same offset. A disconnect after the finite duration does not prevent natural end.
The ordinary cooperative stop/cancellation path reports `cancelled`, not failure.

## Finite, live and pacing

`duration_ns` is an inclusive elapsed bound; zero emits the initial measurements.
The default is a one-second finite accelerated run with no clock reads or waits.
It naturally reports `complete`. `duration_ns=None` advertises live capability;
the owner must cancel/close it (or configure disconnect).

`realtime=True` paces production using injected/default monotonic nanoseconds and
cooperative waits of at most one second per step. Recorded measurement times are
the schedule, not late arrival time. A late worker does not rewrite timestamps or
silently skip attempted measurements. Real-time finite runs wait through their
duration, even if the last measurement is earlier. `now_ns` and `wait(token, ns)`
are injectable so pacing/cancellation tests can advance fake time with no sleeps.
Clock regression or invalid outputs become observable source failures. Replay
speed/selected time from #22 never controls generation.

The source owns the same worker and bounded queue as `IteratorSource`. Drain it
on the application owner thread with `TelemetrySession.ingest_source`, then query
and present snapshots. Live sessions require their own retention count cap.
Accelerated mode can outrun a small source queue; queue loss remains observable.
Signal scheduling uses one next item per signal, not a preallocated whole run.
Close must return true before claiming the worker has released its resources.

## Example families and expected readings

For `seed=17`, `start_ns=0`, `duration_ns=1000000000`, adequate queue capacity:

| Family and signal | Rate | Expected records and values |
| --- | --- | --- |
| process.pressure | 100 Hz | 101 attempts; 5 lost (indices 20–24), 5 stale (70–74), 91 emitted including 5 unavailable nulls (50–54). Cycle 0, 5, 8, 3 Pa. |
| process.temperature | 1 Hz | Two records at 0 and 1 second; cycle 20, 21, 19 °C with seeded ±0.1 noise. |
| process.status | 2 Hz | Three records; structured states idle, ready, idle. |
| motion.vector | 10 Hz | Eleven three-component vectors, alternating [0,1,2] and [3,2,1] m/s². |
| motion.speed | 3 Hz | Four records at exact floor-rounded offsets; cycle 0,4,8 rad/s with seeded ±0.2 noise. |
| motion.enabled | 1 Hz | Two records: false and true, both available. |
| motion.notice | 1 Hz | Two independent structured events, each with code tick. |

These are illustrative bench datasets, not selected hardware or required vehicle
channels. Callers may replace every descriptor and cycle with unrelated names,
units, types and rates without changing widgets or storage code.

```python
from lbr_dashboard.core import SimulatorSource, TelemetrySession, example_signals

source = SimulatorSource(example_signals("process"), seed=17, capacity=256)
session = TelemetrySession(source.schema, max_samples_per_signal=200)
source.open()
source.start()
assert source.join(2)
session.ingest_source(source)
view = session.snapshot()
assert len(view.series["process.pressure"]) == 91
assert len(view.series["process.temperature"]) == 2
assert view.nearest("process.pressure", 220_000_000, max_distance_ns=0) is None
missing = view.nearest("process.pressure", 500_000_000, max_distance_ns=0)
assert missing is not None and not missing.sample.available
assert source.simulator_stats.lost == 5
assert source.close()
```
