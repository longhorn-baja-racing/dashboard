# Current values and interval statistics

`lbr_dashboard.core.statistics` computes the current value of one numeric signal
and basic statistics over a time interval (issue #43). It is standard library
only and takes plain inputs, so it can run in a worker and does not depend on
the signal/session model that is still being designed:

- `times_ns`: integer nanosecond timestamps, non-decreasing (equal timestamps
  are allowed), for example an `array('q')`.
- `values`: one reading per timestamp, for example an `array('d')` or a list
  that may contain `None`.

`lbr_dashboard.ui.statistics_panel.StatisticsPanel` is a mock text readout of
the results. It is not wired into the main window yet.

## Sample classification

| Reading | Class | Used in min/max/mean |
| --- | --- | --- |
| finite number (including negative values and 0) | valid | yes |
| `None` or NaN | missing | no, counted in `missing_count` |
| `+inf` or `-inf` | invalid | no, counted in `invalid_count` |

Missing and invalid readings are never replaced by zero and never filled in
with an earlier valid value.

## Interval rules

- Intervals are closed: a sample at exactly `start_ns` or `end_ns` is inside.
  `start_ns > end_ns` is rejected with `ValueError`; `start_ns == end_ns`
  selects only the samples at that instant.
- Every sample in the interval is counted once, including samples that share a
  timestamp. `sample_count = valid_count + missing_count + invalid_count`.
- `minimum`, `maximum`, and `mean` are computed from valid samples only. When
  there are no valid samples (empty or all-missing interval) they are `None`
  and the readout shows `-- (no valid samples)`.
- `first_valid_ns` and `last_valid_ns` give the time span actually covered by
  valid samples, which can be shorter than the requested interval.

## Weighting

Statistics are **sample-weighted**: each valid sample counts once, no matter
how much time separates it from its neighbors. The mean is
`fsum(valid values) / valid_count`. Consequences:

- A burst of samples at a high rate pulls the mean toward the burst values.
  For example, samples `0, 0, 10` at `0 ns, 1 ns, 1 s` have a mean of `10 / 3`,
  not the time-weighted value close to 0.
- Signals with different sample rates get independent counts over the same
  interval: a 100 Hz and a 10 Hz signal over a closed 1 s interval have 101 and
  11 samples.
- Gaps (missing samples or long pauses) are not interpolated.

Time-weighted means, median, standard deviation, and RMS are possible later
extensions and are not part of this slice.

## Current value

`current_value(times_ns, values, at_ns, max_age_ns=None)` looks at the latest
sample at or before `at_ns` (the last one if several share that timestamp):

| State | Meaning | Value shown |
| --- | --- | --- |
| `NO_DATA` | no sample at or before `at_ns` | `--` |
| `MISSING` | the latest sample is `None` or NaN | `--` |
| `INVALID` | the latest sample is `+/-inf` | `--` |
| `STALE` | the latest sample is valid but older than `max_age_ns` | value with `(stale)` |
| `VALID` | the latest sample is valid and recent enough | value |

A missing latest sample never falls back to an older valid value. An age equal
to `max_age_ns` is still `VALID`; `max_age_ns=None` disables the stale check.
The readout spells out the state in words, so it never relies on color alone.

## Display format

- Numbers use six significant digits (`-4.5 V`, `0.333333`, `1.23457e+06 rpm`)
  followed by the unit when one is given.
- Times and durations are exact decimal seconds from the integer nanoseconds,
  with at least three decimals (`1.500 s`, `1.000000001 s`).
- The readout always includes a `Weighting` row and a `Counts` row with the
  definitions above.

## Performance

The interval is located with binary search, so the work is proportional to the
number of samples inside the interval. Ordering is checked for those samples
and a `ValueError` is raised if they are out of order; out-of-order samples
outside the selection are not detected. A full 1,000,000-sample selection takes
a fraction of a second in pure Python; running it off the UI thread is left to
the integration with the window queries (#21).
