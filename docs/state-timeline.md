# State timelines

`lbr_dashboard.core.state_timeline` turns the samples of one boolean or
category signal (switch positions, button state, gear, status codes) into
state segments and sample events. `lbr_dashboard.ui.state_timeline_widget`
draws them on a time axis. In the app they are shown by the **Analysis >
State timeline** tool (`lbr_dashboard.ui.tools.state_timeline`, see below).

Nothing in either module knows about specific sensors: any signal is shown by
its generic signal id, with optional labels for its values.

## Inputs

```python
from lbr_dashboard.core.state_timeline import build_state_timeline

timeline = build_state_timeline(
    times_ns,  # integer nanoseconds, non-decreasing
    values,  # bool, int or str; None means a missing sample
    labels={0: "neutral", 1: "first", 2: "second"},  # optional
    max_hold_ns=500_000_000,  # optional
    data_start_ns=0,  # optional: span the recording covers
    data_end_ns=60_000_000_000,
    window_start_ns=10_000_000_000,  # optional: visible window
    window_end_ns=20_000_000_000,
)
```

`times_ns` must be non-decreasing. The window ends are found with bisect, so
only the samples needed for the window are visited; each visited pair is
checked and a decreasing time is an error. The inputs are never modified.

## Display policy

A sample's state is shown from its own timestamp until the next sample, or
until `max_hold_ns` has passed, whichever comes first. After that the timeline
shows **no data** until the next sample. State is never carried through an
unobserved period beyond that limit.

- Without `max_hold_ns` a state holds until the next sample. Use it whenever
  the signal is expected at a known rate, so a dropout shows as no data.
- The last sample holds until `data_end_ns` (bounded by `max_hold_ns`). With
  no `data_end_ns` it holds for `max_hold_ns`; with neither it has no known
  duration, so it gets no segment but still has an event marker.
- Time between `data_start_ns` and the first sample is no data. With no
  samples at all, the whole `data_start_ns .. data_end_ns` span is no data.

## Values and categories

| Sample value                       | Segment kind  | Label                        |
| ---------------------------------- | ------------- | ---------------------------- |
| `None` or NaN                      | `NO_DATA`     | `no data`                    |
| bool/int/str with no `labels`      | `STATE`       | `true`/`false`, `str(value)` |
| bool/int/str found in `labels`     | `STATE`       | the given label              |
| bool/int/str not found in `labels` | `UNKNOWN`     | `unknown: <raw value>`       |
| integral float such as `1.0`       | as int `1`    | as above                     |
| other float, bytes, list, ...      | `UNSUPPORTED` | `unsupported ...` with why   |

The raw value is kept in `segment.value` for unknown and unsupported values.
`True` and `1` are different categories: a `labels` key `True` does not match
a sample `1`. Integral floats are read as ints because CSV columns arrive as
floats; this is the only conversion.

## Segments and events

`StateSegment(start_ns, end_ns, kind, value, label, transitions, no_data_ns)`
covers `[start_ns, end_ns)`. Neighbouring samples with the same state are
joined into one segment. Segments are contiguous, and they are clipped to the
window.

Every input sample inside the closed window `[window_start_ns, window_end_ns]`
is also returned as a `StateEvent` at its exact time, with its input `index`.
`transition` is true when the sample's state differs from the state shown just
before it (the state before the first sample is no data).

`transition_count` is the number of boundaries between segments in the window,
including changes into and out of no data. `sample_count` is the number of
samples in the window.

## Duplicate timestamps

Samples with the same timestamp keep their input order. The last one sets the
state from that time on; the earlier ones have zero duration and get no
segment. All of them are returned as events, in input order.

## Bounded output

At most `max_segments` (default 1000) segments and `max_events` (default 2000)
events are returned, however many samples the window holds.

- Segments: a segment at least `2 * span / (max_segments - 1)` long is kept.
  Runs of shorter neighbours are combined until they reach that length and are
  returned as one `MIXED` segment whose `transitions` counts the changes inside
  it and whose `no_data_ns` says how much of it was no data. Its label reads
  `N changes` (plus `some no data` when relevant). `transition_count` always
  equals the hidden transitions plus the boundaries between returned segments.
- Events: the window is split into `max_events` equal time buckets and the
  samples in one bucket become one event with `count` samples, from `time_ns`
  to `last_time_ns`. If they do not all share one state its kind is `MIXED`.

`segments_bounded` and `events_bounded` say whether anything was combined.

## Widget

`StateTimelineWidget.show_timeline(signal_id, timeline, origin_ns=None)` draws
each segment as a bar on a seconds axis relative to `origin_ns` (default: the
window start, else the first segment or event). Each bar carries its label as
text, so nothing depends on color alone:

- known states: solid fill, a stable color per label
- no data: gray with diagonal hatching and the label `no data`
- unknown/unsupported: orange cross hatching and the reason as label
- mixed: light dotted fill and `N changes`

Sample events are drawn as markers on a row under the bars; transitions are
taller, combined events are diamonds. At most `MAX_LABELS` (200) of the widest
segments get a text label. A summary line under the plot gives the sample,
transition and segment counts and the hold policy.

`set_selected_time(time_ns)` shows a vertical cursor at that time and
`set_selected_time(None)` hides it. The cursor survives `show_timeline`.
`clear()` removes everything.

The widget draws with `segment_bars`, `segment_labels` and `event_markers`,
which the analysis tool below also uses to draw one row per signal.

## Status and event records

`category_value(value)` reads a flat record such as the simulator's status
`{"state": "idle"}` or event `{"code": "tick"}` as one category: the string
`state=idle` (`key=value` pairs in their original order, joined by `, `).
Equal records therefore form one state. Records with nested values, empty
records, lists and every other value are returned unchanged, so vectors and
nested records stay `UNSUPPORTED` with their reason as label.

## Analysis > State timeline

The tool opens as a dock from the **Analysis** menu and shows the loaded
session (CSV file or simulator), one row per signal:

- **Signals.** The list at the top shows every signal of the session by its
  generic signal id and type; boolean, status, string and event signals come
  first and the first 8 of them are ticked automatically. Tick or untick any
  signal to show it; at most 24 rows are drawn. A numeric signal can be ticked
  too: whole numbers (for example 0/1 digital channels from a CSV file) become
  states and fractional values are shown as `unsupported: float ...`.
- **Rows.** Boolean, status and string rows show state bars with labels and a
  tick per sample under them (taller ticks are changes). Event rows show only
  markers at the exact event times, labeled while at most 40 events are in
  view; no state is held between events.
- **Gaps.** Unavailable samples and time before a signal's first sample are
  hatched gray `no data`. Session time is the earliest to the latest reading
  over all signals, so the last sample of a signal holds until the session end.
- **Hold limit.** `Hold state at most` sets `max_hold_ns` for every row.
  `auto` (the default) uses 3 sample periods for signals that declare a
  nominal rate, and otherwise holds until the next sample. The summary under
  the plot states the limit of each row.
- **Time axis.** The x axis is seconds since the main plot's origin (the first
  reading of the signal plotted there) while the main plot shows a time trace
  of the same session, else seconds since the session start. The summary
  under the plot says which one is in use.
- **Zoom.** `Follow main plot zoom` (on by default) keeps the x range equal to
  the main plot's in both directions while both use the same origin. `Fit
  session` shows the whole session.
- **Replay and interval.** A vertical line marks the shared replay cursor
  (`AnalysisContext.cursor_ns`), and the shared interval selection is drawn as
  a draggable region; dragging it changes the interval for every tool.
- **Bounded.** After every pan, zoom or new snapshot (coalesced to one refresh
  per 100 ms) each row is rebuilt from `SessionSnapshot.range(..., neighbors=True)`
  for the visible range only, with at most 500 segments and 1000 events per
  row, so drawing stays bounded however long the session is.

`configuration()` returns `{"signals": [...] or None, "max_hold_s": float or
None, "follow_main_plot": bool}` (`None` signals means the automatic choice,
`None` hold means `auto`). `restore_configuration(config)` applies it and
raises `ValueError` for anything else. Ticked ids that the current session
does not have are kept and listed under the plot as not in this session.
