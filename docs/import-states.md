# Actionable import states (P0 issue 103)

Fresh launch explains the supported flow and offers **Open CSV** and **Try demo
data**. No data is shown as connected. The demo is a small static fictional
fixture; it is explicitly not a live sensor. Select `lab.temperature` or
`bench.pressure` to plot the known readings, or inspect `status.note` in the table.
The table is read-only, so editing it cannot misleadingly appear to change the
underlying data.

File-menu and Open CSV imports use the existing CSV parser on one owned worker
thread. An indeterminate activity bar, row count and Cancel import action remain
responsive while reading. The previous graph/table remains available. A second
import cannot start until the first worker finishes. Cancel suppresses a completed
but not yet displayed result as well as requesting cancellation during parsing.
Numeric conversion and validation are performed before the owner commits a new
model and presents it. Cancellation does not commit partial results.

Close requests cancellation and joins for at most one second. If a file read is
still blocked, the window remains open with an explanation rather than abandoning
the reader. Local file I/O can block in the operating system; cancellation is
cooperative between parser iterations, not a promise to interrupt a kernel read.
The worker never accesses Qt widgets. The owner polls one bounded result slot;
diagnostic counters do not accumulate unbounded progress events.

The legacy table preview has explicit limits: 16 MiB file size at open, 50,000
data rows, 128 columns, and 50,000 cells including the header. Oversized input is
rejected with a smaller-file recovery action rather than building millions of
QTableWidget cells. CSV's normal field-length limit also remains in effect.
These are preview bounds, not performance claims for the planned session store.

## Failure and recovery

Failed UI imports keep the previous model, selection, plot and raw table intact.
The inline nonmodal message gives both a reason and recovery action. Error details
can be hidden/shown or copied locally. Open another CSV or Try demo data recovers
from an error; successful loads clear stale error details. Cancelling the file
chooser changes nothing.

The existing `MainWindow.load_csv(path)` synchronous compatibility API remains
available to callers and propagates importer exceptions. The interactive actions
use the new validated asynchronous path; they do not raise hidden Qt-slot errors.

| Input | Interactive behavior |
| --- | --- |
| Missing file / permissions / invalid UTF-8 | Clear reason and corrective action; old session retained |
| Empty or header-only file | No-data error; old session retained |
| Broken quotes, inconsistent row widths | Malformed-file error; no partial table |
| Duplicate or empty headers | Ambiguous-name error; no incorrect column lookup |
| Invalid/missing/non-finite timestamp_ms | Time-column error; no fabricated time or blanked prior graph |
| NaN/infinite numeric value | Explicit error; empty cells are the supported missing-value representation |
| No timestamp_ms column | Visible sample-index warning; no inferred measurement unit |
| Text/status column | Raw values retained; visible explanation that the current preview plots numeric values only |
| Duplicate/out-of-order finite times | Original row order and values retained; no sorting or timestamp rewriting |
| UTF-8 BOM / quoted commas / empty numeric cells / zero | Supported; originals unchanged, empty remains missing and zero remains zero |
| Non-.csv extension | Unsupported-file explanation |

No importer export or file rewrite is performed. The app's current wide CSV model
is retained. Timestamp/unit mapping belongs to #31; simulator/source selection
and final demo integration belong to #23/#35/#102. This branch does not claim
those unmerged features are available. It emits `import_failed(reason, recovery)`
once per failed import for application observers. Connecting to #25 diagnostics
remains a follow-up after that implementation lands; no second error service or
remote reporting is introduced.

## Validation

Windows, Python 3.12.14, PySide6/Qt 6.11.2:

- **52 tests passed**, **97.05%** branch-aware coverage.
- **22 native Windows UI tests passed**, including real clipboard copy.
- Native application smoke clicked production Open CSV, demo, details and copy
  buttons; verified actual demo series, visible loading, preserved data after a
  malformed import, recovery and shutdown. Screenshots were visually inspected.
- Separate regression mutations: disconnect the demo button, skip validation, or
  discard the stored model on error. Each variant failed its intended test.
- Ruff lint/format, Pyright, and isolated wheel/sdist import/resource/CSV/plot/
  application-event-loop smoke passed. Package smoke also exercises demo and
  asynchronous loading rather than only the compatibility method.

Use `uv sync --locked --extra dev`, then the existing README quality commands.
Focused checks: `uv run --locked pytest tests/ui/test_import_states.py
tests/importers/test_import_limits.py`. Qt tests use offscreen mode by default;
set `QT_QPA_PLATFORM=windows` to run them with native Windows Qt.

Manual check: launch normally, click Try demo data, select pressure, then Open CSV
with a malformed or non-UTF-8 file. Expect actionable inline details with the
previous pressure graph still present. Copy the details, then load a valid file
or click Try demo data again. No device or network is needed.
