# Performance budgets and measurement

Team laptops must stay responsive with real session sizes. This page defines
the fixtures, the one command that measures them, and provisional budgets.
The budgets become final once they are measured on the team's reference
Windows laptop. Until then, they are targets, not release gates.

## Fixtures

`scripts/make_bench_session.py` writes deterministic lbr-csv sessions shaped
like a real DAQ export:

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

The same preset always produces the same bytes. Fixtures are generated into
`build/bench/` (ignored by git) on first use and are not committed.

## The command

```sh
uv run --locked python scripts/benchmark.py                       # standard, 3 runs
uv run --locked python scripts/benchmark.py --preset long --repeat 5
uv run --locked python scripts/benchmark.py --onscreen            # real display, not offscreen
uv run --locked python scripts/benchmark.py --enforce             # exit 1 if a budget is missed
```

Each run starts a fresh process, so startup and peak memory are measured
cleanly. The command prints the median, minimum and maximum of each metric
and compares the medians with the budgets. It also writes
`build/bench/results-<preset>.json` with every run and the machine details
(OS, CPU, cores, memory, Python).

Metrics, all for the lbr-csv session view:

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

## Provisional budgets

| Metric | Budget | Why |
| --- | --- | --- |
| `startup_s` | at most 3 s | The app should be usable within a few seconds at the track |
| `import_rows_per_s` | at least 150k rows/s | A 20 minute full-rate session (about 7M rows) opens in under a minute |
| `plot_one_s` | at most 0.5 s | Checking a signal should feel immediate |
| `plot_six_s` | at most 1 s | Comparing a few signals should stay fluid |
| `readout_ms` | at most 16 ms | One frame at 60 Hz while moving the cursor |
| `session_mb_per_million_rows` | at most 400 MB | A 2 h session fits on an 8 GB laptop |

Plot decimation is checked separately: `tests/test_performance_tools.py`
plots a two-minute fixture, confirms that peak downsampling is active, and
checks that the single-sample spike and the `SENSOR_FAULT` gap are still drawn.

## Results so far

Development laptop only. This is **not** the reference machine.

| Date | Machine | Preset | startup | import | open | plot 1 / 6 | readout | memory above baseline |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-11-02 | Linux x86_64, 12 cores, 16 GB, Python 3.13, offscreen | standard (1.5M rows) | 0.34 s | 558k rows/s (2.7 s) | 3.0 s | 0.07 / 0.31 s | 0.04 ms | 61 MB per million rows |

All provisional budgets were met, with three runs and small spread (see the
JSON for min and max).

## To do on the reference Windows laptop

1. Record the laptop model, CPU, RAM and Windows version here.
2. Run `scripts/benchmark.py --preset standard --repeat 5 --onscreen` and
   `--preset long --repeat 3 --onscreen` from the developer setup, and add the
   rows to the table above.
3. Adjust the budgets to what that machine can sustain with margin, then
   consider running `--enforce` on the `smoke` preset in CI.

Storage choices (for example a columnar cache, #32) should be decided from
these measurements, not ahead of them.
