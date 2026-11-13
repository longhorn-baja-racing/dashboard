# Issue #18 acceptance validation

Validated locally on Windows, Python 3.12.14 and Qt 6.11.2, October 25, 2026.
Branch `codex/p0-18-generic-signals` starts from PR #113 commit `a259cf5`.
The previous issue #18 PR #97 is closed and was not reopened or modified.

## Acceptance evidence

| Requirement | Verification |
| --- | --- |
| Generic IDs, hierarchy, types, shapes, units, sources/devices, rates and metadata | Eight unrelated descriptors across four sources in `tests/fixtures/generic_signals.json`; duplicate display names remain independent. Known type and nested vector shape validation rejects mismatches. No hardware or codec dependency. |
| Independent sampling rates and event/status values | 101 samples at 100 Hz and two samples at 1 Hz keep independent timestamps/counts; no alignment or synthetic values. Structured event/status and future opaque type round trips are checked. |
| Original timestamps, units, quality and missingness | Signed nanoseconds and values above uint64 retain integer precision; repeated/reset timestamps preserve order; source/domain mismatches fail descriptor validation. Original sample units survive even when different from canonical units. Unavailable samples require null, and available samples require a value. |
| Versioned serialization and compatibility | Schema JSON and descriptor/sample/series dictionaries round trip. Unknown fields are retained recursively by default and rejected recursively in strict mode. Unknown type names remain opaque. Invalid envelopes, duplicate JSON keys, non-string keys, non-finite numbers, cycles and excessive nesting are rejected. |
| Deterministic, safe registry | Stable ID ordering, prefix hierarchy, search, duplicate IDs across sources, collision retries and atomic batch registration. A 10,000-descriptor schema checks large generic inputs. |
| No data loss through shared mutable values | Mutating original input containers or serialized output cannot change model snapshots. Metadata keys such as `k` and ` k` survive independently. Reserved extra fields cannot overwrite known fields. |
| Architecture remains independent of Qt | Existing architecture tests import every core module, including signals, in a subprocess that blocks Qt/plotting imports. |

The mapping policy for legacy CSV times, explicit adapter validation boundaries,
unknown versions/types, units and missingness is in `docs/signal-model.md`.
CSV-to-signal adapter implementation, clock normalization (#21), wire framing
(#19), hardware drivers and UI redesign are outside this model issue.

## Results

- Full strict Windows test suite: **388 passed**, warnings treated as errors,
  **96.68%** overall branch-aware coverage; signal model rounds to **99%**.
- Ruff lint and format checks passed; Pyright reported zero errors/warnings.
- Built wheel and source distribution; installed each outside the checkout.
  Both passed signal-model round trips and app imports/resources/entry point,
  sample CSV loading and plotting. Installed dependency compatibility passed.
- Native Windows Qt event-loop harness opened the app, verified each duplicate
  column's distinct plot, empty-file clearing, invalid UTF-8 failure reporting
  with previous data preserved, and subsequent sample-file recovery. It exited
  with code zero and saved screenshots. This was an automated native run; the
  file-dialog boundary was exercised by tests with selected paths supplied.

## Tests demonstrated to fail

Replaced only the signal-model module in an isolated copy with the original
implementation from `9dde1bff54bc8a0fff9dd69edb639ce316540d16`. Running the eight
cases in `tests/core/test_signal_regressions.py` produced **eight failures**:
partial batch registration, stripped/colliding metadata keys, unavailable
non-null values (five cases), and mutable sample payloads. The same cases pass
against this implementation. The production checkout was not modified by this
mutation run.

## Reproduction

Use the locked development environment, then run:

```powershell
python -m ruff check .
python -m ruff format --check .
python -m pyright
$env:QT_QPA_PLATFORM='windows'
python -m pytest --cov=lbr_dashboard --cov-branch --cov-report=term-missing
uv build --no-sources
python scripts/check_package.py
```

On a headless CI host select its supported offscreen Qt backend instead.
