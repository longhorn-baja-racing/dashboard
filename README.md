# Longhorn Baja Telemetry Dashboard

A Python desktop tool for exploring vehicle telemetry, built with PySide6 and
PyQtGraph. It is designed to be **sensor-agnostic**: any source that can
describe its signals (names, types, units and timestamps) can feed it through a
source adapter. **Windows is the first release target.** macOS and Linux are
supported for development.

New contributors: start with the
[roadmap and contribution guide (#101)](https://github.com/longhorn-baja-racing/dashboard/issues/101).

## Current status

What works on `main` today:

- A desktop shell that opens CSV files through **File > Open**, lists their
  columns, plots a selected column against `timestamp_ms` (or sample index),
  and shows the raw rows in a table.
- CSV decoding lives outside the Qt widgets. The tabular `LogSession` is a
  compatibility model, not the planned mixed-rate session model.
- Cross-platform CI with tests, lint, formatting, typing, packaging and
  installed-package smoke checks (see [Quality checks](#quality-checks)).

Everything else in this README is planned work tracked in
[GitHub issues](https://github.com/longhorn-baja-racing/dashboard/issues).
Open pull requests and closed, unmerged pull requests are not features on
`main`.

## First milestone: early Windows demo (M0)

The first deliverable is a demo that runs on a Windows laptop from the
developer setup below, with no vehicle, logger or installer required
([#102](https://github.com/longhorn-baja-racing/dashboard/issues/102)):

- open a sample CSV, or run deterministic simulated telemetry
- browse arbitrary signal names and units
- plot a signal against time
- basic replay: play, pause and seek
- clear empty, loading and error states

None of these flows depend on hard-coded sensor or channel names. Later
milestones in #101 cover a fuller local telemetry explorer (M1), optional real
vehicle and live integrations (M2), Windows distribution (M3) and analysis
features (M4).

## Architecture direction

- **Generic signals.** One schema describes arbitrary signals, each with its
  own identity, type, unit, sample times, source and availability. Signals can
  have different rates. Missing data stays distinguishable from zero, and raw
  values, units and timestamps are preserved.
- **Explicit source adapters.** CSV files, simulation and, later, other inputs
  (for example a native logger file, USB serial or CAN) each have an adapter
  that interprets its own documented format and emits the same core contract.
  Plots, the signal browser, statistics and workspaces do not depend on which
  adapter produced the data.
- **No guessing about hardware.** The dashboard does not auto-detect
  undocumented hardware or protocols. An adapter for a real device or log
  format is added only after that format is documented and backed by real
  sample files or golden vectors agreed with the
  [firmware](https://github.com/longhorn-baja-racing/firmware) owners.
- **Read-only telemetry.** Initial live sources only receive data; the
  dashboard sends no vehicle control commands.
- **Bounded and responsive.** Ingestion, memory and plot rendering stay
  bounded so long logs do not freeze a team laptop.
- **Qt-free core.** Signal, session and source code does not import Qt; widgets
  never parse files or serial data themselves.

Hardware and sensor selection for the vehicle is still open. Any sensor named
in issues, fixtures or examples (an IMU, GPS, a temperature sensor and so on)
is **illustrative only** and not a fixed channel list or a confirmed hardware
choice.

## Releases

Planned releases target Windows first: a portable ZIP and an installer
([#52](https://github.com/longhorn-baja-racing/dashboard/issues/52),
[#53](https://github.com/longhorn-baja-racing/dashboard/issues/53),
[#56](https://github.com/longhorn-baja-racing/dashboard/issues/56)). No release
has been published yet, but every pull request builds and smoke-tests a
portable Windows ZIP; see [portable build](docs/portable-build.md). macOS and Linux distributions are deferred until the
Windows release works; CI keeps running on all three platforms for development.

The dashboard is released under the [MIT License](LICENSE). Packaged builds
also contain third-party components under their own licenses; what each one
requires, and where the notices must go in a Windows build, is listed in
[third-party notices](docs/third-party-notices.md).

## Branch and contribution workflow

`main` is the primary integration branch and repository default. Start feature
branches from current `main`, open a PR targeting `main`, and merge after review
and required checks. Use version tags and GitHub Releases to identify releases.
`develop` is retained as a historical branch; new work should not target it.

Dependent PRs may temporarily target their prerequisite feature branch so each
diff stays reviewable. After that prerequisite merges, update the dependent
branch and retarget its PR to `main`. Do not merge closed or superseded PRs.

Issue titles use `[P0]`, `[P1]`, `[P2]`, `[P3]` or `[FUTURE]`. `[P0]` items build
the foundation and the M0 demo; `[P1]` items deliver the local telemetry
explorer and Windows packaging; `[P2]` adds analysis; `[P3]` and `[FUTURE]`
require a demonstrated need and do not block a release. Each implementation
issue states what can start now, real integration dependencies, non-goals,
tests and checkable acceptance criteria. GitHub issues are the source of truth
for completion status.

## Development setup

Python 3.10 to 3.13 is supported; `.python-version` selects 3.13 for
development. Install [uv](https://docs.astral.sh/uv/getting-started/installation/),
then:

```sh
git clone https://github.com/longhorn-baja-racing/dashboard.git
cd dashboard
uv sync --locked --extra dev
uv run --locked lbr-dashboard
```

`uv run --locked python -m lbr_dashboard` uses the same entry point. For a pip
environment with a supported Python, `python -m pip install -e .` installs the
application and `python -m pip install -e ".[dev]"` adds development tools.
`requirements.txt` delegates to `pyproject.toml`; dependencies are declared once.

To try the app, open `sample_data/sample_baja_log.csv` with **File > Open**.
Its columns are example data, not a required format.

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
CI runs Python 3.13 on Windows, macOS and Linux, and Python 3.10 to 3.12 on
Linux. It checks the lockfile, lint, formatting, typing, dependency
consistency, and regression tests with at least 90% branch-aware coverage.
Python warnings, unknown pytest settings or markers, unexpected passing xfails,
Qt critical logs, unhandled Qt callback exceptions, and hung tests fail the
run. Both the wheel and the source distribution must install in an isolated
environment and launch, load CSV data, plot a selected signal, and resolve
package resources without importing from the checkout. The Makefile provides
optional shortcuts; Windows contributors can use the commands above without
installing Make.

Dependency changes update `pyproject.toml` and `uv.lock` together.

CI runs on pull requests, main pushes, merge queues, manual dispatches, and a
weekly schedule. The stable `CI required` check fails if any platform job
fails, is cancelled, or is skipped. Reports, coverage XML, and distributions
are retained for seven days. Actions are pinned to commits; Dependabot proposes
dependency and action updates weekly.

Main requires up-to-date passing CI and a reviewed pull request; force pushes
and deletion are blocked. Do not rename `CI required` without updating the
GitHub branch rules. These checks exercise the current desktop shell and CSV
workflow. Hardware telemetry, planned features, and native installers require
their own acceptance tests when implemented.
