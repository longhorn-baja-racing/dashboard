# Issue #17 acceptance and PR #113 review validation

Validated on Windows with Python 3.12.14 on October 25, 2026, after integrating
main's strict CI and installed-package checks from #116.

## Acceptance criteria

| Requirement | Implementation and evidence |
| --- | --- |
| Core imports without Qt; parsing/storage stay outside widgets | A fresh subprocess blocks PySide6, PyQt6, PyQt5, and PyQtGraph imports, then imports every core module. `CsvImporter` decodes files; `DashboardController` owns `LogSessionStore`; widgets receive presentation rows and numeric series. |
| A worker cancels and joins, cleans up, and delivers errors to its owner | Lifecycle tests check cancellation, an unfinished join, successful join, `finally` cleanup, immutable event handoff, retained queued events, and owner-visible `ServiceFailure` details. |
| Registration is deterministic without provider-specific widget edits | Registry tests check ordering, duplicate/unknown/invalid IDs, independent application registries, factory arguments, and original error causes. A custom importer is selected and composed with the existing window/controller without widget changes. |
| Arbitrary signal IDs, units, and timestamps survive handoff | Acceptance tests preserve an arbitrary signal ID, custom unit, and negative timestamp in an immutable event; a custom imported column and timestamps are plotted through the ordinary presentation path. |
| Ownership, imports, errors, and cancellation are documented | `docs/ARCHITECTURE.md` and ADR 0001 describe the implemented layers and application-owned provider selection. No speculative registries, public plugin SDK, or dynamic provider loading were added. |

The tests are in `tests/architecture/test_acceptance.py`, `test_lifecycle.py`,
`test_registry.py`, `test_boundaries.py`, and `tests/ui/test_csv_workflow.py`.

## Review findings

All five import bypasses reported on PR #113 are rejected by the actual scanner
when injected into temporary copies of the corresponding production files.
The known forbidden-import control also fails. Additional cases cover every
import alias, nested relative imports, literal `import_module` calls and aliases,
literal relative package arguments, `__import__` fromlists, and imports of
top-level application modules such as `dashboard_controller`.

Direct `MainWindow.load_csv()` calls propagate the original filesystem and
decoding exceptions. File-menu requests catch exceptions at the Qt slot boundary
and expose them through a status message and `csv_load_failed(path, message)`.
Failed loads preserve the current session and plot; recovery clears the error.
Cancellation of the file chooser emits no new failure.

Regression tests also verify duplicate CSV names by physical column index,
finite-coordinate plotting, missing cells, successful empty-load clearing,
header-only files, and loading another file afterward. Empty successful imports
clear the model and display together; failed imports retain both. Main's older
empty-import retention tests were updated to assert that distinction.

## Local results

- All 251 tests pass with offscreen Qt and with the Windows Qt backend.
- Strict configuration/markers, warnings-as-errors, and the 30-second test
  timeout are enabled. Branch-aware coverage is 94.14%, above the 90% gate.
- Ruff lint, Ruff formatting, Pyright, and dependency checks pass.
- `uv build --no-sources` produces the wheel and source distribution.
- `scripts/check_package.py` installs each distribution in an isolated temporary
  environment and passes import-location, resources, sample CSV, selection,
  plotting, console-entry-point, event-loop, and dependency checks.
- A separate Windows event-loop run exercised both duplicate columns with a
  NaN timestamp, empty-load clearing, a visible invalid-encoding error with
  retained data, and recovery to the sample CSV; it exited 0.

The two review failures were deliberately restored in disposable source/test
copies. Restoring the original boundary scanner causes all five bypass
regressions to fail, while its control case still passes. Restoring signal-only
direct loading causes both missing-file and invalid-encoding propagation tests
to fail. The working source remains untouched during these checks.

The static import guard covers documented static/literal forms; computed module
names and runtime code generation are outside its scope. Automated file-menu
checks supply file-chooser return values rather than navigating the native
chooser. Other platforms/Python versions are covered by the GitHub CI matrix;
their results must be checked for the pushed commit before merging.

## Rerun

```text
uv sync --locked --extra dev
uv pip check
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pyright
uv run --locked pytest --cov --cov-report=xml --junitxml=test-results/pytest.xml
uv build --no-sources
uv run --locked python scripts/check_package.py
```

Set `QT_QPA_PLATFORM=offscreen` for headless checks, or `windows` for the native
Windows backend. The local environment used the existing Python 3.12 runtime
explicitly. Local cache/temp paths were redirected to a fresh workspace folder
because an older pytest cache had Windows access restrictions.
