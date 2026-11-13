# Diagnostics and local logs

When something goes wrong at the track, the dashboard keeps local evidence and
tells the user once, without stopping. Nothing is uploaded anywhere.

## Where logs go

| Platform | Folder |
| --- | --- |
| Windows | `%LOCALAPPDATA%\LBR Dashboard\logs` |
| macOS | `~/Library/Logs/LBR Dashboard` |
| Linux | `$XDG_STATE_HOME/lbr-dashboard/logs` (default `~/.local/state/lbr-dashboard/logs`) |

The file is `dashboard.log`. Help > Diagnostics shows the exact path and has an
**Open log folder** button.

Environment overrides:

- `LBR_DASHBOARD_LOG_DIR` sets the folder (tests use this to stay out of real
  user folders).
- `LBR_DASHBOARD_LOG_LEVEL` sets the level (`DEBUG`, `INFO`, `WARNING`,
  `ERROR`). The default is `INFO`; unknown values fall back to the default.

## Bounds

- Each file is at most 1 MiB, rotated to `dashboard.log.1` to `.3`, so logs
  use at most 4 MiB.
- A single message is cut to 4000 characters, so a bug can never dump a large
  block of telemetry into the log.
- The in-memory list of recent warnings and errors keeps the newest 50.

## What is kept out

- Credential values: anything like `password=...`, `token: ...`,
  `api_key=...` or `Authorization: Bearer ...` is replaced with `<redacted>`,
  including inside tracebacks.
- The user's home folder is shown as `~`, so reports do not reveal user names.
- Telemetry values and file contents are not logged by the diagnostics code,
  and the report says so. Code that logs should name signals and sources, not
  dump samples.

## When logging itself fails

Logging never stops the application:

- If the folder cannot be created or opened, logging continues to stderr and
  memory, and Diagnostics shows `Log status: not writing a log file (...)`.
- If a write fails later (full disk, a file locked during rotation because two
  copies are running on Windows, a removed folder), the failure is counted, one
  short note goes to stderr, and Diagnostics shows the count and last reason.
- Closing the log at exit tolerates the same failures.

## Uncaught errors

`sys.excepthook` and `threading.excepthook` are installed at startup. This
covers exceptions raised in Qt slots (PySide6 routes them to `sys.excepthook`)
and in worker threads. Each one is:

1. Logged at `CRITICAL` with its full traceback.
2. Shown to the user in a non-modal notice on the GUI thread, with the short
   error, its location (`file.py:line in function`, no full paths), and buttons
   to copy details or open Diagnostics.

The same failure (same exception type and location) is shown only once per
run. While a notice is open, further different errors are counted in that
notice instead of stacking dialogs. `KeyboardInterrupt` keeps its normal
behavior. Qt's own warnings are routed into the log as well.

## Diagnostics report

Help > Diagnostics opens a non-modal window with a plain-text report that can
be copied, saved to a `.txt` file, or refreshed. It contains:

- dashboard, Python, platform, Qt runtime, PySide6, pyqtgraph and numpy
  versions, and whether this is a frozen (packaged) build
- the log file path and log status
- recent warnings and errors (redacted)

Source and worker health will be added through the report's `extra` lines as
the source contract (#20) and health display (#44) land.

## For developers

```python
import logging

logger = logging.getLogger(__name__)  # inside lbr_dashboard modules
logger.warning("Source %s dropped %d frames", source_id, count)
```

Use the standard `logging` module; there is no wrapper. The Qt-free pieces
live in `lbr_dashboard.diagnostics` (`configure_logging`,
`install_exception_hooks`, `build_report`), and the Qt presentation in
`lbr_dashboard.ui.diagnostics`. `app.main()` wires them together and undoes
everything on exit.
