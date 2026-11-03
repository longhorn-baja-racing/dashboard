# XY plot pairing

An XY plot draws one numeric signal against another (for example CVT
temperature against wheel speed). The two signals are usually sampled at
different times and rates, so every plotted point is a *pair* chosen by an
explicit, documented rule. The pairing kernel is `lbr_dashboard.core.xy_pairing`
(standard library only); the scatter widget is `lbr_dashboard.ui.xy_plot`.

## Inputs

Each signal is two parallel sequences, the same arrays an importer produces:

- `times_ns`: integer nanoseconds, non-decreasing (equal values are allowed).
- `values`: floats where `None`, NaN and +/-inf all mean "missing".

Mismatched lengths, decreasing timestamps, a negative tolerance or
`start_ns > end_ns` raise `ValueError`.

## Driver signal

One signal is the driver, X by default. Each driver sample produces at most one
pair; the other signal is looked up at the driver timestamp. Driving by X fits
how an XY plot is read ("Y as a function of X") and keeps every X reading at
most once. A sample of the other signal may pair with several driver samples
when the driver is sampled faster.

Pass `driver="y"` to drive by Y instead, for example when Y is the slower
signal and repeating its samples would be misleading. The result still has X
on `x` and Y on `y`, and `PairedSeries.driver` records the choice.

## Alignment policies

`tolerance_ns` is the largest allowed time difference. A difference exactly
equal to `tolerance_ns` is accepted.

- `NEAREST`: the other-signal sample nearest in time to the driver sample.
  Ties pick the earlier sample. It may be after the driver time.
- `PREVIOUS`: the latest other-signal sample at or before the driver time
  (sample-and-hold). It never looks ahead, and a held value older than
  `tolerance_ns` is not used.

Example, other signal at t=0 (1.0) and t=10 (2.0), `tolerance_ns=5`:

| Driver time | `NEAREST` | `PREVIOUS` |
| --- | --- | --- |
| 4 | 1.0 | 1.0 |
| 5 | 1.0 (tie -> earlier) | 1.0 |
| 8 | 2.0 | excluded (held value 8 ns old) |
| 16 | excluded (6 ns away) | excluded (6 ns old) |

When several samples share one timestamp, the last one in input order is used.

There is no interpolation and no resampling. Linear interpolation could be
added later as a separate policy.

## Missing data

A pair is never fabricated. A driver sample that cannot be paired is excluded
and counted once, by the first reason that applies:

1. `x_missing`: the X value is missing.
2. `y_missing`: the selected Y value is missing. If the nearest (or held)
   sample is missing, the pair is excluded; the kernel does not skip to a
   different sample.
3. `no_match`: the other signal has no sample within `tolerance_ns`.

With `driver="y"` the same names apply to the X and Y values. The counts are
in `PairedSeries.excluded` and the widget shows them as text.

## Interval

`start_ns` and `end_ns` select driver samples with `start_ns <= t <= end_ns`
(closed interval; `None` leaves a side open). The interval applies to the
driver only: the other signal may be matched up to `tolerance_ns` outside it,
so a sample-and-hold value set just before the interval still applies at its
start. Exclusion counts only cover driver samples inside the interval.

## Units

`x_unit` and `y_unit` are carried on the `PairedSeries` unchanged. The widget
labels axes as `name (unit)`, or just `name` when the unit is unknown. Units
are not converted or rescaled.

## Point limit

`limit_points(series, max_points)` keeps at most `max_points` evenly spaced
pairs by index, always including the first and last, and returns how many were
dropped. The result depends only on the input. The widget applies it with a
default of 20000 points, so large intervals stay responsive; the note shows
"Showing N of M pairs". Dropped pairs are valid pairs that are just not drawn,
so isolated outliers can be hidden at high point counts.

## Widget

`XYPlotWidget.show_pairs(series, x_label, y_label)` draws the scatter, labels
the axes and writes a text note with the policy, tolerance, driver, point count
and exclusion counts. Pan and zoom are the PyQtGraph defaults (drag, wheel,
right-click menu). `highlight_time(t_ns)` marks the pair whose driver time is
nearest `t_ns` (ties pick the earlier pair), searching every pair including
ones not drawn, and returns that driver time; `None` or an empty series hides
the marker.

The widget is not yet part of the main window. Signal selection, a selected
interval and saving the configuration depend on the shared signal model and
widget configuration work.

## Running the tests

On Windows (PowerShell) or any other platform, from the repository root:

```
uv run --locked --extra dev pytest tests/core/test_xy_pairing.py tests/ui/test_xy_plot.py
```
