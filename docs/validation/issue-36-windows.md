# Issue #36 Windows validation

Validated on Windows on November 9, 2026 with locked dependencies,
Python 3.12.14 and PySide6 6.11.2.

- Final full offscreen run: **664 passed**, **98.26%** branch coverage.
- Ruff lint/format checks, Pyright and dependency compatibility pass.
- Native Windows browser/picker checks: **31 passed**.
- Wheel and source archive both build, install outside the checkout, and pass
  application startup, CSV, plotting, resource and entry-point smoke checks.
- Focused browser/picker run passes, including the real source-controller binding.
- Repeated CSV/simulator switching and filtered dashboard plot selection three
  times with coverage enabled: two workflow tests pass on every repetition.
- Browser tests include 3,000 descriptors in nested families, duplicate labels,
  ID/path/unit/source/device search, unavailable and not-yet-observed readings,
  selection hidden by filtering, catalog replacement, model-index stability
  during availability updates and drag data through the proxy model.
- A negative control deliberately changes the search role and verifies that
  the normal search test fails.

An earlier full verification process stalled with a faulthandler snapshot in
Qt/PyQtGraph event processing and the coverage collector. It was interrupted.
The final isolated full run and all three repeated workflow runs above pass;
the cause of that earlier stall has not been established.

Native pytest-qt checks on this machine can print the existing Windows COM
exception report documented in [issue #35 validation](issue-35-windows.md).
That report also reproduces on the unchanged pre-picker baseline and does not
terminate those test runs.

See the [manual walkthrough and drag contract](../signal-browser.md). Open the
dashboard, connect a simulator, search for temperature or its unit, select the
leaf, clear the filter and retry. Selection should remain on the same ID.
