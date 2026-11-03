# Interval selection

An interval selection marks a time range of the loaded session for analysis.
Statistics, export, and every plot read the same selection object, so they
always agree on which samples are "in the interval".

The selection is separate from playback. The session clock (#22) owns the
current time and playback; selecting an interval never seeks or loops.

## Modules

| Module | Layer | Purpose |
| --- | --- | --- |
| `lbr_dashboard.core.interval` | `core` (stdlib only) | `TimeInterval`, `IntervalSelection` |
| `lbr_dashboard.ui.interval_region` | `ui` | `IntervalRegion` plot binding, `PlotTimeScale` |

## Rules

- **Units.** Endpoints are integer nanoseconds on the session time base. Floats
  and booleans are rejected with `TypeError`, so a value can never silently lose
  precision on the way in.
- **Closed intervals.** A sample at exactly `start_ns` or `end_ns` is inside.
  Two intervals that only touch at one instant overlap.
- **Zero width is valid.** `start_ns == end_ns` selects a single instant. This is
  also the result of clamping an interval that only touches the session range.
- **Reversed endpoints are swapped.** `select(30, 10)` stores `10..30`. The
  `TimeInterval` constructor itself rejects reversed endpoints; use
  `TimeInterval.between` to accept either order.
- **Bounds.** `IntervalSelection.bounds` is the session time range (or `None`
  when unknown). A selection that partly overlaps the bounds is clamped to them.
  A selection entirely outside raises `ValueError` and leaves the state and
  listeners untouched.
- **Endpoint edits.** `set_start(t)` behaves exactly like `select(t, end)` and
  `set_end(t)` like `select(start, t)`: an edit that crosses the other endpoint
  swaps them. Editing needs a current selection (`ValueError` otherwise).
- **Session changes.** `set_bounds(new)` re-clamps the current selection to the
  new range and clears it when there is no overlap. Widening the bounds later
  does not restore the old range.
- **Mixed sample rates.** An interval is a pure time range. Each signal is cut by
  its own timestamps with `interval.sample_slice(times_ns)` (non-decreasing
  times), so a 200 Hz and a 10 Hz signal share one interval without resampling.

## Notifications

```python
selection = IntervalSelection(bounds=TimeInterval(session_start_ns, session_end_ns))
unsubscribe = selection.subscribe(lambda interval: print(interval))
selection.select(session_start_ns, session_start_ns + 5_000_000_000)
unsubscribe()
```

- Listeners receive the new `TimeInterval | None`, in subscription order, and
  only when the stored value actually changes (re-selecting the same range, or a
  request that normalizes to it, is silent).
- A listener that raises does not stop the others. After all listeners ran, the
  selection raises `IntervalListenerError` with every failure in `errors`; the
  change itself stays applied.
- A listener may change the selection. That change is delivered to everyone
  after the current value finishes, so every listener sees values in order.
- The returned `unsubscribe` is safe to call more than once. A listener removed
  during a notification is not called afterwards.

## Plot binding

`IntervalRegion(plot_widget, selection, scale)` adds a `pg.LinearRegionItem` to
a `pg.PlotWidget`:

- The region is hidden while nothing is selected and follows every selection
  change.
- Dragging the region or one of its edges commits when the drag finishes. The
  committed range is clamped to the bounds and the region snaps to the stored
  value; a drag entirely outside the session is reverted.
- Region updates caused by the selection are never fed back into it, so there
  is no notification loop even when plot coordinates round.
- Escape clears the selection while the plot widget has keyboard focus. With
  nothing selected, Escape is passed on to the plot.
- `select_visible_range()` starts a selection from the visible x range, clamped
  to the bounds (`ValueError` when the view is entirely outside the session).
- `detach()` removes the region and stops following the selection. Deleting the
  plot widget unsubscribes automatically.

It takes a `pg.PlotWidget` rather than a bare `PlotItem` so that it can receive
the plot's key events.

### Plot x units and precision

`PlotTimeScale(ns_per_unit, origin_ns)` maps `x = (t - origin_ns) / ns_per_unit`.
`PlotTimeScale.seconds(origin)` and `PlotTimeScale.milliseconds(origin)` match
plots drawn in seconds or milliseconds since the session start.

Plot coordinates are float64. A round trip `to_ns(to_x(t))` is exact to the
nanosecond while `|t - origin_ns|` stays below about 2**50 ns (about 13 days).
Always set `origin_ns` to the session start: raw epoch timestamps (about
1.7e18 ns) with `origin_ns=0` lose up to a few hundred nanoseconds per edge.
Values set programmatically are stored exactly; only drags pass through float.

## Not yet included

- Wiring into the main window, menus, or toolbar (waits for the plot work in #38
  and the session model in #21).
- Taking `bounds` from the session time range (#21/#22).
- Looping and sample or time stepping inside the interval (tracked in #42, built
  only when needed).
