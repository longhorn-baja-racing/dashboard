# Issue #22 validation

Validated on Windows with Python 3.12.14 and Qt 6.11.2, October 28, 2026.
Branch `codex/p0-22-session-clock` builds on issue #21 commit `ab41b4e`.

## Acceptance evidence

- One application-owned `SessionClock` publishes the same immutable frame object
  and selected time to every subscriber. Widgets do not own playback timers.
- Injected monotonic time tests cover play, pause, resume, speed changes, backwards
  seek, empty/single-point bounds, end clamping and retained live bounds.
- Rational arithmetic retains fractional nanoseconds across 3,000 fake ticks;
  selected integer time has less than one nanosecond floor quantization error.
- Live-follow uses the latest recorded end, holds while disconnected and catches
  up on reconnect only when follow intent remains active. Seek/pause detach it.
- Session snapshot binding identifies one reference signal/source/time domain.
  Gaps do not cause interpolation, sample snapping, fabricated zeros or changes
  to original timestamps, units, quality and unavailable values.
- Subscriber exceptions still allow delivery to remaining subscribers; reentrant
  commands and controls from other threads are rejected. Unsubscription is
  idempotent, and snapshots never advance time merely by being read.

## Results

The full strict suite passed **525 tests**, with **98.24%** overall branch-aware
coverage. All new clock statements are covered; clock coverage rounds to 99%.
Ruff lint/format, Pyright and the documented example passed.

Wheel and source distribution were installed outside the checkout. Shared-clock,
session/source/schema checks, application resources/entry point, CSV plotting
and installed dependency compatibility passed for both distributions.

The native Windows two-view harness passed play/pause/rate/end, backwards seek,
zero/missing/gap distinctions, identical subscriber frames, live disconnect and
reconnect. Both plots use the same visible reference-time range and seconds axes
with automatic SI prefixes disabled. Temporary interactive controls remain
outside the production package; production replay/cursor integration is #40/#41.

Five isolated mutations produced targeted assertion failures: ignoring speed,
rounding movement every tick, removing end clamping, disabling reconnect catch-up,
and giving subscribers different selected times. Production files were unchanged
by these mutation runs.

## Reproduction

```powershell
uv sync --locked --extra dev
python -m ruff check .
python -m ruff format --check .
python -m pyright
$env:QT_QPA_PLATFORM='windows'
python -m pytest --cov=lbr_dashboard --cov-branch --cov-report=term-missing
uv build --no-sources
python scripts/check_package.py
```

Use a supported offscreen Qt backend for headless CI. Command semantics,
rounding, bounds, subscriber and reconnect policies are in `docs/session-clock.md`.
