# Filters

`lbr_dashboard.core.filters` turns one numeric signal into a smoothed copy.
`lbr_dashboard.ui.filter_overlay` draws the raw and filtered curves together.
Neither is wired into the main window yet.

Only a moving average exists. Low-pass, high-pass and band-pass filters are
out of scope for this first slice: no one has asked for a specific one yet, and
they need a vetted DSP dependency (for example SciPy), which the project does
not have. They will be added as separate named filters with their own rules
when a concrete analysis needs them.

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
bad spacing. Nothing is resampled or interpolated. Spacings within the
tolerance are accepted as they are; samples still get equal weight.

Timestamps must be integers and strictly increasing; duplicates and backwards
steps are errors.

## Gaps

A sample whose value is `None`, NaN or +/-inf is missing. It never enters an
average, and it is never treated as zero. It splits the signal into
segments, and a window never reaches across a split.

A spacing of at least `gap_ns` (when given) is a time gap: it also splits the
signal and is not checked for jitter. Without `gap_ns`, a dropout (for example
a spacing of three periods) is an irregular spacing and is rejected. `gap_ns`
must be longer than the nominal period plus its tolerance.

## Edges

No partial windows. A sample gets an output only when its whole window lies
inside its segment. Otherwise its output is missing:

- centered: the first and last `h` samples of each segment,
- trailing: the first `window_samples - 1` samples of each segment,
- every sample of a segment shorter than the window.

Shrinking the window at edges is not done, because it would silently change the
filter (and, for centered, its symmetry) near every gap.

## Result

`FilteredSignal` is frozen and carries:

| Field | Meaning |
| --- | --- |
| `times_ns` | read-only copy of the input timestamps |
| `values` | read-only float array aligned with `times_ns`, NaN where there is no output |
| `values_or_none()` | the same values as a list with `None` instead of NaN |
| `filter_name`, `version` | `"moving_average"` and the algorithm version |
| `params` | `MovingAverageParams` (window, alignment, tolerance, gap) |
| `input_count`, `missing_count`, `edge_count`, `output_count` | sample counts |
| `nominal_period_ns`, `window_span_ns`, `delay_ns` | timing; `None` with fewer than two samples |
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

## Overlay

`overlay_filtered(plot_item, raw_values, result, name=...)` adds two curves to
a PyQtGraph `PlotItem`, with x in seconds: the raw signal as a thin dashed line
and the filtered signal as a thick solid line. Both break at missing values
instead of joining across them. Legend names spell out the filter and its
parameters, so the curves are not told apart by color alone.
