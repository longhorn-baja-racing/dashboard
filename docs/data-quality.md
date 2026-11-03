# Data-quality summaries

`lbr_dashboard.core.data_quality` summarizes how complete and well-timed a
recorded signal is over an interval. It is pure standard-library code in
`core`: no Qt, no I/O, no wall-clock reads. The same input always produces the
same summary and the same text rows.

It works on recorded data only. It does not need USB or a live capture, and it
does not claim root causes (RF, wiring, firmware, storage). It reports what the
data shows and who established each finding.

## Inputs

- `times_ns`: integer nanoseconds, one per sample, in **recorded order**. Unlike
  interval queries, the timestamps do not have to be sorted; out-of-order
  samples are what the regression check looks for.
- `values`: one value per sample. `None` and NaN both mean "missing".
  Positive and negative infinity are counted separately as non-finite.
- `sequence` (optional): a per-sample counter stored in the recording, for
  example a record or frame number.
- `start_ns` / `end_ns` (optional): the interval. Intervals are **closed**: a
  sample at exactly `start_ns` or `end_ns` is inside. `None` leaves that side
  open. Samples are selected by their own timestamp and keep their recorded
  order.

```python
from lbr_dashboard.core.data_quality import format_quality, summarize_signal

quality = summarize_signal(
    [0, 10_000_000, 20_000_000, 50_000_000],
    [1.0, None, 2.0, 3.0],
    name="rpm",
    expected_period_ns=10_000_000,
)
for label, value in format_quality(quality):
    print(f"{label}: {value}")
```

`summarize_session(signals)` takes a mapping of signal name to
`(times_ns, values)` and returns one summary per signal, ordered by name. Each
signal is summarized independently, so signals logged at different rates get
their own rate estimate and reference period. `expected_periods_ns` gives a
per-signal expected period; naming a signal that is not in the mapping is an
error rather than being ignored.

## Evidence labels

Every finding says who established it. `format_quality` shows the label in
brackets after each row name.

| Label | Meaning | Findings |
| --- | --- | --- |
| `recorded` | Read directly from values and counters stored in the recording. | value counts, sequence counter findings, completeness |
| `inferred` | Derived by the dashboard from timestamps, possibly against a reference period. | time range, duplicates, regressions, deltas, rate estimate, gaps |

A third label, transport-observed evidence (for example USB packet loss seen
by a live capture), is a planned future input. It is deliberately not part of
these summaries yet: SD and USB observations describe different streams and
must not be merged. Loss seen on the live link says nothing about SD
completeness, and a decimated live stream is not missing data.

## Value counts (`recorded`)

- `samples`: samples inside the interval.
- `missing`: values that are `None` or NaN.
- `non_finite`: values that are `+inf` or `-inf`.
- `valid`: `samples - missing - non_finite`.

## Timing (`inferred`)

Regressions are judged in recorded order. Everything else is judged on the
timestamps sorted in time, because a sample recorded out of order still covers
its moment: a regression is not also reported as a gap.

- `first_ns` / `last_ns`: earliest and latest timestamp in the interval (in
  time, not in recorded order).
- `duplicate_timestamps`: samples whose timestamp equals that of another
  sample in the interval (sample count minus distinct timestamps).
- `regressions`: samples whose timestamp is earlier than the sample recorded
  immediately before it. The count is exact; `regression_examples` keeps the
  first 10 with their input positions.
- `min_delta_ns`, `median_delta_ns`, `max_delta_ns`: over the positive deltas
  between consecutive sorted timestamps. Zero deltas (duplicates) are
  excluded. The median is the lower median, so it is always an observed delta.
- `rate_hz`: `1e9 / median_delta_ns`. It is an **estimate from the median
  delta**, not a configured or measured logging rate.

### Gaps

- The reference period is `expected_period_ns` when given (for example from a
  documented logging rate), labelled `expected`. Otherwise it is the median
  delta, labelled `median`. With no positive delta and no expected period
  there is no reference and no gap check.
- A gap is a positive delta strictly longer than `gap_factor * reference`
  (default `gap_factor = 1.5`, which flags a single skipped sample;
  `gap_factor` must be greater than 1).
- Each gap reports the timestamps of the samples on either side (`start_ns`,
  `end_ns`), `duration_ns = end_ns - start_ns`, and `missing_periods`: the
  delta divided by the reference, rounded half up, minus one, never negative.
  It is approximate.
- Gaps at the edges of the requested interval (before the first or after the
  last sample) are not reported.
- A gap is an observed absence of samples, not a confirmed dropped record. A
  signal that is decimated or logged on change will show gaps against a faster
  reference. Choose the reference that matches the stream being checked.

## Sequence counters (`recorded`)

When a counter is given, each step between consecutive samples in recorded
order is classified:

- `previous + 1`: normal, not reported.
- equal to `previous`: a **repeat**.
- larger than `previous + 1`: **missing** counter values,
  `value - previous - 1` of them.
- smaller than `previous`, with `sequence_modulus` set and the forward
  distance across the wrap, `value + modulus - previous`, at most half the
  modulus: a **rollover**. Values skipped across the wrap count as missing.
  With a modulus, counter values must lie in `[0, modulus)`.
- any other decrease: a **reset**. It starts a new run. Values lost across a
  reset cannot be counted. Without a modulus every decrease is a reset.

Counts are exact; `events` keeps the first 10 non-normal steps.

Missing counter values become **confirmed dropped records** only when the
caller states the counter is contiguous (`sequence_contiguous=True`: it
increases by exactly one per record). Otherwise `dropped_records` is `None`,
because a counter may legitimately skip values.

## Completeness

| Situation | Completeness |
| --- | --- |
| No sequence counter | `unknown` |
| Counter not defined as contiguous | `unknown` |
| Contiguous counter, no samples in the interval | `unknown` |
| Contiguous counter, any missing values | `incomplete` |
| Contiguous counter, no missing values but a reset | `unknown` |
| Contiguous counter, no missing values, no resets | `complete` |

Timestamps alone never make a signal complete or incomplete: without a
recorded counter, completeness is unknown, never "no loss". `complete` covers
the records between the first and last counter value in the interval; records
lost before the first or after the last one cannot be seen.

## Not covered yet

- Recorded heartbeat, decoder, queue or storage health and reconnect events
  (they depend on the source health work in #44).
- Transport-observed evidence from live capture.
- Wiring into the session model (#21) and any UI beyond text rows.
