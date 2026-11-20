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
| `source_health` | the latest `SourceMonitorReport` of the active (or last) source, or None | `source_health_changed` |

Helpers:

- `numeric_series(signal_id, interval=None)` returns `(times_ns, values)` for a
  numeric signal. Unavailable readings are `None`, never zero. With an interval
  only the records inside the closed range are returned.
- `reading_at(signal_id, time_ns)` returns the last reading at or before a time.
- `style_plot(plot_widget)` colors a tool's PyQtGraph plot for the current theme
  and again after every theme change, until the plot is deleted.

A legacy table import clears the context, so no tool keeps showing a session
that is no longer loaded.

## Adding a tool

1. Write a widget that takes the context and listens to the change signals it
   needs. Coalesce refreshes with a short single-shot timer when the work is
   more than trivial, because snapshots arrive often while a source streams.
2. Export `TOOL = AnalysisTool("key", "Menu label", WidgetClass)` from a module
   in `lbr_dashboard/ui/tools/`.
3. Add it to `TOOLS` in `lbr_dashboard/ui/tools/__init__.py`.

The dock is created the first time the menu entry is used and reused after that.

## Tools

| Menu entry | Module | Issue |
| --- | --- | --- |
| Statistics | `ui/tools/statistics.py` | #43 |
| Source health | `ui/tools/source_health.py` | #44 |
