# Issue #35 Windows validation

Validated locally on Windows on November 9, 2026 using Python 3.12.14,
PySide6 6.11.2 and the repository's locked dependencies.

- Full offscreen regression suite: **645 passed**, branch coverage **98.16%**.
- Ruff lint, Ruff formatting, Pyright and installed dependency compatibility pass.
- Wheel and source archive build and install outside the checkout; the installed
  application imports, starts, loads the sample CSV, plots and exits successfully.
- Native Windows source-picker suite: **12 passed**. These tests exercise the
  production dashboard and source controller, including actual curve data.
- Verified exact timestamp boundaries and precision, unavailable versus zero,
  duplicate column names, malformed/empty files, invalid UTF-8, size/row/sample
  limits, and cancellation while reading. Source files remain unchanged and
  can be removed after reading on Windows.
- Verified CSV/process/motion switching, deterministic retry, a worker failure,
  preparation cancellation, delayed cleanup, live disconnect, and window close.
  A deliberately disconnected render method makes the ordinary picker workflow
  test fail because samples do not reach the plot.

The native pytest-qt runs print a Windows exception report for `0x8001010d`
inside pytest-qt's setup event processing, then continue and pass with exit 0.
The same report reproduces in the unchanged baseline's
`test_source_workflow.py` / `test_simulator_workflow.py` native run (three tests
pass). It is not reproduced in the offscreen suite or the standalone review
window's stderr log. This report is retained in the local validation logs,
not hidden by disabling fault reporting.

The [manual walkthrough](../source-picker.md) covers the dashboard controls.
The CSV preset and production #31 arbitrary mapping integration are explicitly
separate; no physical USB/serial hardware is represented by this picker.
