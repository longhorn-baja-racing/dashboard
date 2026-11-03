# Issue #24 local preferences validation

Validated on Windows with Python 3.12.14 and Qt 6.11.2, October 29, 2026.
Branch `codex/p1-24-local-preferences` starts from simulator commit `3e925a3`.

## Acceptance evidence

| Requirement | Verification |
| --- | --- |
| Settings survive restart | A real subprocess reopens saved preferences and asserts theme plus two unrelated source kinds/locations. UI tests recreate a window/store, check the saved theme and recent files, and reopen a CSV from the real menu. |
| Invalid input cannot partially change active state | Immutable validated preference values; complete validation precedes writes or active updates. Tests cover malformed UTF-8/JSON, duplicate keys, future versions, wrong types, oversized/deep/node-heavy documents, cycles, non-finite values and invalid fields. Existing active preferences remain unchanged. |
| Failed saves preserve prior data | Same-directory temporary writes, flush/fsync/close and atomic replacement. Tests inject backup replacement, primary replacement, fsync, temporary-file and directory failures, checking exact prior bytes, active object identity and temporary-file cleanup. An unreadable or unsupported-version primary is not silently overwritten. |
| Recovery and migration | Valid previous primaries become atomic backups. Corrupt primary recovery is visible; corrupt data cannot replace a valid backup. Both-invalid load retains active settings. Explicit version-0 migration is deterministic, deduplicates paths and retains unknown metadata. Saving writes version 1. |
| Machine-local versus portable | Platform-local config paths; theme, default source kind and at most ten recent locations. No workspace layout, raw measurements or signal units/time/quality are serialized. No source auto-opening on startup. Portable workspace schema/import/export remains #49. |
| Real app integration | Production Preferences → Theme and File → Recent CSV files menus use an application controller. Saving succeeds before theme/history changes become active. Failed file loads do not enter history; failed history saves do not discard successfully loaded telemetry. Clear recent does not delete files or clear current data. |

## Results

- **650 strict tests passed**, warnings as errors; **98.48%** overall branch-aware
  coverage. Preference storage coverage rounds to 99%.
- Ruff lint/format and Pyright passed. Installed wheel/source-distribution checks
  include preference save/reopen plus existing simulator/source/session/clock,
  resources, application startup, CSV plotting and dependency compatibility.
- The native Windows verification window passed visible light/dark changes,
  saved theme/recent paths on restart, recent-file reopening/clear, backup
  recovery and simulated failed saves. Visual inspection caught and corrected
  stale child-widget colors; UI tests assert light table/list/menu palettes and
  preservation of actual plotted readings during theme changes.
- Tests and the manual harness use isolated paths, leaving user settings alone.
  `main()` uses the platform store; composition without an explicit store remains
  memory-only for embedding/tests.

## Mutation evidence

Four isolated mutated copies caused targeted assertion failures:

1. Updating active preferences before completing a save fails all five injected
   save-interruption cases.
2. Saving new data as the backup fails previous-version recovery checks.
3. Allowing unsupported future-version overwrite fails refusal/preservation.
4. Resetting active preferences on invalid input fails 20 malformed-field cases.

Corresponding unmodified tests pass; production files were not changed by the
mutation runs.

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

Use a supported offscreen backend for headless CI. Schema, bounds, migration,
platform paths, UI behavior and filesystem/multi-writer limits are documented in
`docs/local-preferences.md`. No PR/push is part of this local verification step.

## Manual-review contrast correction

The user's screenshot exposed low-contrast toolbar text for widgets added after
theme setup. Explicit themed tool-button and inspector colors now cover late
children. Three new native-widget cases check readable system/light/dark colors.
The local harness starts with a temperature sample and keeps its inspector compact;
restart still intentionally reloads preferences without automatically opening files.

## Manual-review graph edit correction

The user reproduced editable table cells diverging from the application model.
Cell edits now commit an immutable model update and redraw the selected plot,
without writing the source file or preferences. Nine tests cover actual keyboard
editor commit, value/timestamp updates, physical duplicate columns, ragged cells,
non-finite/text/blank missingness, selection changes and reload/reset behavior.
Programmatic table loading blocks edit signals. The native harness also checks
changing the first pressure value to 20 updates the curve while CSV bytes stay
unchanged.
