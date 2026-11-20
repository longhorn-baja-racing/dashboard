# Performance budgets and measurement

Team laptops must stay responsive with real session sizes. This page defines
the reference hardware, the fixtures, the one command that measures them, the
budgets, and the results so far. The budgets are provisional until they are
measured on the team's reference Windows laptop. Until then, they are targets,
not release gates. CI only checks them with a generous margin (see
[CI](#ci)).

## Reference hardware

The budgets are meant for the slowest laptop the team brings to the track.

| Item | Value |
| --- | --- |
| Laptop model | to be recorded |
| CPU | to be recorded |
| RAM | to be recorded (the budgets assume 8 GB) |
| Windows version | to be recorded |
| Power | plugged in, "Balanced" power plan |
| Python | 3.13 from the developer setup (`uv sync --locked`) |

`scripts/benchmark.py` records the OS, CPU model, core count, memory, Python,
PySide6, pyqtgraph and numpy versions, the Qt platform and the git commit in
every JSON result, so a result can always be traced to its machine.

## Fixtures

All fixtures are generated, deterministic and not committed. The same preset
or workload always produces the same data (fixed seed `27`).

### lbr-csv sessions

`scripts/make_bench_session.py` writes lbr-csv sessions shaped like a real DAQ
export:

- 24 IMU signals at 100 Hz on four corner nodes
- 9 temperature and GPS signals at 10 Hz
- 2 signals at 1 Hz
- heartbeats for six nodes once a second
- per-sample timing jitter, so timestamps are irregular and rows of different
  channels are slightly out of order
- a 2 s `SENSOR_FAULT` gap on `corner_br` every minute
- one single-sample spike on `corner_fl.imu_fl_accel_z` halfway through

| Preset | Duration | Rows | File size |
| --- | --- | --- | --- |
| `smoke` | 30 s | about 75k | about 5 MB |
| `standard` | 10 min | about 1.5M | about 100 MB |
| `long` | 2 h | about 18M | about 1.2 GB |

The files are generated into `build/bench/` (ignored by git) on first use.

### Generic session stream

`scripts/bench_generic.py` generates `SourceEvent` records for the generic
session stack (the source picker path) in memory, in measurement-time order,
one second at a time. Each signal has its own seeded generator, so a longer
fixture starts with the same samples as a shorter one.

| Group | Signals | Rate | Samples per second |
| --- | --- | --- | --- |
| `bench.strain` | 1 | 1 kHz | 1000 |
| `bench.imu.NN` | 24 | 100 Hz | 2400 |
| `bench.temp.NN` | 9 | 10 Hz | 90 |
| `bench.battery.NN` | 2 | 1 Hz | 2 |
| total | 36 | mixed | 3492 |

- timestamps jitter by up to 30 % of a period, so they are irregular and
  interleaved across signals
- `bench.strain` and `bench.imu.23` report unavailable `SENSOR_FAULT` readings
  for 1 % of the duration (at least 50 ms), starting at one quarter
- `bench.strain` has one single-sample spike of 5000 halfway through
- a sparse `timestamp_ms` CSV (rows 10 ms apart, every seventh cell blank) is
  written for the source picker measurement

| Workload | Replay session | Live data (warm-up) | Picker CSV | Seeks |
| --- | --- | --- | --- | --- |
| `smoke` (tests) | 2 s, 7k samples | 3 s (1 s) | 200 x 4 | 5 |
| `quick` (CI) | 20 s, 70k samples | 30 s (10 s) | 1000 x 6 | 40 |
| `full` (reference) | 10 min, 2.1M samples | 10 min (60 s) | 5000 x 10 | 200 |

## The command

```sh
uv run --locked python scripts/benchmark.py              # full mode, 3 runs, about 10 min
uv run --locked python scripts/benchmark.py --quick      # CI-sized, 2 runs, under a minute
uv run --locked python scripts/benchmark.py --ci         # --quick --enforce --margin 4
uv run --locked python scripts/benchmark.py --onscreen   # real display, not offscreen
uv run --locked python scripts/benchmark.py --enforce    # exit 1 if a budget is missed
uv run --locked python scripts/benchmark.py --preset long --workload full --repeat 5
```

| Mode | lbr-csv preset | Generic workload | Runs |
| --- | --- | --- | --- |
| full (default) | `standard` | `full` | 3 |
| `--quick` | `smoke` | `quick` | 2 |

`--preset`, `--workload` and `--repeat` override a mode's choices. Each run
starts two fresh processes, one for the lbr-csv view and one for the generic
stack, so startup and peak memory are measured cleanly. The command prints the
median, minimum, maximum and spread ((max - min) / median) of each metric,
checks each budgeted median and prints PASS or FAIL with the headroom left. It
writes `build/bench/results-<mode>.json` (`results-full.json`,
`results-quick.json`, or `results-<preset>-<workload>.json` with overrides)
with:

- `machine`: OS, CPU, cores, memory, versions, Qt platform, commit
- `workload`: the stated rates, signal counts, durations and seed
- `budgets`: every budget with its limit and the limit after the margin
- `summary`: median, min, max, standard deviation and spread per metric
- `checks`: pass or fail and headroom per budget; `missed` and `passed`
- `runs`: every raw run

Exit codes: 0 when done (or all budgets met with `--enforce`), 1 when
`--enforce` is set and a budget is missed, 2 when a measurement crashed. The
live measurement also fails the run outright if any signal keeps more than
1000 samples, because then retention is not bounded.

## Metrics

lbr-csv session view (`scripts/benchmark.py`):

| Metric | Meaning |
| --- | --- |
| `startup_s` | Process start to the main window shown, including imports |
| `import_s`, `import_rows_per_s` | Reading the whole export into the session model |
| `open_s` | File > Open equivalent: import plus building the session view |
| `plot_one_s` | Checking one 100 Hz signal until the window has rendered |
| `plot_six_s` | Checking six 100 Hz signals until the window has rendered |
| `readout_ms` | Median time to update the cursor readout at a new time |
| `peak_mb`, `baseline_mb` | Peak memory of the process, and after startup |
| `session_mb_per_million_rows` | Memory above the startup baseline, per million rows |

Generic session stack (`scripts/bench_generic.py`):

| Metric | Meaning |
| --- | --- |
| `ingest_samples_per_s` | `TelemetrySession.ingest` throughput for the replay session, in batches of 512 (the source controller's per-poll limit) |
| `snapshot_ms` | Median `TelemetrySession.snapshot()` on the whole replay session |
| `range_ms` | Median `SessionSnapshot.range` for a 10 s window of a 100 Hz signal (about 1000 records) |
| `session_mb_per_million_samples` | Resident memory the replay session adds, per million samples |
| `plot_full_ms` | `TimePlot.refresh` plus a repaint with the whole 1 kHz trace fitted (decimated) |
| `plot_zoom_ms` | The same, zoomed to a 10 s window (10k points) |
| `plot_trace_points`, `plot_drawn_points` | Points in the trace and points left after decimation |
| `seek_ms`, `seek_p95_ms` | Median and 95th percentile of a replay seek in the real window (`ReplayController.seek_slider` plus the resulting events), on the picker CSV |
| `app_open_s`, `app_open_samples_per_s` | Opening the picker CSV through the source picker until replay is ready |
| `live_cpu_pct` | Share of one core spent ingesting and snapshotting at the stated 3492 samples/s, polled every 30 ms as the app does, with `max_samples_per_signal=1000` |
| `live_samples_per_s` | The highest rate that live ingest could sustain on one core |
| `live_poll_p95_ms` | 95th percentile time of one live poll (ingest plus snapshot) |
| `live_rss_growth_mb`, `live_rss_mb_per_min` | Resident memory growth after the warm-up, total and per minute of live data |
| `live_retained_max`, `live_evicted` | Largest retained series (must not exceed 1000) and samples dropped by retention |
| `generic_peak_mb` | Peak memory of the generic process |

The live measurement uses simulated measurement time: it runs as fast as the
work allows, polls every 30 ms of data time, and times only the dashboard's
work (ingest and snapshot), not generating the fixture.

## Budgets

All budgets are defined once, in `BUDGETS` in `scripts/benchmark.py`. A budget
compares the median of the runs.

| Metric | Budget | Why |
| --- | --- | --- |
| `startup_s` | at most 3 s | The app should be usable within a few seconds at the track |
| `import_rows_per_s` | at least 150k rows/s | A 20 minute full-rate export (about 7M rows) opens in under a minute |
| `plot_one_s` | at most 0.5 s | Checking a signal should feel immediate |
| `plot_six_s` | at most 1 s | Comparing a few signals should stay fluid |
| `readout_ms` | at most 16 ms | One frame at 60 Hz while moving the cursor |
| `session_mb_per_million_rows` | at most 400 MB | A 2 h export fits on an 8 GB laptop |
| `ingest_samples_per_s` | at least 100k samples/s | A 20 minute session at 3492 samples/s (4.2M samples) ingests in under 45 s |
| `snapshot_ms` | at most 50 ms | The app takes a snapshot every poll; it must not stall for several frames |
| `range_ms` | at most 1 ms | A plot window query is a small part of one frame |
| `session_mb_per_million_samples` | at most 600 MB | A 10 minute session (2.1M samples) fits in about 1.3 GB |
| `generic_peak_mb` | at most 2000 MB | The full workload fits on an 8 GB laptop next to the OS |
| `plot_full_ms` | at most 250 ms | Fitting the whole trace feels immediate |
| `plot_zoom_ms` | at most 50 ms | Panning and zooming stay smooth |
| `seek_ms` | at most 16 ms | A typical seek lands within one frame |
| `seek_p95_ms` | at most 50 ms | Dragging the replay slider never stutters |
| `live_cpu_pct` | at most 25 % of one core | Live ingest leaves the GUI thread time to render |
| `live_poll_p95_ms` | at most 10 ms | Each 30 ms poll leaves time to draw |
| `live_rss_mb_per_min` | at most 16 MB per minute | Bounded retention: memory stops growing once the buffers are full |

`app_open_samples_per_s` is recorded but not budgeted yet; see
[findings](#findings-so-far).

Plot decimation is checked by tests, not timings:
`tests/test_performance_tools.py` confirms that the lbr-csv view and the
generic `TimePlot` decimation (`render_points`) keep the single-sample spike
and break the line at the unavailable gap.

## CI

The `Performance` workflow (`.github/workflows/performance.yml`) runs
`scripts/benchmark.py --ci` on `ubuntu-latest` and `windows-latest` for every
pull request and push to `main`, and uploads `build/bench/results-*.json` as
the `benchmark-<os>` artifact for 30 days.

Hosted runners are shared, virtualized and noisy, so `--ci` loosens every
budget by `CI_MARGIN` = 4x: a time may be four times its budget, a
throughput a quarter of it, and memory four times its budget before CI fails.
CI therefore only catches gross regressions (an accidental quadratic loop,
unbounded retention, a broken decimation path). It is not a substitute for
the reference measurement, and its numbers must not be copied into the results
table. The workflow is not part of the required `CI required` check.

## Running on the reference Windows laptop

1. Plug the laptop in, select the "Balanced" power plan, close other
   applications and let it idle for a minute.
2. In a fresh clone: `uv sync --locked`.
3. Fill in the [reference hardware](#reference-hardware) table.
4. Run the full mode on the real display:
   `uv run --locked python scripts/benchmark.py --onscreen`
   (about 10 minutes). Optionally also run
   `uv run --locked python scripts/benchmark.py --onscreen --preset long --workload full`.
5. Copy the medians from `build/bench/results-full.json` into the Windows row
   of the results table below, and attach the JSON to the pull request.
6. If a budget is missed, or met by a huge margin, adjust it in `BUDGETS` and
   in the budgets table here in the same change, with the measurement as the
   reason.

## Results so far

Medians. The development laptop is **not** the reference machine.

lbr-csv session view, `standard` preset (1.5M rows):

| Date | Machine | startup | import | open | plot 1 / 6 | readout | memory above baseline |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-11-02 | Linux x86_64, 12 cores, 16 GB, Python 3.13, offscreen, 3 runs | 0.34 s | 558k rows/s (2.7 s) | 3.0 s | 0.07 / 0.31 s | 0.04 ms | 61 MB per million rows |
| 2026-11-20 | Linux x86_64, Core 7 150U, 12 cores, 16 GB, Python 3.13, offscreen, 2 runs | 0.93 s | 288k rows/s (5.2 s) | 5.9 s | 0.14 / 0.52 s | 0.06 ms | 59 MB per million rows |
| to be measured | Reference Windows laptop, onscreen | | | | | | |

Generic session stack, `full` workload:

| Date | Machine | ingest | snapshot (2.1M) | range | MB per million samples | plot full / zoom | seek median / p95 | live CPU | live poll p95 | live growth | peak |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-11-20 | Linux x86_64, Core 7 150U, 12 cores, 16 GB, Python 3.13, offscreen, 2 runs | 530k samples/s | 98 ms (FAIL) | 0.03 ms | 445 MB | 171 / 18 ms | 5.4 / 12 ms | 2.8 % | 1.7 ms | 0 MB/min | 1089 MB |
| to be measured | Reference Windows laptop, onscreen | | | | | | | | | | |

## Findings so far

From the development laptop only; confirm on the reference laptop.

- **Snapshots are O(session size).** `TelemetrySession.snapshot()` copies
  every retained record, about 75-100 ms for the 2.1M-sample replay session,
  which misses the 50 ms budget. The source controller takes one snapshot per
  30 ms poll while a replay source loads, so loading large sessions gets
  slower as they grow. Live sessions are bounded (1000 samples per signal), so
  live snapshots stay near 1 ms.
- **Opening through the source picker is paced, not CPU bound.** The picker
  CSV opens at about 3.3k-3.8k records/s (50k records in 13 s): each 30 ms poll drains at most what
  the source queue holds (128 events for the CSV source), so the 200k-record
  CSV import limit takes about a minute to open. Raising the per-poll drain or
  queue capacity is cheap; it is recorded here for #30/#38 rather than tuned
  in this change.
- **Generic sessions cost about 450 MB per million samples**, several times
  the lbr-csv session's columnar storage (61 MB per million rows). A 2 h
  session at 3492 samples/s (25M samples) would not fit in 8 GB. That is the
  measured evidence to weigh for a columnar store or cache (#32).
- Live ingest at the stated rate uses a few percent of one core and memory
  stays flat once the buffers are full.

Storage choices (for example a columnar cache, #32) should be decided from
these measurements, not ahead of them.
