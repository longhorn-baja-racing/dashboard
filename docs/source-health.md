# Source health, freshness and observable loss

`lbr_dashboard.core.source_health` tracks whether a telemetry source is
connected, whether its data is current, and how much loss the dashboard can
actually observe (issue #44). It is standard library only, has no I/O and never
reads the wall clock. `lbr_dashboard.ui.source_health_panel.SourceHealthPanel`
is a text readout of one snapshot. Neither is wired into the main window yet.

## Feeding the tracker

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
| `sequence_bits` | `None` | lost, duplicates, out of order, rollovers and resets are unknown |
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
| back to an already received `n`, then `n + 1` also already received | reset at `n` | duplicate for `n` taken back, resets + 1, a new run starts at `n` |

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
- **Restart within the window**: a device that restarts without a disconnect
  event shortly after start (or just after a rollover) steps back by less
  than the reorder window, to numbers already received. The first replayed
  number is provisionally a duplicate. When the very next packet is its
  successor and is also an already received number behind the newest, the
  pair is a restarted counter: the duplicate is taken back and a reset starts
  a new run at the first replayed number. A lone repeat, a repeated pair
  `(n, n)`, a repeat of the newest number, and late packets that fill counted
  losses (for example `1, 4, 2, 3`) keep their classification.
- **Known ambiguity**: a restart that sends only one number before jumping
  ahead again still reads as a duplicate. A forward jump of less than half the range is always counted
  as loss, even if it was really a restart. Sources that can say they
  restarted should report a disconnect/connect pair.
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

## Mapping to the source contract (#20)

The planned `SourceHealth` / `SourceEvent` contract can feed the tracker
without changing this module:

| Contract | Tracker input |
| --- | --- |
| `SourceState` OPEN / RUNNING | `observe_connect()` |
| `SourceState` ENDED / STOPPED / CLOSED / FAILED, `terminal_reason` | `observe_disconnect(reason=...)` |
| RECORD event | `observe_packet(...)` |
| ERROR event for a decode failure | `observe_decode_error(message=...)` |
| `SourceHealth.dropped_records`, queue `dropped_records` (as increments) | `observe_overflow(dropped)` |
| HEALTH event fields from the firmware | `observe_reported_status(...)` |

## Not covered yet

- Wiring a live adapter and the panel into the main window.
- Radio-specific metrics and the historical report view (#71).
