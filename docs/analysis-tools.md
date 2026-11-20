# Analysis tools

Analysis tools are dockable views over one shared state. They live in
`lbr_dashboard.ui.tools` and open from the **Analysis** menu.

## Shared state

`lbr_dashboard.ui.analysis.AnalysisContext` is owned by the main window. The
source controller and replay controller keep it current:

| Attribute | Meaning | Change signal |
| --- | --- | --- |
| `snapshot` | the latest `SessionSnapshot` of the loaded session, or None | `snapshot_changed` |
| `selected_id` | the signal selected in the signal browser | `signal_changed` |
| `cursor_ns` | the replay cursor time (session clock) | `cursor_changed` |
| `selection` / `interval` | the one session-level `IntervalSelection` | `interval_changed` |
| `bounds` | earliest to latest measurement time over all signals | `snapshot_changed` |
| `log_path` | the file the session was read from, or None (simulators) | `snapshot_changed` |
| `plot` / `plot_origin_ns` | the main time plot and its x origin (x is seconds since it) | `plot_changed` |
| `plot_signal_id` | the signal the main time plot shows | `plot_changed` |
| `following_live` | true while the replay bar follows live data | `cursor_changed` |

Helpers:

- `numeric_series(signal_id, interval=None)` returns `(times_ns, values)` for a
  numeric signal. Unavailable readings are `None`, never zero. With an interval
  only the records inside the closed range are returned.
- `reading_at(signal_id, time_ns)` returns the last reading at or before a time.
- `request_seek(time_ns)` asks the replay clock to pause and move its cursor to
  a session time; `cursor_ns` changes when it did.
- `plot_changed` is emitted whenever the main plot is attached again, including
  after it was cleared, so a tool can redraw its items on it.
- `style_plot(plot_widget)` colors a tool's PyQtGraph plot for the current theme
  and again after every theme change, until the plot is deleted.

A legacy table import clears the context, so no tool keeps showing a session
that is no longer loaded.

## Adding a tool

1. Write a widget that takes the context and listens to the change signals it
   needs. Coalesce refreshes with a short single-shot timer when the work is
   more than trivial, because snapshots arrive often while a source streams.
2. Export `TOOL = AnalysisTool("key", "Menu label", WidgetClass)` from a module
   in `lbr_dashboard/ui/tools/`. Every such module is found automatically and
   listed alphabetically by label in the Analysis menu; keys must be unique.

The dock is created the first time the menu entry is used and reused after that.

## Tools

Each tool module in `lbr_dashboard/ui/tools/` and its doc page describe what
the tool shows; the Analysis menu lists them all.
