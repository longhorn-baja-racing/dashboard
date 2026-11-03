# Issue #20 source contract validation

Validated locally on Windows, Python 3.12.14 and Qt 6.11.2, October 25, 2026.
Branch `codex/p0-20-source-lifecycle` starts from tested issue #18 commit
`ded6748` (PR #120). Previous PR #99 is closed and was not modified.

## Acceptance mapping

| Requirement | Implementation and evidence |
| --- | --- |
| One typed contract for finite, live, replay-like and simulated data | Qt-free `TelemetrySource`, capabilities, lifecycle states, typed identified events and a production `IteratorSource`. Tests run that production class with finite replay-like inputs, live iterators and failure-injected factories, including unrelated numeric, boolean and structured status signals. |
| Every record retains signal/source, time, units and quality | Source events require source ID and record signal ID. Descriptor binding validates known signal, source, type/shape and timestamp domain. Tests assert exact signed/large timestamps, duplicate timestamps/order, units, missing nulls, real zero/false and immutable error context. |
| Stop unblocks production and releases resources | Stop cancels and closes publication; bounded join/close report whether worker cleanup finished. Tests cancel a live iterator with a stalled consumer, cancel during factory opening, link external tokens, repeat close/reopen, and assert cleanup. Noncooperative opening is deliberately tested: close returns false within its deadline and succeeds after the adapter is released. |
| Overflow, terminal and error state remain observable | Bounded queue capacity includes controls. Descriptor/error/end publication honors timeout/cancellation/close. Source catalog and persistent health retain descriptors, latest error, loss counts and terminal reason even when all control events cannot fit. Tests cover a 1,000-record source with no consumer and concurrent producers. |
| Failures and unsupported operations are truthful | Factory, worker-start, read, bad-data, fatal/recoverable adapter and cleanup failures are tested. Read failure is retained when cleanup also fails. Seek/rate capabilities are false for iterator sources and both operations explicitly raise unsupported-operation errors. |
| No blocking work or UI callbacks on the Qt owner | A production source runs on its own worker. A Qt timer drains identified records and presents them through the existing window. Tests assert different producer/owner thread IDs, owner heartbeats, plotted values and cleanup. All core modules also pass subprocess import tests that block Qt imports. |

The format-specific CSV adapter (#31), simulator scenarios (#23), session queries
(#21), replay clock (#22) and reusable adapter conformance toolkit (#111) remain
separate integrations. The source contract does not require or implement #19's
unselected binary transport. Consequently the unrelated ineffective CRC test
from old PR #99 was not carried into this branch.

## Local results

- **447 strict tests passed**, with warnings as errors and **97.78%** overall
  branch-aware coverage. Source contracts and concrete source code were fully
  covered by the measured run.
- Ruff lint/format checks and Pyright passed.
- Built wheel and source distribution and installed each outside the checkout.
  Source start/end/cleanup, signal-model round trips, application resources and
  entry point, CSV loading/plotting, and dependency compatibility passed.
- An automated native Windows event-loop run delivered original values
  `[5, 0, null, 8]` at `[0, 10000000, 20000000, 30000000]` nanoseconds. The plot
  retained zero and omitted the unavailable point. Owner timer ticks continued;
  producer and owner had different thread IDs. Cancellation reported `cancelled`,
  iterator cleanup completed and the application exited with code zero.
- The existing native CSV harness also passed distinct duplicate-column plots,
  empty-load clearing, visible decode failure with prior data retained, and valid
  sample recovery. File-dialog paths were supplied programmatically by tests.

## Mutation evidence

The full-control-queue regression failed before the fix: a 20 ms publish timeout
still had not returned after 500 ms. Test cleanup drained the queue and joined the
producer so the reproduction did not leave a blocked thread behind.

Three isolated mutated copies were tested, leaving production files unchanged:

1. Restoring the original queue implementation makes the timeout regression fail.
2. Adding one nanosecond to every published sample makes exact-record fidelity
   checks fail.
3. Disabling dropped-record accounting makes the no-consumer loss test fail.

Each mutation exited with assertion failures; the corresponding unmodified tests
pass. No assertion relies on an unrelated noise prefix or merely on a window
existing.

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

For headless CI use a supported offscreen Qt backend. Lifecycle, cancellation,
queue semantics and an adapter example are in `docs/telemetry-sources.md`.
