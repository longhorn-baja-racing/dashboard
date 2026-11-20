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
  order. Steps between samples (timestamp regressions and counter steps) are
  taken in recorded order over the whole input first and only then attributed
  to the interval, so a sample outside the interval never makes two samples
  inside it look adjacent.

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
  immediately before it, even when that earlier-recorded sample is outside the
  interval. The count is exact; `regression_examples` keeps the first 10 with
  their input positions.
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

Steps are classified over the whole input in recorded order and then
attributed to the interval:

- A step whose two samples are both inside the interval is counted as above.
- A step with one sample outside the interval, between the first and last
  inside samples in recorded order, cannot place any records it lost inside or
  outside the interval. If it skips values or resets it is counted in
  `unattributed_steps` instead; a `+1` step or repeat there is ignored. For
  example, times `[0, 100, 10]` with counters `[0, 1, 2]` over `[0, 10]` loses
  nothing, because counter 1 is in the recording at 100 ns.
- Steps before the first or after the last inside sample are outside the
  interval and ignored.

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
| Contiguous counter, no missing values but an unattributed step | `unknown` |
| Contiguous counter, no missing values, no resets | `complete` |

Timestamps alone never make a signal complete or incomplete: without a
recorded counter, completeness is unknown, never "no loss". `complete` covers
the records between the first and last counter value in the interval; records
lost before the first or after the last one cannot be seen.

## Session report

`lbr_dashboard.core.session_quality.summarize_snapshot(snapshot, start_ns=None,
end_ns=None)` runs the summaries above on every signal of a loaded
`SessionSnapshot` and adds what only the session knows. It is pure and
Qt-free: the same snapshot and interval always give the same report and text.

Per signal (schema order):

- Records are taken in **arrival order** (`SessionSnapshot.arrival_order`), so
  timestamp regressions stay visible. Unavailable records (`available=False`)
  are missing values. Non-numeric signals count each present reading as a
  value, so missing readings are counted for every data type.
- A declared `nominal_rate_hz` becomes the expected period for gaps
  (labelled `expected`) and gives an **expected record count** between the
  first and last record in the interval: `round(span * rate) + 1`, compared
  with the records present (unavailable readings included). The difference is
  the shortfall. Absences before the first or after the last record are not
  counted, so a source that ends or disconnects never inflates it. Without a
  declared rate there is no expected count and gaps use the median delta.
  Both are `inferred`.
- Runs of consecutive unavailable records, with their times (`recorded`).
- Counts of the quality label each record carries, for example `measured` /
  `missing` from the CSV preset or `simulated_unavailable` from the simulator
  (`recorded`).
- Records evicted by session retention (`SessionSnapshot.evicted`), labelled
  `retention`. Eviction removes old records to bound memory in live sessions;
  it is **not data loss** and is never added to missing values, gaps,
  shortfalls or dropped records. The report covers the retained records only.

Sequence counters reach the report through a descriptor declaration, because
the session stores counters per sample only as sample `extra` data:

```python
SignalDescriptor(..., extra={"sequence": {"field": "seq", "contiguous": True, "modulus": 65536}})
SignalSample(t, value, extra={"seq": 41})
```

When every record of the signal carries an integer counter in that field (in
range when a modulus is given), the counter goes to `summarize_signal`, so
`contiguous: true` turns skipped values into confirmed dropped records and
completeness can become `complete` or `incomplete`. Each counter event is placed
in time by the records on either side of it. If any record lacks the counter,
completeness stays `unknown` and the report says why. No current source
declares a counter yet.

Per source (by source id):

- The latest `SourceHealth` the source reported: state and terminal reason,
  records the source reports it lost before they reached the session
  (`dropped_records`, for example the simulator's scheduled loss or a full
  source queue), records still waiting in the source buffer, rejected controls
  and the last error. All `recorded`, because the source reported them.
- A source that has ended, stopped or closed is reported as no longer
  delivering: time after its last record is not missing data. When the user
  disconnects, the app commits the released source's final health to the
  session so the report shows it.
- Retained source events (the session keeps the latest 64): errors, end of
  stream, and health events whose `last_error` changed (heartbeats that repeat
  the same error add nothing). An event's time comes from
  `error.context["timestamp_ns"]` when the source attached one.
- Each source keeps its own evidence. Loss reported by one source (for example
  a live link) is never applied to another source's records (for example an SD
  recording), and a source's reported loss is shown next to, not subtracted
  from, the per-signal counts.

`findings(report)` lists gaps, unavailable runs, counter events and timed source
events in time order; `signal_row` and `source_rows` give the ASCII text the
tool shows.

## Analysis > Data quality

**Analysis > Data quality** opens a dock over the loaded session (CSV or
simulator). It summarizes the shared interval, or the whole session when no
interval is selected (**Whole session** clears the interval). It refreshes at
most every 200 ms while a source streams.

- **Timeline**: one row per signal over the whole session with its coverage
  (grey), gaps (red, `inferred`), unavailable runs (orange, `recorded`) and
  counter events (blue), plus a top row of timed source events (purple). The
  shared interval is drawn and can be dragged as on the main plot, and a green
  line follows the replay cursor. Clicking a gap or an unavailable run selects
  it as the shared interval.
- **Signals**: records, unavailable readings, expected records from a declared
  rate, gaps, confirmed dropped records, completeness and retention evictions
  per signal, with the evidence in each column header.
- **Gaps and events**: every finding in the interval with its times and
  evidence. Clicking a gap, unavailable run or counter event selects it as the
  shared interval.
- **Sources**: the per-source facts above.
- **Selected signal**: the full `format_quality` rows for the signal selected in
  the browser, plus its expected count, evictions and quality labels.

To try it on Windows (or any platform), run the app, open
`sample_data/timestamped_demo.csv` through the source picker and choose
**Analysis > Data quality**: the temperature reading at 0.250 s and the flow
reading at 0.500 s are listed as unavailable. The **Fictional process
simulator** adds pressure gaps at 0.190-0.250 s (5 records the source reports
lost) and 0.690-0.750 s (5 stale records), and 5 unavailable readings at
0.500-0.540 s.

## Not covered yet

These need a live transport adapter (#33) or protocol definitions:

- Firmware-reported versus transport-observed attribution. Today every source
  fact is labelled `recorded` (reported by the source adapter); a serial
  adapter will have to say whether loss was reported by the firmware or seen on
  the link.
- Recorded heartbeat, decoder, queue or storage health beyond what
  `SourceHealth` carries, and reconnect events. `core/source_health.py`
  computes link health, heartbeats and reconnects for a live transport, but no
  source delivers it into the session yet.
- Protocol-defined counter rollover and reset rules for real devices, and
  logging or decimation policies that would let live versus SD completeness be
  compared.
