# Source health, freshness and observable loss

`lbr_dashboard.core.source_health` tracks whether a telemetry source is
connected, whether its data is current, and how much loss the dashboard can
actually observe (issue #44). It is standard library only, has no I/O and never
reads the wall clock. `lbr_dashboard.core.source_monitor.SourceHealthMonitor`
feeds it from a running source, and
`lbr_dashboard.ui.source_health_panel.SourceHealthPanel` is a text readout of
one snapshot.

## In the app

- **Source picker**: a `Source health:` line beside the status line always shows
  the headline state, the arrival age of the newest data (rounded to 0.1 s),
  the packet rate once a full window has passed, and the observable loss
  (`loss unknown (no sequence numbers)` when the source has none). Dashboard
  drops and decode errors are added when they are not zero. The label carries
  a `healthStatus` property (`live`, `late`, `stale`, `waiting for data`,
  `disconnected`, `unknown`) for style sheets; the words alone tell the states
  apart.
- **Analysis > Source health** opens a dock with every row of
  `format_source_health`, followed by one `Signal <id>` row per signal with its
  record count, unavailable readings and last arrival age judged with the same
  thresholds. It repaints at most every 200 ms.

The source controller creates one monitor per started source, hands it every
batch it drains (`TelemetrySession.ingest_source(..., observer=...)`) and
publishes `monitor.report()` on every poll as
`AnalysisContext.source_health` (signal `source_health_changed`). The report
of the last source stays visible after a disconnect until another source
starts; a legacy table import clears it.

## Feeding the tracker from a source

`SourceHealthMonitor(source, clock=...)` maps the source contract onto tracker
observations:

| Contract | Tracker input |
| --- | --- |
| `source.state` OPEN / RUNNING | `observe_connect()` on the change |
| `source.state` STOPPING / STOPPED / CLOSED | `observe_disconnect(reason="stopped")` |
| `source.state` ENDED | `observe_disconnect(reason="end of data")` (or the terminal reason) |
| `source.state` FAILED | `observe_disconnect(reason="failed: <last error>")` |
| RECORD event | `observe_packet(...)`, timed when the batch is handed over |
| HEALTH event | `observe_reported_status(...)` with the contract fields; also `observe_heartbeat()` when declared |
| ERROR event with code `decode_error` | `observe_decode_error(...)` when declared |
| growth of `source.events.stats.dropped_records` while connected | `observe_overflow(dropped)` |

CREATED and OPENING leave the connection as it was. Records the bounded queue
rejects after the source was stopped are not counted as drops.
`SourceHealth.dropped_records` is what the source reports (for the simulator
it includes scheduled loss) and is shown under `Reported: dropped_records`,
never merged into the dashboard's own counters. Session retention (the live
1,000 readings per signal limit) is not loss and is not counted.

A source declares the health metadata it provides with an optional
`health_config` attribute holding a `SourceHealthConfig`. Without it, sequence
numbers, heartbeat and decode errors are unknown. Dashboard drops are always
known, because every source has a bounded queue. The live simulator declares a
heartbeat with a timeout of twice its 1 s health interval; the finite simulators
and CSV replay declare none.

A source that declares `sequence_bits` puts each packet's number in
`SignalSample.extra["sequence"]`. Consecutive records of different signals with
the same number are one packet; the same signal again with the same number
starts a new packet (a duplicate). Records without a number, or with one that
does not fit the declared bits, count for freshness and rate only.

The monitor does not feed device time: per-signal measurement times of
multi-signal sources (for example CSV columns) are not in arrival order, so
the `Device time` row stays unknown. Measurement time is shown by the replay
clock and the plots instead.

## Tracker observations

The code that owns a source creates one `SourceHealthTracker` per source and
feeds it plain observations from the thread that owns the source:

| Call | Meaning |
| --- | --- |
| `observe_connect()` | link established or re-established |
| `observe_disconnect(reason=...)` | link lost, with an optional reason |
| `observe_packet(sequence=..., device_time_ns=...)` | one valid, decoded packet |
| `observe_decode_error(message=...)` | bytes arrived but did not decode |
| `observe_heartbeat()` | the source said it is alive |
| `observe_overflow(dropped)` | the dashboard dropped records itself (full queue) |
| `observe_reported_status(fields)` | status fields sent by the firmware |

Each call takes its arrival time from the tracker's monotonic clock
(`time.monotonic_ns` by default) unless `arrival_ns` is passed. Arrival times
must never go backwards. Tests inject a fake clock; nothing sleeps.

`snapshot()` returns a frozen `SourceHealthSnapshot` that is safe to hand to
another thread, the UI, or session consumers (#71). Counters are cumulative
for the life of the tracker and are not cleared by reconnects.
`format_source_health(snapshot)` returns ASCII `(label, value)` rows and
`status_text(snapshot)` the headline.

```python
from lbr_dashboard.core.source_health import SourceHealthConfig, SourceHealthTracker

tracker = SourceHealthTracker("usb0", SourceHealthConfig(sequence_bits=16))
tracker.observe_connect()
tracker.observe_packet(sequence=41)
tracker.observe_packet(sequence=43)
snapshot = tracker.snapshot()
assert snapshot.sequence is not None and snapshot.sequence.lost == 1
```

## Declared metadata and "unknown"

Not every source has sequence numbers or a heartbeat. `SourceHealthConfig`
declares what the source provides, and anything not declared is `None` in the
snapshot and `unknown (...)` in the text. It is never shown as `0`.

| Config | Default | When absent |
| --- | --- | --- |
| `sequence_bits` | `None` | lost, duplicates, out of order, rollovers, resets and suspected resets are unknown |
| `heartbeat_timeout_ns` | `None` | heartbeat state is `unknown` |
| `reports_decode_errors` | `True` | decode errors are unknown |
| `reports_overflow` | `False` | dashboard drops are unknown |

Feeding undeclared metadata (a sequence number without `sequence_bits`, a
heartbeat without a timeout) raises `ValueError`, so a counter can never
appear to be supported by accident. Device time and firmware-reported status
are `None` until the source sends them. A source with no connection events
shows connection `unknown`; packets alone do not imply a connection.

## Freshness

Freshness is judged on the **arrival age** of the newest valid packet since
the latest connect, using the monotonic clock. Device timestamps describe
when a measurement was taken; they never decide freshness.

| Age | Freshness | Text |
| --- | --- | --- |
| `age <= warn_after_ns` | `FRESH` | fresh |
| `warn_after_ns < age <= stale_after_ns` | `WARN` | late |
| `age > stale_after_ns` | `STALE` | stale |
| no packet since connect | `NO_DATA` | no data |

- Defaults: `warn_after_ns = 500 ms`, `stale_after_ns = 2 s`. Both must be
  positive integers and `stale_after_ns` must be greater than `warn_after_ns`.
- An age exactly equal to a threshold is still on the better side.
- Connected but silent for longer than `stale_after_ns` without any packet is
  `STALE`, not "waiting".
- Decode errors, heartbeats and reported status do not refresh freshness;
  only valid packets do.

## Headline status

`snapshot.status` combines connection and freshness, and the text always names
the state in words so it does not depend on color:

| Connection | Freshness | Status | Text |
| --- | --- | --- | --- |
| disconnected | any | `DISCONNECTED` | `Disconnected (reason)` |
| connected | stale | `STALE` | `Connected but stale` |
| unknown | stale | `STALE` | `Stale (connection unknown)` |
| connected / unknown | late | `LATE` | `Connected, data late` / `Data late (connection unknown)` |
| connected / unknown | no data | `WAITING` | `Connected, waiting for data` / `Waiting for data` |
| connected / unknown | fresh | `LIVE` | `Connected, live` / `Live (connection unknown)` |

Connected-but-stale (the link is up, nothing is arriving) is therefore always
distinguishable from disconnected (the link is down).

## Packet rate

`rate_hz` counts valid packets that arrived in the window
`(now - rate_window_ns, now]` (default 1 s) and divides by the window length.
It is `None` ("measuring") until a full window has passed since the latest
connect, or since the first packet when the source has no connection events,
so a short burst right after connect is not reported as a high rate. After
data stops, the rate falls to 0.

## Sequence numbers

With `sequence_bits` declared, sequence numbers wrap at `2**sequence_bits`
(2 to 64 bits). `SequenceTracker` classifies each number with serial-number
arithmetic (RFC 1982) against the newest number of the current run:

| Step relative to newest | Classification | Effect |
| --- | --- | --- |
| `+1` | in order | none |
| `+2` up to less than half the range | gap | skipped numbers count as lost |
| `0` | duplicate | duplicates + 1 |
| `-1` to `-reorder_window`, number was counted lost | out of order | out of order + 1, lost - 1 |
| `-1` to `-reorder_window`, number is older than the run | out of order | out of order + 1 |
| `-1` to `-reorder_window`, number already received | duplicate | duplicates + 1 |
| further back than `reorder_window`, or exactly half the range | reset | resets + 1, a new run starts |
| back to an already received `n`, then `n + 1` also already received | suspected reset | duplicates + 1 for each, suspected resets + 1, the run continues |

- **Rollover**: a forward step that wraps past the top of the range (for
  example 255 -> 0 for 8 bits) is a rollover, counted in `rollovers`, and is
  not loss. Gaps across a rollover (255 -> 2) count the skipped numbers. The
  same step 255 -> 0 for a 16-bit counter is a step back, so it is a reset.
- **Reset**: a large step backward means the counter restarted (for example
  a firmware reboot). A new run begins; nothing is counted as lost because
  loss across a restart cannot be measured.
- **Reconnect**: `observe_connect()` starts a new run at the next number. It
  is counted as a reset when that number is not ahead of the newest number
  seen before the reconnect (for example it went back to 0). Numbers skipped
  while disconnected are not counted as transport loss; the disconnect itself
  is the evidence.
- **Reorder window**: defaults to `min(32, 2**bits / 4)` and must be smaller
  than half the range. Only numbers within the window can be taken back as
  late arrivals; older lost numbers stay lost.
- **Suspected reset (replay within the window)**: a device that restarts
  without a disconnect event shortly after start (or just after a rollover)
  steps back by less than the reorder window, to numbers already received.
  A transport that delivers the same packets twice looks exactly the same,
  so this is not treated as a confirmed reset. When a packet steps back to an
  already received `n` and the very next packet is `n + 1`, also already
  received and behind the newest, the pair is a suspected reset: both
  packets count as duplicates, `suspected_resets` grows by one, and the
  current run is kept. Further packets that keep stepping forward through
  received numbers are part of the same replay and do not add another
  suspected reset. Because the run is kept, numbers received before the
  replay are never counted as lost afterwards. For example `10, 11, 12, 10,
  11, 13` is 2 duplicates, 1 suspected reset, 0 resets and 0 lost, and
  `10, 11, 12, 10, 11, 15` loses only 13 and 14. A lone repeat, a repeated
  pair `(n, n)`, a repeat of the newest number, and late packets that fill
  counted losses (for example `1, 4, 2, 3`) keep their classification.
  The `Sequence` row lists suspected resets next to resets when there are
  any.
- **Confirmed resets**: `resets` counts only steps that cannot be duplicated
  delivery: a step back further than the reorder window (or exactly half the
  range), and a step back after a reconnect. Loss is counted only against
  numbers that were never received, so a suspected reset never adds loss.
- **Known ambiguity**: a device that really restarted within the window is
  reported as a suspected reset with its replayed packets as duplicates, and
  a packet it then fails to send is not counted as lost (for example a
  restart that sends `10, 11, 13` after `12` was received before the
  restart). A restart that sends only one number before jumping ahead again
  reads as a duplicate. A forward jump of less than half the range is always
  counted as loss, even if it was really a restart. Sources that can say they
  restarted should report a disconnect/connect pair, which makes the reset
  confirmed.
- `loss_fraction = lost / (lost + packets - duplicates)`.

Packets may omit the sequence number even when `sequence_bits` is declared;
they count for freshness and rate only.

The same `SequenceTracker` can classify recorded counters for the
data-quality summary (#71), so both use one rollover and reset rule.

## Other counters

- **Decode errors**: bytes that arrived but did not decode. They are counted
  separately from loss: a corrupt packet is not a sequence gap by itself, and
  if it carried a sequence number that number will appear as lost.
- **Dashboard drops** (`overflow_dropped`): records the dashboard discarded
  itself, for example a full event queue. This is not transport loss and is
  never merged with `sequence.lost`. Intentional downsampling for display is
  not loss and is not reported here at all.
- **Heartbeat**: `ALIVE` while the time since the latest heartbeat (or the
  latest connect, before the first heartbeat) is at most
  `heartbeat_timeout_ns`, `TIMED_OUT` after that, `WAITING` before the first
  heartbeat. `heartbeat_timeouts` counts gaps longer than the timeout that
  ended with a heartbeat (recoveries). A reconnect restarts the timing, so an
  outage is reported as a disconnect rather than as a heartbeat timeout.
- **Device time**: the device timestamp of the newest packet that carried one,
  and `device_time_regressions`, the number of times it went backwards within
  one connection.

## Firmware-reported status

`observe_reported_status(fields)` stores a complete status message (text,
number or boolean values) exactly as reported, replacing the previous one.
It is shown in rows labelled `Reported: <name>` with its arrival age and is
never mixed into inferred counters: a reported `sd_dropped` does not change
`overflow_dropped` or `sequence.lost`.

## Not covered yet

- A hardware smoke test with a real serial adapter (#33); so far only the
  simulators, CSV replay and fake sources drive the monitor.
- Radio-specific metrics and the historical report view (#71).
