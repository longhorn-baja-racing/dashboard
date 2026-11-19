# Issue #21 session storage validation

Validated locally on Windows with Python 3.12.14 and Qt 6.11.2, October 28, 2026.
Branch `codex/p0-21-session-queries` starts from issue #20 commit `3f14795`
(PR #122). No changes were pushed and no issue #21 PR was opened for this run.

## Acceptance evidence

| Requirement | Implementation and checks |
| --- | --- |
| Same query serves replay/live | One `TelemetrySession` and `SessionSnapshot` range/nearest/bounds API. Tests feed the production #20 source in finite and live modes and assert the same original query results. |
| Independent rates, missing never zero | 101 samples at 100 Hz, two at 1 Hz, irregular boolean samples and structured events retain independent records. Zero, false, explicit unavailable null and absent intervals remain distinct. |
| Consistent reader snapshots | A synchronized writer/reader test captures 30 atomic multi-signal batches under retention; each snapshot has matching signal versions/counts and eviction totals. Earlier snapshots stay unchanged after append/eviction. Nested metadata, series and records are immutable. |
| Bounded live retention | Live source ingestion requires a positive per-signal count cap. Count/age tests cover inclusive cutoffs, zero-age windows, duplicate timestamps, late arrivals and independent slow-signal watermarks. Control-event history and ingestion batch sizes are bounded. |
| Source time, units and identity | Tests preserve signed and larger-than-uint64 timestamps, original sample object identity, original units/quality, resolved source IDs and raw delivery order. Wrong sources/types/domains reject a batch atomically. No implicit clock normalization, interpolation or padding occurs. |
| Known range and nearest results | Inclusive range endpoints, empty/unknown inputs, duplicate timestamps, earlier-time tie rules, last-arrival duplicate rules, missing records and explicit distance limits are tested. Random late/duplicate/missing samples are compared against an independent reference query. |
| Errors/health survive ingestion | Persistent source health is captured even when descriptor/error/end controls cannot all fit. Explicit latest health takes precedence over queued older messages. Rejected drained batches remain available through `RejectedSourceBatch.events`; the store is unchanged. |

The clock/domain, gap, retention, arrival order and snapshot policies are specified
in `docs/telemetry-sessions.md`, including an executed example. Source-specific
CSV mapping, production source selection, replay clock/buttons, comparison,
derived channels and alternative storage backends are outside this issue.

## Results

- **485 strict tests passed**, with warnings as errors and **98.05%** overall
  branch-aware coverage. The new session module has all statements covered and
  approximately 99% branch-aware coverage.
- Ruff lint/format and Pyright passed; the documented Python example executed.
- Wheel and source distribution were built and each installed outside the
  checkout. Session queries, source lifecycle, schema round trips, app resources,
  entry point, CSV loading/plotting and dependency compatibility passed.
- The native Windows Qt workflow used a real source worker, application-owned
  polling timer, bounded session and existing plot. It verified 32 retained fast
  samples versus two slow samples, an unavailable record, a real zero, a gap
  tolerance, frozen bounds after append and worker cleanup.
- A separate local interactive harness was smoke-tested on the Windows backend:
  101 pressure / two temperature / three boolean fixture records; inclusive
  400–600 ms queries; missing value at 500 ms versus no slow sample within
  tolerance; live retention capped at 50 per signal; frozen snapshots while
  ingestion continues; late arrivals; atomic bad-batch rejection; clean stop.
  Those developer-only testing controls remain outside the production package.

## Tests demonstrated to fail

Five isolated copies were mutated without changing the production checkout:

1. Excluding the inclusive range end fails the endpoint/duplicate test.
2. Ignoring nearest-query distance bounds fails the mixed-rate gap test.
3. Disabling count retention fails the retained-count/duplicate test.
4. Adding one nanosecond to stored timestamps fails original-sample fidelity.
5. Committing a record before batch validation finishes fails all five atomic
   invalid-batch cases.

Each mutation returned assertion failures, and the corresponding unmodified tests
pass. Tests assert actual records, query results, revision consistency and worker
cleanup rather than merely that a window exists.

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

Use a supported offscreen Qt backend for headless CI. The interactive harness is
for pre-PR manual review; it does not add product replay/source controls.
