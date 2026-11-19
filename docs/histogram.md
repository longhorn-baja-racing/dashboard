# Histograms

`lbr_dashboard.core.histogram` counts how many samples of one numeric signal
fall into each value bin over a time interval. `lbr_dashboard.ui.histogram_widget`
draws the result. The widget is not wired into the main window yet.

This is the sample-count mode only. Density, cumulative and time-weighted
histograms are out of scope until an analysis needs them.

## Inputs

```python
from lbr_dashboard.core.histogram import histogram

hist = histogram(
    times_ns,  # integer nanoseconds, non-decreasing
    values,  # floats; None or NaN means missing
    bins=20,  # or edges=[...]
    value_range=(0.0, 4000.0),  # optional
    start_ns=0,
    end_ns=5_000_000_000,
    unit="rpm",
)
```

The inputs are the same parallel arrays an importer produces (`array("q")`
times and `array("d")` values). `times_ns` must be non-decreasing; it is
searched with bisect and not re-checked.

## Time interval

The interval is closed: a sample at exactly `start_ns` or `end_ns` is inside.
`None` for either end means unbounded on that side. `start_ns > end_ns` is an
error. The result keeps the requested `start_ns`/`end_ns`.

## Bins

Give either `bins` (a positive integer, default 10) for equal-width bins, or
`edges` (at least two finite, strictly increasing values). `edges` cannot be
combined with `bins` or `value_range`.

Edge rule, the same as `numpy.histogram`: every bin is half-open `[a, b)`
except the last, which is closed `[a, b]`. A value exactly on an inner edge
goes into the bin to its right; a value exactly on the last edge goes into the
last bin.

Equal-width edges are `low + i * ((high - low) / bins)` for `i = 0 .. bins - 1`,
with the last edge set to exactly `high`. These are bit-for-bit the edges numpy
computes. Each value's bin is found by searching the edges, not by division, so
values on an edge always land in the bin the rule says.

Range for equal-width bins:

- `value_range=(low, high)` if given (finite, `low <= high`).
- Otherwise the min and max of the valid values in the interval.
- If the two ends are equal (for example all valid values are `7.0`), the range
  becomes `value - 0.5 .. value + 0.5`, as numpy does.
- A range too wide to split (its width overflows) or too narrow for the number
  of bins (edges would repeat) is an error.

## What is counted where

Every sample inside the time interval is counted exactly once, in one of:

| Field         | Meaning                                                  |
| ------------- | -------------------------------------------------------- |
| `counts[i]`   | finite value inside bin `i`                              |
| `below_range` | finite value below the first edge                        |
| `above_range` | finite value above the last edge                         |
| `missing`     | `None` or NaN                                            |
| `non_finite`  | `+inf` or `-inf` (never counted as below or above range) |

`valid` is `sum(counts)` and `total` is the sum of all five. Missing values
never enter a bin and never widen the automatic range; they are not zeros.

Weighting is by sample: each valid sample counts once however long it lasted,
so a 100 Hz signal has ten times the counts of a 10 Hz one over the same
interval. Compare shapes, not raw counts, across signals with different rates.

## Empty results

- No valid samples and no `edges`/`value_range`: `edges` and `counts` are both
  empty. Nothing is invented.
- No valid samples but explicit `edges` or `value_range`: the requested bins are
  kept with zero counts, which is a true answer for those bins.

In both cases `is_empty` is true and the missing/non-finite counts still say
what the interval held.

## Result and export

`Histogram` is frozen and carries `edges`, `counts`, the four out-of-bin counts,
`unit`, `start_ns`, `end_ns` and `edge_rule` (the rule above as text).
`to_rows()` returns one `HistogramRow(bin_start, bin_end, count, closed)` per
bin, where `closed` is `"left"` for `[a, b)` and `"both"` for the last bin. This
is the shape a later CSV export will write.

## Widget

`HistogramWidget.show_histogram(hist, name)` draws one bar per bin from its
start edge to its end edge. The x axis is labelled with the signal name and
unit, the y axis with `samples`. A text line under the plot lists the valid,
missing, non-finite and out-of-range counts and the interval in seconds, so no
information depends on color alone. An empty result draws no bars and says so in
the title. `clear()` resets the view.
