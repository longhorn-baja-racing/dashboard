# Filters

`lbr_dashboard.core.filters` turns one numeric signal into a smoothed copy.
`lbr_dashboard.ui.filter_overlay` draws filtered curves on a PyQtGraph plot, and
**Analysis > Filters** (`lbr_dashboard.ui.tools.filters`) overlays a moving
average of the selected signal on the main time plot.

Only a moving average exists. Low-pass, high-pass and band-pass filters are
deferred: no one has asked for a specific one yet, and they need a vetted DSP
dependency (for example SciPy), which the project does not have. They will be
added as separate named filters with their own rules when a concrete analysis
needs them.

## In the app

1. Load a session (a timestamped CSV or a simulator from the source picker)
   and select a numeric signal in the signal browser.
2. Open **Analysis > Filters**.
3. Choose the window (samples), the alignment (centered or trailing), the
   jitter tolerance (percent of the nominal period) and, optionally, a time gap
   that splits the signal into segments.
4. Click **Overlay on main plot**.

The filtered trace is drawn as a thick orange line on the main time plot, on
the same axes as the raw trace (seconds since the plot's first reading). The
raw trace is not changed or hidden. A legend in the plot names both traces;
the filtered one carries every parameter, for example
`pressure [Pa] moving average, 5 samples centered (50 ms)` or, for trailing
windows, `..., delay 20 ms`. Below the controls the tool shows the sample
counts (output, missing, edge, overflow), the nominal period, the largest
spacing deviation and the window span.

When the sampling or the parameters are unsuitable nothing is drawn and the
tool shows the reason, for example
`Not filtered: sample spacing 60000000 ns between samples 19 and 20 differs
from the nominal period 1e+07 ns by more than 0.1 of it ...`. Setting a time
gap, raising the tolerance or choosing another window resolves it.

The overlay is removed when another signal is selected, when the main plot is
reset (for example a new session or a legacy table import), or with **Remove
overlay**. While a source streams, the overlay is recomputed with the same
parameters as new readings arrive (at most every 100 ms); if the new data can
no longer be filtered, the overlay is removed and the reason shown.

`FilterTool.configuration()` returns the filter name, its parameters and the
signal whose overlay is shown (or None). `restore_configuration(config)`
validates the parameters with the same rules as `moving_average` (raising
`ValueError` and changing nothing when they are invalid), sets the controls and
re-draws the overlay when that signal is selected.

## Inputs

```python
from lbr_dashboard.core.filters import moving_average

smoothed = moving_average(
    times_ns,  # integer nanoseconds, strictly increasing
    values,  # floats; None, NaN or +/-inf means "no usable sample"
    window_samples=5,
    alignment="centered",  # or "trailing"
    jitter_tolerance=0.1,  # optional, relative to the nominal period
    gap_ns=50_000_000,  # optional, spacings this long split the signal
    signal="engine.rpm",  # optional, carried through
    unit="rpm",  # optional, carried through; the output has the same unit
)
```

The inputs are the same parallel arrays an importer produces (`array("q")`
times and `array("d")` values) or plain lists. They are copied; the raw input is
never modified, and filtering never replaces the raw signal.

## Window

The window is a number of samples, `window_samples`, and every sample in it
has the same weight. It is not a duration. Its time span is reported as
`window_span_ns = window_samples * nominal_period_ns`.

- `alignment="centered"`: the output at sample `i` is the mean of samples
  `i - h .. i + h` with `h = (window_samples - 1) / 2`. The window must be odd.
  There is no delay (zero phase): a straight line comes out unchanged.
- `alignment="trailing"`: the output at sample `i` is the mean of samples
  `i - window_samples + 1 .. i`. It only uses past samples, so it could run on
  live data, but it lags the input by `(window_samples - 1) / 2` periods.
  `delay_ns` reports this lag.

`window_samples=1` returns the valid input values unchanged.

## Sample-rate requirement

A window counted in samples only means the same time span everywhere if the
sampling is regular. The nominal period is the median spacing between
consecutive timestamps (spacings of at least `gap_ns` are left out). Every
other spacing must be within `jitter_tolerance` (default 0.1, that is +/-10%)
of it, or `IrregularSamplingError` (a `ValueError`) is raised naming the first
bad spacing. A deviation exactly equal to the tolerance is accepted. Nothing is
resampled or interpolated. Spacings within the tolerance are accepted as they
are; samples still get equal weight. The result reports how regular the
accepted sampling was as `max_spacing_deviation`: the largest
`|spacing - nominal_period_ns| / nominal_period_ns` over the checked spacings
(0.0 for perfectly regular timestamps), so a result with 9% jitter is not
mistaken for a perfectly regular one.

Timestamps must be integers inside the signed 64-bit range and strictly
increasing; duplicates, backwards steps and values that do not fit in int64
(including unsigned values above 2**63 - 1) are errors, never wrapped.

## Gaps

A sample whose value is `None`, NaN or +/-inf is missing. It never enters an
average, and it is never treated as zero. It splits the signal into
segments, and a window never reaches across a split.

A spacing of at least `gap_ns` (when given) is a time gap: it also splits the
signal and is not checked for jitter. Without `gap_ns`, a dropout (for example
a spacing of three periods) is an irregular spacing and is rejected. `gap_ns`
must be longer than the nominal period plus its tolerance. A spacing exactly
equal to `gap_ns` is a gap. If every spacing is a gap there is no nominal
period: `nominal_period_ns`, `max_spacing_deviation`, `window_span_ns` and
`delay_ns` are `None`, and every sample is its own segment.

## Edges

No partial windows. A sample gets an output only when its whole window lies
inside its segment. Otherwise its output is missing:

- centered: the first and last `h` samples of each segment,
- trailing: the first `window_samples - 1` samples of each segment,
- every sample of a segment shorter than the window.

Shrinking the window at edges is not done, because it would silently change the
filter (and, for centered, its symmetry) near every gap.

## Overflow

Finite values near the float limit (about 1.8e308) can overflow while a window
is summed. Such windows are summed again with each value divided by the window
length first, so `[1e308, 1e308, 1e308]` averages to 1e308. If even that mean
is not a finite float, that sample gets no output (NaN) and is counted in
`overflow_count`; an infinite value is never emitted or counted as output.

## Result

`FilteredSignal` is frozen and carries:

| Field | Meaning |
| --- | --- |
| `times_ns` | read-only copy of the input timestamps |
| `values` | read-only float array aligned with `times_ns`, NaN where there is no output |
| `values_or_none()` | the same values as a list with `None` instead of NaN |
| `filter_name`, `version` | `"moving_average"` and the algorithm version (2) |
| `params` | `MovingAverageParams` (window, alignment, tolerance, gap) |
| `input_count`, `missing_count`, `edge_count`, `overflow_count`, `output_count` | sample counts; output = input - missing - edge - overflow |
| `nominal_period_ns`, `window_span_ns`, `delay_ns` | timing; `None` without a nominal period (fewer than two samples, or only gaps), for both alignments |
| `max_spacing_deviation` | largest relative deviation of a checked spacing from the nominal period, or `None` |
| `segment_count` | number of runs of valid, gap-free samples |
| `signal`, `unit` | passed through unchanged |

`provenance()` returns a plain dict with the filter name, version, parameters,
input count, signal and unit. `moving_average(times, values,
**result.params.as_dict())` reproduces the same output.

## Accuracy

Each output is the sum of its window divided by `window_samples` (a numpy
convolution with a window of ones). It matches a plain loop to within normal
floating point rounding (the tests use a relative tolerance of 1e-12). Cost is
proportional to `input_count * window_samples`.

Version 2 adds the overflow handling and `max_spacing_deviation`; outputs that
were finite in version 1 are unchanged.

## Overlay

`overlay_filtered(plot_item, raw_values, result, name=...)` adds two curves to
a PyQtGraph `PlotItem`, with x in seconds: the raw signal as a thin dashed line
and the filtered signal as a thick solid line. Both break at missing values
instead of joining across them. Legend names spell out the filter and its
parameters, so the curves are not told apart by color alone.

`plot_filtered(plot_item, result, name=..., origin_ns=...)` adds only the
filtered curve, with x in seconds since `origin_ns`; the Filters tool uses it on
the main plot, whose raw trace is already drawn.
