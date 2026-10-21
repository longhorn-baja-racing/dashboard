# Longhorn Baja Telemetry Dashboard

A Python desktop telemetry tool built with PySide6 and PyQtGraph. Windows is
the first-class target; macOS and Linux are supported development targets.

## Current status

The app currently launches a desktop shell and opens legacy CSV files through
**File → Open**. CSV decoding lives outside Qt widgets. The tabular `LogSession`
is a compatibility model, not the planned mixed-rate telemetry session model.
Native SD-log replay, USB live telemetry, workspaces and installers are planned
in the [issue roadmap](https://github.com/longhorn-baja-racing/lbr-dashboard/issues).
Closed, unmerged PRs are not completed features.

## Project direction

- One schema describes arbitrary signals, each with its own sample times, units,
  source identity and availability. Missing data stays distinguishable from zero.
- Replay, live USB and simulation feed one `TelemetrySource` contract and shared
  telemetry/session model. One clock owns replay and selected time across widgets.
- The 2026 logger is a Raspberry Pi Pico with an Adafruit BNO055 and microSD.
  Log acceleration, gyro, temperature, quaternion/orientation and calibration
  state when available. There is no GPS this season.
- Full-rate SD logs are authoritative. Read-only USB serial is the first live
  transport; received desktop data may be downsampled or incomplete. Preserve
  device monotonic measurement timestamps; track sequence gaps, freshness and heartbeats.
- Preserve raw data and expose a configurable sensor-to-vehicle transform.
  Protocol definitions and golden vectors must be shared with
  [lbr-vehicle-firmware](https://github.com/longhorn-baja-racing/lbr-vehicle-firmware).
- Build saveable workspaces around a hierarchical signal browser, PyQtGraph
  plots, multiple signals/Y axes, synchronized cursor and replay controls.
  Analysis follows the core: derived signals, run comparison, annotations,
  alarms and statistics. Keep ingestion, memory and rendering bounded.
- Releases will include a standalone Windows installer and portable ZIP, plus
  tested macOS/Linux artifacts. Future sensors, CAN, radio, GPS and video extend
  the shared model.

## Branch and contribution workflow

`main` is the primary integration branch and repository default. Start feature
branches from current `main`, open a PR targeting `main`, and merge after review
and required checks. Use version tags and GitHub Releases to identify releases.
`develop` is retained as a historical branch; new work should not target it.

Dependent PRs may temporarily target their prerequisite feature branch so each
diff stays reviewable. After that prerequisite merges, update the dependent
branch and retarget its PR to `main`. Do not merge closed or superseded PRs.

Issue titles use `[P0]`, `[P1]`, `[P2]`, `[P3]` or `[FUTURE]`. Every implementation
issue states context, scope, architecture guidance, dependencies, out of scope,
test requirements and objective acceptance criteria. `[P0]` establishes the
foundation; `[P1]` delivers the 2026 core and packaging; `[P2]` adds analysis;
`[P3]` and `[FUTURE]` require a demonstrated need and are not 2026 release blockers.
GitHub issues are the source of truth for completion status.

## Development setup

Python 3.10–3.13 is supported; `.python-version` selects 3.13 for development.
Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then:

```sh
git clone https://github.com/longhorn-baja-racing/lbr-dashboard.git
cd lbr-dashboard
uv sync --locked --extra dev
uv run --locked lbr-dashboard
```

`uv run --locked python -m lbr_dashboard` uses the same entry point. For a pip
environment with a supported Python, `python -m pip install -e .` installs the
application and `python -m pip install -e ".[dev]"` adds development tools.
`requirements.txt` delegates to `pyproject.toml`; dependencies are declared once.

## Package layout

- `lbr_dashboard.app` composes the application and CSV importer.
- `lbr_dashboard.core.log` contains the immutable CSV compatibility model and
  session holder without Qt dependencies. The shell owns the current holder.
- `lbr_dashboard.importers.csv_importer` decodes CSV into that model.
- `lbr_dashboard.ui` renders the supplied rows and values.
- `lbr_dashboard.resources` loads bundled resources; `version.py` owns the version.

The older `src/main.py`, `src/app.py`, `src/ui` and `src/models` paths are import
wrappers for compatibility. New implementation belongs in `src/lbr_dashboard`.

## Quality checks

Run the same checks locally as CI:

```sh
uv sync --locked --extra dev
uv run --locked pytest --cov --cov-report=xml
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pyright
uv build --no-sources
uv run --locked python scripts/check_package.py
```

Tests set Qt's offscreen mode and require no physical telemetry hardware.
CI runs Python 3.13 on Windows, macOS and Linux, and Python 3.10–3.12 on Linux.
It checks the lockfile, lint, formatting, typing, dependency consistency, and regression tests with at least 90% branch-aware coverage. Python warnings, unknown pytest settings/markers, unexpected passing xfails, Qt critical logs, unhandled Qt callback exceptions, and hung tests fail the run. Both the wheel and source distribution must install in an isolated environment and launch, load CSV data, plot a selected signal, and resolve package resources without importing from the checkout.
The Makefile provides optional shortcuts; Windows contributors can use the
commands above without installing Make.

Dependency changes update `pyproject.toml` and `uv.lock` together. Before
distributing releases, the team must choose and include a project license and
the required bundled-dependency notices.

CI runs on pull requests, main pushes, merge queues, manual dispatches, and a weekly schedule. The stable `CI required` check fails if any platform job fails, is cancelled, or is skipped. Reports, coverage XML, and distributions are retained for seven days. Actions are pinned to commits; Dependabot proposes dependency and action updates weekly.

Main requires up-to-date passing CI and a reviewed pull request; force pushes and deletion are blocked. Do not rename `CI required` without updating the GitHub branch rules. These checks exercise the current desktop shell and CSV workflow. Hardware telemetry, planned features, and native installers require their own acceptance tests when implemented.
