# Issue #23 simulator validation

Validated on Windows with Python 3.12.14 and Qt 6.11.2, October 29, 2026.
Branch `codex/p0-23-generic-simulator` starts at issue #22 commit `d14f8f7`.

## Acceptance evidence

| Requirement | Evidence |
| --- | --- |
| Hardware-free mixed-rate app data | Production `SimulatorSource` implements the existing source contract. Native Qt tests feed both fictional process and motion families through the session/query/plot flow, without sensor-specific widget changes. |
| Configurable names, types, units and rates | Descriptors and repeating value cycles configure every signal. Tests include unrelated arbitrary string names/source/domain, numeric pressure/temperature, vector, boolean, status and event values. No required vehicle channels or protocol. |
| Repeatable seeds/timestamps | Repeated finite runs compare all events and statistics. Adding another signal or reversing configuration order does not alter the original stream. Different seeds change noisy values only. A 3 Hz schedule is checked against exact expected integer offsets to catch drift. |
| Loss differs from unavailable/stale | Loss and stale windows omit records and emit reasoned recoverable events; unavailable windows emit explicit nulls. Tests check real zero/false, exact absent versus null query results and separate persistent counters. Heartbeats continue during gaps. |
| Finite/live/end/cancel/disconnect | Accelerated finite tests forbid clock reads/waits. Injected fake pacing produces the same records as accelerated runs. Tests cancel live generation, close with a stalled consumer, check fatal disconnect boundaries, zero duration, malformed configurations and observable clock failures. |
| Source/session/clock compatibility | Source events retain IDs, original time domain, units, type and quality. Qt owner polling populates bounded sessions; the shared clock's subscribers see identical frames. Generation never consults replay selected time. |

The mapping, fault-window, seed, scheduling and pacing policies plus expected
example counts are documented in `docs/simulator.md`. The example executed.
CSV fixture generation (#105), production source-selection/demo wiring (#102),
scenario editing, physics and real hardware are outside this implementation.

## Results

- **576 strict tests passed**, warnings as errors; **98.35%** overall branch-aware
  coverage. Simulator coverage rounds to 99%.
- Ruff lint/format and Pyright passed.
- Wheel and source distribution were built and each installed outside the
  checkout. Simulator generation, source/session/clock behavior, application
  resources/entry point, CSV plotting and dependency compatibility passed.
- The native Windows harness passed repeated-seed fingerprints, unrelated
  signal families, mixed rates, loss/null/stale distinctions, scheduled fatal
  disconnect and live cancellation/cleanup. It displays original records and
  separate simulator/queue loss counters. Its local controls are outside the
  production package and remain a manual-review tool.

## Mutation evidence

Five isolated mutated copies produced targeted assertion failures, without
changing the production checkout:

1. Emitting available zero for unavailable samples fails missingness checks.
2. Filling loss/stale windows with records fails absent-record assertions.
3. Ignoring the seed fails different-seed noisy-value comparisons.
4. Rounding each period before multiplying fails exact 3 Hz timestamps.
5. Misreporting a simulated disconnect fails all three boundary cases.

Each corresponding unmodified test passes. Assertions inspect actual events,
samples, timestamps, source health and cleanup rather than window existence.

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

Use a supported offscreen Qt backend on headless CI. Generation/pacing tests
advance injected time rather than sleeping for the simulated recording duration.
