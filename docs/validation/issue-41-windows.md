# Issue #41 Windows validation

Validated locally on Windows using Python 3.12.14, PySide6 6.11.2 and locked
project dependencies.

- Full offscreen suite: **692 passed**, **98.20%** branch coverage.
- Native Windows replay, browser and source-picker checks: **50 passed**.
- Ruff lint, Ruff formatting, Pyright and dependency compatibility pass.
- Wheel and source archive build, install outside the checkout, and pass
  application startup, sample CSV, plot, resource and entry-point checks.

Production replay widgets are exercised with an injected monotonic clock:
play/pause, held time, changing speed, exact integer seek arithmetic, end/restart,
empty and single-sample sessions, live pause/follow/disconnect, source changes,
and text-entry/navigation protection. Original session measurements remain
unchanged. Tests verify the time marker against the clock, preserve selection
when switching signals, and prevent cursor alignment across distinct clock
domains or structured nonnumeric signals.

The ordinary seek workflow test is also run with the real seek signal
disconnected. It must fail because the slider no longer updates SessionClock.

The final native run completes without the COM exception report previously
observed in native Qt tests on this machine. Earlier test assertion/setup errors
were corrected before these final runs; no failing test is excluded.

For manual review, use the [replay walkthrough](../replay-controls.md). The
review app opens a finite process simulator paused at the start. Play/Pause,
the slider, speed and keyboard commands belong to the **Session replay** row.
The source adapter's separate seek/rate capability buttons remain disabled
because cached-session replay does not call unsupported adapter operations.
